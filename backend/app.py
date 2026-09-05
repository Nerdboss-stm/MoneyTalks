import asyncio
import logging
import os
import sys
from typing import Any

import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from mandate import desk as D
from mandate import runner, voice
from mandate.bus import EventBus, bus
from mandate.desk import DeskError
from mandate.schemas import Event

structlog.configure(
    processors=[structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()

app = FastAPI(title="mandate")


class Runtime:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.run_version = os.environ.get("RUN_VERSION", "v1")
        self.run_index = int(os.environ.get("RUN_INDEX", "1"))
        self.speed = float(os.environ.get("RUN_SPEED", "0"))
        self.fleet, self.desk, _ = runner.build_fleet(self.run_version, self.run_index, self.speed, bus=bus)
        self.replayer: runner.Replayer | None = None
        self.task: asyncio.Task | None = None


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(bus)
    return _runtime


class CompileIn(BaseModel):
    text: str


class ConfirmIn(BaseModel):
    mandate_id: str


class ReplayCmd(BaseModel):
    cmd: str
    arg: Any = None


class VoiceText(BaseModel):
    text: str
    as_of: str | None = None
    session: str | None = None


class Released(BaseModel):
    agent_id: str | None = None


class ExplainLoad(BaseModel):
    dir: str | None = None
    mode: str = "v2"
    index: int = 1


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/desk/compile")
async def desk_compile(body: CompileIn) -> dict:
    return await get_runtime().desk.compile(body.text)


@app.post("/desk/inject")
async def desk_inject() -> dict:
    return await get_runtime().desk.inject()


@app.post("/desk/confirm")
async def desk_confirm(body: ConfirmIn) -> dict:
    try:
        m = await get_runtime().desk.confirm(body.mandate_id)
    except DeskError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return m if isinstance(m, dict) else m.model_dump(mode="json")


@app.get("/record/{agent_id}")
async def record(agent_id: str, as_of: str | None = None, session: str | None = None) -> dict:
    rt = get_runtime()
    if agent_id not in rt.fleet.agents:
        raise HTTPException(status_code=404, detail="unknown agent")
    return rt.desk.record_for(agent_id, as_of, session)


def _prove(v1: str, v2: str) -> dict:
    try:
        base = runner.prove(v1, v2)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"missing recording: {exc.filename}")
    return base | D.prove_extras()


@app.get("/prove")
async def prove(v1: str = "mandate-v1-01", v2: str = "mandate-v2-01") -> dict:
    return _prove(v1, v2)


@app.get("/explain/prove")
async def explain_prove(v1: str = "mandate-v1-01", v2: str = "mandate-v2-01") -> dict:
    return _prove(v1, v2)


@app.get("/explain/links")
async def explain_links() -> dict:
    return D.links_payload()


@app.post("/run")
async def start_run(until: str = "Fri 17:05") -> dict:
    rt = get_runtime()
    if rt.task and not rt.task.done():
        raise HTTPException(status_code=409, detail="a run or replay is active")
    rt.desk.replay_session = None
    rt.task = asyncio.create_task(runner.run(rt.run_version, rt.run_index, rt.speed, bus=rt.bus, until=until))
    return {"started": runner.session_id(rt.run_version, rt.run_index)}


# /replay/cmd must be declared before /replay/{session_id} or the wildcard shadows it
@app.post("/replay/cmd")
async def replay_cmd(body: ReplayCmd) -> dict:
    rt = get_runtime()
    if rt.replayer is None:
        raise HTTPException(status_code=409, detail="no replay active")
    return rt.replayer.command(body.cmd, body.arg)


@app.post("/replay/{session_id}")
async def start_replay(session_id: str, speed: float = 1.0, stage: bool = False) -> dict:
    rt = get_runtime()
    if rt.task and not rt.task.done():
        raise HTTPException(status_code=409, detail="a run or replay is active")
    rt.desk.replay_session = session_id
    if stage:
        rt.replayer = runner.Replayer(rt.bus, runner.load_events(session_id), 1.0)

        async def go() -> None:
            for seg in runner.load_stage():
                rt.replayer.seek(seg["start"])
                rt.replayer.set_speed(float(seg["speed"]))
                await rt.replayer.play(until=seg["end"])
            rt.replayer.done = True

        rt.task = asyncio.create_task(go())
    else:
        rt.replayer = runner.Replayer(rt.bus, runner.load_events(session_id), speed)
        rt.task = asyncio.create_task(rt.replayer.play())
    return rt.replayer.status()


# ---- explain


@app.post("/explain/load")
async def explain_load(body: ExplainLoad) -> dict:
    from explain.meeting import data_dir

    rt = get_runtime()
    try:
        meeting = rt.desk.load_meeting(body.dir or data_dir(), body.mode, body.index)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await rt.desk.publish_meeting_loaded()
    return meeting.summary()


@app.get("/explain/evidence")
async def explain_evidence() -> dict:
    rt = get_runtime()
    if rt.desk.meeting is None:
        raise HTTPException(status_code=404, detail="no meeting loaded")
    return rt.desk.meeting.evidence


# ---- voice


@app.post("/voice/text")
async def voice_text(body: VoiceText) -> dict:
    rt = get_runtime()
    return await rt.desk.voice_turn(body.text, as_of=body.as_of, session=body.session)


@app.post("/voice/utterance")
async def voice_utterance(audio: UploadFile = File(...), as_of: str | None = None, session: str | None = None) -> dict:
    rt = get_runtime()
    data = await audio.read()
    text = await voice.stt(data, audio.content_type or "audio/webm")
    if rt.replayer is not None and not rt.replayer.done:
        rt.replayer.pause()
    out = await rt.desk.voice_turn(text, as_of=as_of, session=session)
    out["transcript"] = text
    return out


@app.post("/voice/released")
async def voice_released(body: Released) -> dict:
    rt = get_runtime()
    if body.agent_id:
        await rt.fleet.publish("agent.released", agent_id=body.agent_id)
    if rt.replayer is not None and not rt.replayer.done:
        rt.replayer.resume()
    return {"ok": True}


@app.get("/voice/stream/{job_id}")
async def voice_stream(job_id: str) -> StreamingResponse:
    job = voice.JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown speech job")
    return StreamingResponse(job.iter(), media_type="audio/mpeg", headers={"Cache-Control": "no-store", "X-Voice-Source": job.source})


@app.get("/audio/{name}")
async def audio_file(name: str) -> FileResponse:
    if not name.endswith(".mp3") or "/" in name or ".." in name:
        raise HTTPException(status_code=404)
    p = voice.AUDIO_CACHE / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="not cached")
    return FileResponse(p, media_type="audio/mpeg")


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    rt = get_runtime()
    queue: asyncio.Queue[Event] = asyncio.Queue()
    unsubscribe = rt.bus.subscribe(lambda e: queue.put_nowait(e))
    log.info("ws.connect", client=str(ws.client))

    async def sender() -> None:
        while True:
            await ws.send_text((await queue.get()).model_dump_json())

    async def receiver() -> None:
        while True:
            msg = await ws.receive_json()
            if rt.replayer is not None and "cmd" in msg:
                await ws.send_json({"type": "replay.status", "payload": rt.replayer.command(msg["cmd"], msg.get("arg"))})

    try:
        await asyncio.gather(sender(), receiver())
    except (WebSocketDisconnect, RuntimeError):
        log.info("ws.disconnect", client=str(ws.client))
    finally:
        unsubscribe()
