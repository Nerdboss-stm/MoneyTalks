from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from mandate.schemas import Mandate, Payment, Stamp

BAND = 0.05
KNOWN_SCOPES = {"all"}


class Verdict(BaseModel):
    decision: Literal["allow", "hold", "escalate"]
    mandate_id: str
    reason: str
    excerpt: str


class EvidenceBundle(BaseModel):
    payment: Payment
    mandate: Mandate | None
    reason: str
    rationale: str
    recommendation: str


def _excerpt(m: Mandate) -> str:
    return m.transcript[:120]


def scope_applies(m: Mandate, p: Payment, agent_ids: set[str], vendors: set[str]) -> bool | None:
    """True/False when the scope is understood; None when it is ambiguous."""
    if m.scope == "all":
        return True
    if m.scope in agent_ids:
        return m.scope == p.agent_id
    if m.scope.startswith("vendor:"):
        name = m.scope[7:]
        if name in vendors:
            return name == p.vendor
        return None
    return None


def check_one(m: Mandate, p: Payment, now: datetime, exempt_ids: frozenset[str], agent_ids: set[str], vendors: set[str]) -> Verdict:
    applies = scope_applies(m, p, agent_ids, vendors)
    if applies is False:
        return Verdict(decision="allow", mandate_id=m.id, reason=f"{m.id} scope {m.scope} does not cover {p.agent_id}", excerpt=_excerpt(m))
    if p.id in exempt_ids and p.agent_id in m.exceptions:
        return Verdict(decision="allow", mandate_id=m.id, reason=f"{m.id} excepts {p.agent_id}", excerpt=_excerpt(m))
    if not (m.window_start <= now < m.window_end):
        return Verdict(decision="allow", mandate_id=m.id, reason=f"{fmt_(now)} is outside {m.id} window", excerpt=_excerpt(m))
    if p.amount_cents <= m.threshold_cents:
        if m.threshold_cents > 0 and p.amount_cents > int(m.threshold_cents * (1 - BAND)):
            return Verdict(decision="escalate", mandate_id=m.id, reason=f"{p.amount_cents} is within 5% below threshold {m.threshold_cents}", excerpt=_excerpt(m))
        return Verdict(decision="allow", mandate_id=m.id, reason=f"{p.amount_cents} <= threshold {m.threshold_cents}", excerpt=_excerpt(m))
    if applies is None:
        return Verdict(decision="escalate", mandate_id=m.id, reason=f"{m.id} scope {m.scope!r} is ambiguous for {p.vendor}", excerpt=_excerpt(m))
    if p.agent_id in m.exceptions and p.id not in exempt_ids:
        return Verdict(decision="escalate", mandate_id=m.id, reason=f"{p.agent_id} is excepted but {p.id} is not exception-eligible", excerpt=_excerpt(m))
    return Verdict(decision="hold", mandate_id=m.id, reason=f"{p.amount_cents} > threshold {m.threshold_cents} inside {m.id} window", excerpt=_excerpt(m))


def fmt_(t: datetime) -> str:
    return t.strftime("%a %H:%M:%S")


def check(
    payment: Payment,
    mandates: list[Mandate],
    now: datetime,
    exempt_ids: frozenset[str],
    agent_ids: set[str] | None = None,
    vendors: set[str] | None = None,
    fallback_excerpt: str = "",
) -> Verdict:
    try:
        agent_ids = agent_ids or set()
        vendors = vendors or set()
        verdicts = [check_one(m, payment, now, exempt_ids, agent_ids, vendors) for m in mandates if m.bound_at is not None]
        for wanted in ("hold", "escalate"):
            for v in verdicts:
                if v.decision == wanted:
                    return v
        if verdicts:
            return verdicts[0]
        return Verdict(decision="allow", mandate_id="none", reason="no active mandate", excerpt=fallback_excerpt)
    except Exception as exc:  # fail closed
        return Verdict(decision="hold", mandate_id="error", reason=f"boundary error: {type(exc).__name__}: {exc}", excerpt=fallback_excerpt)


def stamp_for(v: Verdict, at: datetime) -> Stamp:
    return Stamp(mandate_id=v.mandate_id, checked_at=at, decision=v.decision, reason=v.reason, transcript_excerpt=v.excerpt)


def recommendation(v: Verdict) -> str:
    if "within 5%" in v.reason:
        return "Confirm whether the threshold is a hard line; release if the human accepts the 5% band."
    if "ambiguous" in v.reason:
        return "Name the agent or vendor the order applies to, then release or hold."
    if "exception-eligible" in v.reason:
        return "Confirm whether this payment is part of the excepted obligation."
    return "Review the stamp and release or hold."
