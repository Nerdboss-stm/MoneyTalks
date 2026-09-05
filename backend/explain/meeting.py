"""The variance review meeting: load the engine, publish meeting.loaded, ask six questions through the desk."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from explain import ingest, owners
from explain.engine import Engine

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "shared" / "explain"
LIVE = DATA / "live"
RECORDINGS = ROOT / "recordings"

QUESTIONS = [
    "What changed in August?",
    "Controller A, what drove enterprise revenue?",
    "Procurement, why is cloud hosting up?",
    "Controller B, what happened to SMB subscriptions?",
    "Payroll, anything to flag?",
    "Collections, how did mid-market subscriptions move?",
]


def data_dir() -> Path:
    return LIVE if any(LIVE.glob("transactions_*.csv")) else DATA


def load_engine(directory: Path | None = None) -> Engine:
    directory = directory or data_dir()
    files = sorted(directory.glob("transactions_*.csv")) + sorted(directory.glob("summary_*.csv"))
    if not files:
        raise FileNotFoundError(f"no transactions_*.csv in {directory}")
    norm = ingest.load(files)
    return Engine.from_periods(norm.transactions, norm.summaries)


def session_id(mode: str, index: int) -> str:
    return f"explain-{mode}-{index:02d}"


class QuietJob:
    """Headless meetings do not synthesize audio; the answer text still goes through the gate and the bus."""

    def __init__(self, text: str, voice_id: str) -> None:
        self.text = text
        self.voice_id = voice_id
        self.url = None
        self.error = None
        self.duration_ms = 0
        self.source = "quiet"


async def quiet_speak(text: str, voice_id: str) -> QuietJob:
    return QuietJob(text, voice_id)


async def run_meeting(mode: str, index: int, directory: Path | None = None, llm: Any = None, speak: Any = None, memory_path: Path = owners.MEMORY_FILE, bus: Any = None, questions: list[str] | None = None) -> dict:
    from mandate import prism_util
    from mandate.agents import Fleet, RuleDecider
    from mandate.bus import EventBus
    from mandate.desk import Desk
    from mandate.scenario import CompanyClock, build_scenario, ct

    sid = session_id(mode, index)
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS / f"{sid}.jsonl"
    if bus is None:
        if path.exists():
            path.unlink()
        bus = EventBus(sink=path)
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 10:00")), bus, decider=RuleDecider(), session_id=sid, run_version=mode, handlers={"explain": True} if prism_util.config() else None)
    desk = Desk(fleet)
    desk.memory_path = memory_path
    desk.load_meeting(directory or data_dir(), mode, index)
    await desk.publish_meeting_loaded()
    turns = []
    for q in questions or QUESTIONS:
        out = await desk.voice_turn(q, speak=speak or quiet_speak, explain_llm=llm)
        turns.append(out)
        await fleet.publish("explain.turn", question=q, intent=out["intent"], agent_id=out.get("agent_id"), answer=out.get("answer"), verify=out.get("verify"), replaced=out.get("replaced"))
    learned = None
    if mode == "v2":
        learned = owners.learn(desk.meeting.engine, sid, memory_path)
        await fleet.publish("memory.learned", session_id=sid, line=learned)
    await fleet.publish("meeting.end", session_id=sid, turns=len(turns), unverifiable=sum(len((t.get("verify") or {}).get("unverifiable_figures", [])) for t in turns))
    await fleet.prism.drain()
    return {"session_id": sid, "recording": str(path), "turns": turns, "learned": learned}


def main(argv: list[str] | None = None) -> int:
    from mandate.runner import load_env

    load_env()
    ap = argparse.ArgumentParser(prog="python -m explain.meeting")
    ap.add_argument("--mode", default="v2", choices=["v1", "v2"])
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--speak", action="store_true", help="synthesize audio for each answer (costs ElevenLabs credits)")
    a = ap.parse_args(argv)
    speak = None
    if a.speak:
        from mandate import voice

        speak = voice.speak
    out = asyncio.run(run_meeting(a.mode, a.index, Path(a.dir) if a.dir else None, speak=speak))
    for t in out["turns"]:
        v = t.get("verify") or {}
        print(f"[{t['intent']:>12}] {t.get('agent_id') or '-':<13} {t.get('answer')}", file=sys.stderr)
        print(f"               verify ok={v.get('ok')} unverifiable={v.get('unverifiable_figures')} coverage={v.get('driver_coverage_pct')} replaced={t.get('replaced')}", file=sys.stderr)
    if out["learned"]:
        print(f"memory: {out['learned']}", file=sys.stderr)
    print(out["recording"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
