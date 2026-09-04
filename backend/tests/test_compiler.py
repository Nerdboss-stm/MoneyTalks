import json
from pathlib import Path

from mandate import compiler
from mandate.compiler import CANONICAL_MANDATE, CANONICAL_ORDER, CompanyState, compile, conflicts, rules_engine
from mandate.scenario import ct, fmt

PHRASINGS = json.loads((Path(__file__).parent / "phrasings.json").read_text())
STATE = CompanyState(now=ct("Mon 10:02:07"), payroll_due=ct("Fri 17:00"), tax_due=ct("Mon 17:00"))


def test_phrasings_count():
    assert len(PHRASINGS) == 20


def test_phrasings():
    for case in PHRASINGS:
        m, readback, questions = compile(case["text"], STATE, engine=rules_engine)
        assert m.scope == case["scope"], case["text"]
        assert m.threshold_cents == case["threshold_cents"], case["text"]
        assert fmt(m.window_end) == case["window_end"], case["text"]
        assert m.expires_on == case["expires_on"], case["text"]
        assert m.exceptions == case["exceptions"], case["text"]
        assert m.precedence == case["precedence"], case["text"]
        assert bool(questions) == case["question"], (case["text"], questions)
        for a in case["assumptions"]:
            assert a in readback, (case["text"], readback)
        if not case["question"]:
            assert readback.endswith("Confirm?"), readback
            assert readback.count(". ") <= 2
        else:
            assert readback.endswith("?")
        assert m.transcript == case["text"]


def test_canonical_matches_sentence():
    m, readback, questions = compile(CANONICAL_ORDER, STATE, engine=rules_engine)
    assert not questions
    for f in ("scope", "threshold_cents", "window_end", "exceptions", "precedence", "expires_on"):
        assert getattr(m, f) == getattr(CANONICAL_MANDATE, f)
    assert CANONICAL_MANDATE.id == "m-canonical"
    assert readback == compiler.CANONICAL_READBACK


def test_fallback_on_engine_error():
    def broken(text, state):
        raise RuntimeError("api down")

    m, readback, questions = compile("Hold anything over 50k", STATE, engine=broken)
    assert m.threshold_cents == CANONICAL_MANDATE.threshold_cents and m.id == "m-canonical"
    assert readback.endswith("Confirm?") and not questions


def test_conflicts():
    a, _, _ = compile("Hold anything over 50k until Friday.", STATE, engine=rules_engine)
    b, _, _ = compile("Hold anything over 100k until Friday.", STATE, engine=rules_engine)
    c, _, _ = compile("AP West, hold anything over 100k until Friday.", STATE, engine=rules_engine)
    assert conflicts(a, b) is not None and conflicts(a, b).endswith(".")
    assert conflicts(a, a.model_copy()) is None
    assert conflicts(c, compile("Treasury, hold anything above 250k until Friday.", STATE, engine=rules_engine)[0]) is None
