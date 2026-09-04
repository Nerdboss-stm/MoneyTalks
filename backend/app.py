import asyncio
import logging
import os
import sys
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from mandate import runner
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
    return m.model_dump(mode="json")


@app.get("/record/{agent_id}")
async def record(agent_id: str, as_of: str | None = None) -> dict:
    rt = get_runtime()
    if agent_id not in rt.fleet.agents:
        raise HTTPException(status_code=404, detail="unknown agent")
    return rt.fleet.get_record(agent_id, as_of)


@app.get("/prove")
async def prove(v1: str = "mandate-v1-01", v2: str = "mandate-v2-01") -> dict:
    try:
        return runner.prove(v1, v2)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"missing recording: {exc.filename}")


@app.post("/run")
async def start_run(until: str = "Fri 17:05") -> dict:
    rt = get_runtime()
    if rt.task and not rt.task.done():
        raise HTTPException(status_code=409, detail="a run or replay is active")
    rt.task = asyncio.create_task(runner.run(rt.run_version, rt.run_index, rt.speed, bus=rt.bus, until=until))
    return {"started": runner.session_id(rt.run_version, rt.run_index)}


@app.post("/replay/{session_id}")
async def start_replay(session_id: str, speed: float = 1.0, stage: bool = False) -> dict:
    rt = get_runtime()
    if rt.task and not rt.task.done():
        raise HTTPException(status_code=409, detail="a run or replay is active")
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


@app.post("/replay/cmd")
async def replay_cmd(body: ReplayCmd) -> dict:
    rt = get_runtime()
    if rt.replayer is None:
        raise HTTPException(status_code=409, detail="no replay active")
    return rt.replayer.command(body.cmd, body.arg)


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
