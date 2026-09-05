from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx

from mandate.scenario import ROLES

ROOT = Path(__file__).resolve().parents[2]
VOICES_FILE = ROOT / "config" / "voices.json"
AUDIO_CACHE = ROOT / "audio_cache"
ELEVEN = "https://api.elevenlabs.io"
STT_MODEL = "scribe_v1"
TTS_MODEL = "eleven_flash_v2_5"
OUTPUT_FORMAT = "mp3_44100_128"
STT_TIMEOUT = 6.0
TTS_TIMEOUT = 20.0
MP3_BPS = 128_000


def api_key() -> str | None:
    for k in ("ELEVENLABS_API_KEY", "elevenlabs_api_key", "ELEVEN_API_KEY"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return None


class Voices:
    def __init__(self, desk: str, agents: dict[str, str]) -> None:
        self.desk = desk
        self.agents = agents

    def for_agent(self, agent_id: str | None) -> str:
        return self.agents[agent_id] if agent_id else self.desk


def load_voices(path: Path = VOICES_FILE) -> Voices:
    if not path.exists():
        raise RuntimeError(f"voices: {path} is missing")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"voices: {path} must be an object with desk and agents")
    desk = data.get("desk")
    agents = data.get("agents") or {}
    missing = [r for r, _ in ROLES if not str(agents.get(r, "")).strip()]
    if not str(desk or "").strip():
        missing.insert(0, "desk")
    if missing:
        raise RuntimeError(f"voices: missing voice id for {', '.join(missing)} in {path}")
    return Voices(desk=str(desk), agents={r: str(agents[r]) for r, _ in ROLES})


VOICES = load_voices()


def cache_key(text: str, voice_id: str) -> str:
    return hashlib.sha256(f"{text}\x00{voice_id}".encode()).hexdigest()


def cache_path(text: str, voice_id: str) -> Path:
    return AUDIO_CACHE / f"{cache_key(text, voice_id)}.mp3"


def cache_url(text: str, voice_id: str) -> str | None:
    p = cache_path(text, voice_id)
    return f"/audio/{p.name}" if p.exists() else None


def estimate_ms(text: str) -> int:
    return max(600, int(len(text) * 62))


async def stt(audio_bytes: bytes, mime: str = "audio/webm") -> str | None:
    key = api_key()
    if not key or not audio_bytes:
        return None
    try:
        async with httpx.AsyncClient(timeout=STT_TIMEOUT) as client:
            r = await client.post(
                f"{ELEVEN}/v1/speech-to-text",
                headers={"xi-api-key": key},
                data={"model_id": STT_MODEL},
                files={"file": ("utterance", audio_bytes, mime)},
            )
            if r.status_code != 200:
                print(f"stt: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
                return None
            text = (r.json().get("text") or "").strip()
            return text or None
    except Exception as exc:
        print(f"stt: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


async def tts_stream(text: str, voice_id: str) -> AsyncIterator[bytes]:
    """Stream mp3 chunks from ElevenLabs Flash as they arrive; cache the full file when done."""
    key = api_key()
    if not key:
        raise RuntimeError("tts: no ElevenLabs API key")
    AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
    path = cache_path(text, voice_id)
    tmp = path.with_suffix(".part")
    async with httpx.AsyncClient(timeout=TTS_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{ELEVEN}/v1/text-to-speech/{voice_id}/stream",
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
            json={"text": text, "model_id": TTS_MODEL},
        ) as r:
            if r.status_code != 200:
                body = await r.aread()
                raise RuntimeError(f"tts: HTTP {r.status_code} {body[:200]!r}")
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    if chunk:
                        f.write(chunk)
                        yield chunk
    tmp.replace(path)


class SpeechJob:
    """One utterance being spoken. Consumers read chunks while the producer is still streaming."""

    def __init__(self, text: str, voice_id: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.text = text
        self.voice_id = voice_id
        self.chunks: list[bytes] = []
        self.done = asyncio.Event()
        self.error: str | None = None
        self.source = "live"
        self._wake = asyncio.Event()

    @property
    def bytes_total(self) -> int:
        return sum(len(c) for c in self.chunks)

    @property
    def duration_ms(self) -> int:
        return int(self.bytes_total * 8 * 1000 / MP3_BPS) if self.chunks else estimate_ms(self.text)

    @property
    def url(self) -> str:
        return f"/voice/stream/{self.id}"

    async def produce(self) -> None:
        try:
            async for chunk in tts_stream(self.text, self.voice_id):
                self.chunks.append(chunk)
                self._wake.set()
        except Exception as exc:
            cached = cache_path(self.text, self.voice_id)
            if cached.exists():
                self.chunks = [cached.read_bytes()]
                self.source = "cache"
            else:
                self.error = f"{type(exc).__name__}: {exc}"
                print(f"tts: {self.error}", file=sys.stderr)
        finally:
            self.done.set()
            self._wake.set()

    async def iter(self) -> AsyncIterator[bytes]:
        i = 0
        while True:
            while i < len(self.chunks):
                yield self.chunks[i]
                i += 1
            if self.done.is_set():
                return
            self._wake.clear()
            await self._wake.wait()


JOBS: dict[str, SpeechJob] = {}


async def speak(text: str, voice_id: str) -> SpeechJob:
    """Start speaking. Returns immediately; audio streams at job.url. Cache is the fallback, never the first choice."""
    job = SpeechJob(text, voice_id)
    JOBS[job.id] = job
    if len(JOBS) > 64:
        for k in list(JOBS)[:-32]:
            JOBS.pop(k, None)
    if not api_key():
        cached = cache_path(text, voice_id)
        if cached.exists():
            job.chunks = [cached.read_bytes()]
            job.source = "cache"
        else:
            job.error = "no ElevenLabs API key and no cached audio"
        job.done.set()
        return job
    asyncio.create_task(job.produce())
    return job


async def check_voices() -> list[str]:
    """Verify every configured voice id against the ElevenLabs account. Returns the ids that failed."""
    key = api_key()
    if not key:
        raise RuntimeError("voices: no ElevenLabs API key")
    bad: list[str] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for name, vid in [("desk", VOICES.desk), *VOICES.agents.items()]:
            r = await client.get(f"{ELEVEN}/v1/voices/{vid}", headers={"xi-api-key": key})
            if r.status_code != 200:
                bad.append(f"{name}={vid} ({r.status_code})")
    return bad


async def prerender(items: list[tuple[str, str]]) -> list[Path]:
    """Fallback cache only. items = [(text, voice_id)]."""
    out: list[Path] = []
    for text, vid in items:
        p = cache_path(text, vid)
        if not p.exists():
            async for _ in tts_stream(text, vid):
                pass
        out.append(p)
    return out


if __name__ == "__main__":
    from mandate.runner import load_env

    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        bad = asyncio.run(check_voices())
        print("voices ok" if not bad else "voices FAILED: " + ", ".join(bad))
        raise SystemExit(1 if bad else 0)
    if cmd == "prerender":
        from mandate.desk import fallback_items

        paths = asyncio.run(prerender(fallback_items()))
        for p in paths:
            print(p)
