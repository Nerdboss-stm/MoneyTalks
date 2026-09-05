import os
import re

import pytest

os.environ.setdefault("MANDATE_DECIDER", "rules")
os.environ.setdefault("PRISM_HANDLERS", "off")
os.environ["ELEVENLABS_API_KEY"] = ""
os.environ["elevenlabs_api_key"] = ""

from mandate import desk as D  # noqa: E402
from mandate.scenario import ROLES  # noqa: E402

QUESTIONS = [
    ("Treasury, what's your cash position?", "treasury"),
    ("AP East, anything due today?", "ap_east"),
    ("AP West, did you comply with my order?", "ap_west"),
    ("Procurement: why is Vantage held?", "procurement"),
    ("Payroll, are we on time Friday?", "payroll"),
    ("Tax, what did you pay Monday?", "tax"),
    ("Renewals, what's coming up?", "saas_renewals"),
    ("Expenses what's your week", "expenses"),
    ("Controller A, any escalations?", "controller_a"),
    ("Controler B, what's held?", "controller_b"),
    ("Collections, what's in flight?", "collections"),
    ("F X, what settles Friday?", "fx"),
]


async def test_name_prefixed_questions_route_to_agent():
    assert len(QUESTIONS) == 12
    for text, agent in QUESTIONS:
        intent = await D.classify(text, D.rules_classify_async)
        assert intent.kind == "AGENT_QUERY" and intent.agent_id == agent, text
        assert intent.question and intent.agent_id not in intent.question.lower().replace(" ", "_")


async def test_answer_as_never_invents_amounts_or_times():
    rec = D.record_from_recording("mandate-v1-01", "ap_west", "10:04:00")

    async def liar(record, question):
        return "I paid $99,000 at 11:11 and everything is fine."

    assert await D.answer_as(rec, "did you comply?", liar) == D.NO_RECORD

    async def honest(record, question):
        return "At 10:03 I executed Halden Logistics $51,000; compliant with active mandates."

    out = await D.answer_as(rec, "did you comply?", honest)
    assert out.startswith("At 10:03 I executed Halden Logistics $51,000")
    rules = await D.answer_as(rec, "did you comply with my order?", D.rules_answer_async)
    amounts, times = D.allowed_tokens(rec)
    assert all(a in amounts for a in re.findall(r"\$[\d,]+", rules))
    assert all(t.zfill(5) in times for t in re.findall(r"\b(\d{1,2}:\d{2})", rules))
    assert "compliant" in rules


async def test_stt_failure_is_noise(monkeypatch):
    from mandate import voice as V
    from mandate.agents import Fleet, RuleDecider
    from mandate.bus import EventBus
    from mandate.scenario import CompanyClock, build_scenario, ct

    async def dead_stt(audio_bytes, mime="audio/webm"):
        return None

    monkeypatch.setattr(V, "stt", dead_stt)
    assert await V.stt(b"garbage") is None
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 10:04")), bus, decider=RuleDecider())
    desk = D.Desk(fleet)
    out = await desk.voice_turn(await V.stt(b"garbage"), intent_engine=D.rules_classify_async, answer_engine=D.rules_answer_async)
    assert out["intent"] == "NOISE" and out["answer"] == D.NOISE_TEXT
    assert any(e.type == "desk.answer" and e.payload["text"] == D.NOISE_TEXT for e in events)


def test_voice_text_week_mentions_halden():
    from fastapi.testclient import TestClient

    import app as application

    with TestClient(application.app) as client:
        r = client.post("/voice/text", json={"text": "AP West, what's your week?", "session": "mandate-v1-01", "as_of": "10:01:00"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["intent"] == "AGENT_QUERY" and body["agent_id"] == "ap_west"
        assert "Halden Logistics" in body["answer"] and "$51,000" in body["answer"] and "10:03" in body["answer"]
        assert body["audio_url"] is None or body["audio_url"].startswith("/audio/")


def test_voices_config_complete():
    from mandate import voice as V

    assert set(V.VOICES.agents) == {r for r, _ in ROLES}
    assert V.VOICES.desk
    with pytest.raises(RuntimeError):
        V.load_voices(V.ROOT / "config" / "stage.json")
