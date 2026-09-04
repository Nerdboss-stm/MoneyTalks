from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mandate import boundary
from mandate import ledger as L
from mandate.schemas import Mandate, Payment, Stamp


class Exposure(BaseModel):
    mandate_id: str
    at: datetime
    cleared: list[str] = Field(default_factory=list)
    held: list[str] = Field(default_factory=list)
    escalated: list[str] = Field(default_factory=list)
    already_executed: list[dict] = Field(default_factory=list)
    executing: list[str] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "cleared": len(self.cleared),
            "held": len(self.held),
            "escalated": len(self.escalated),
            "already_executed": len(self.already_executed),
            "executing": len(self.executing),
        }


def classify(fleet: Any, m: Mandate, now: datetime) -> Exposure:
    ex = Exposure(mandate_id=m.id, at=now)
    queued = {pid for a in fleet.agents.values() for _, pid in a.queue}
    agent_ids = set(fleet.agents)
    vendors = {p.vendor for p in fleet.ledger.payments.values()}
    for p in fleet.ledger.payments.values():
        applies = boundary.scope_applies(m, p, agent_ids, vendors)
        if applies is False:
            ex.cleared.append(p.id)
            continue
        in_window = m.window_start <= p.scheduled_at < m.window_end
        over = p.amount_cents > m.threshold_cents
        excepted = p.id in fleet.exempt_ids and p.agent_id in m.exceptions
        if p.status == "executed":
            if p.executed_at and m.bound_at and p.executed_at > m.bound_at and in_window and over and not excepted:
                ex.already_executed.append(
                    {
                        "payment_id": p.id,
                        "agent_id": p.agent_id,
                        "vendor": p.vendor,
                        "amount_cents": p.amount_cents,
                        "executed_at": p.executed_at.isoformat(),
                        "seconds_since_bound": (p.executed_at - m.bound_at).total_seconds(),
                        "seconds_since_order": (p.executed_at - m.window_start).total_seconds(),
                    }
                )
            else:
                ex.cleared.append(p.id)
        elif p.status == "held":
            ex.held.append(p.id)
        elif p.status == "escalated":
            ex.escalated.append(p.id)
        elif p.status == "executing" or p.id in queued:
            ex.executing.append(p.id)
        elif not in_window or excepted:
            ex.cleared.append(p.id)
        elif not over:
            if m.threshold_cents > 0 and p.amount_cents > int(m.threshold_cents * (1 - boundary.BAND)):
                ex.escalated.append(p.id)
            else:
                ex.cleared.append(p.id)
        elif applies is None or (p.agent_id in m.exceptions and p.id not in fleet.exempt_ids):
            ex.escalated.append(p.id)
        else:
            ex.held.append(p.id)
    return ex


async def publish_exposure(fleet: Any, ex: Exposure) -> None:
    await fleet.publish(
        "exposure.report",
        mandate_id=ex.mandate_id,
        counts=ex.counts(),
        held=ex.held,
        escalated=ex.escalated,
        executing=ex.executing,
        already_executed=ex.already_executed,
        advisory=fleet.run_version == "v1",
    )


async def act(fleet: Any, m: Mandate, ex: Exposure, now: datetime) -> None:
    """v2 only: reach into plans. Payments already in an execution queue are in flight and left to the boundary."""
    for pid in ex.held:
        p = fleet.ledger.payments[pid]
        if p.status != "planned":
            continue
        stamp = Stamp(mandate_id=m.id, checked_at=now, decision="hold", reason=f"propagation: {p.amount_cents} > {m.threshold_cents} inside window", transcript_excerpt=m.transcript[:120])
        fleet.ledger = L.hold(fleet.ledger, pid, now, stamp)
        agent = fleet.agents[p.agent_id]
        agent.plans.pop(pid, None)
        agent.record_stamp(now, pid, stamp)
        await fleet.publish("payment.held", agent_id=p.agent_id, payment_id=pid, reason=stamp.reason, source="propagation", mandate_id=m.id, amount_cents=p.amount_cents, vendor=p.vendor)
    for pid in ex.escalated:
        p = fleet.ledger.payments[pid]
        if p.status != "planned":
            continue
        v = boundary.check(p, [m], p.scheduled_at, fleet.exempt_ids, set(fleet.agents), {q.vendor for q in fleet.ledger.payments.values()})
        reason = v.reason if v.decision == "escalate" else "propagation: partial exception match"
        stamp = Stamp(mandate_id=m.id, checked_at=now, decision="escalate", reason=reason, transcript_excerpt=m.transcript[:120])
        fleet.ledger = L.escalate(fleet.ledger, pid, now, stamp)
        agent = fleet.agents[p.agent_id]
        agent.plans.pop(pid, None)
        agent.record_stamp(now, pid, stamp)
        bundle = boundary.EvidenceBundle(payment=fleet.ledger.payments[pid], mandate=m, reason=reason, rationale=agent.last_rationale(), recommendation=boundary.recommendation(v))
        await fleet.publish("evidence.bundle", agent_id=p.agent_id, payment_id=pid, source="propagation", **bundle.model_dump(mode="json"))


async def on_bound(fleet: Any, m: Mandate, now: datetime) -> Exposure:
    ex = classify(fleet, m, now)
    if fleet.run_version == "v2":
        await act(fleet, m, ex, now)
        ex = classify(fleet, m, now)
    await publish_exposure(fleet, ex)
    fleet.last_exposure[m.id] = {"held": sorted(ex.held), "escalated": sorted(ex.escalated), "already": sorted(e["payment_id"] for e in ex.already_executed)}
    return ex


async def sweep(fleet: Any, now: datetime) -> None:
    for m in fleet.active_mandates(now):
        ex = classify(fleet, m, now)
        sig = {"held": sorted(ex.held), "escalated": sorted(ex.escalated), "already": sorted(e["payment_id"] for e in ex.already_executed)}
        if sig != fleet.last_exposure.get(m.id):
            fleet.last_exposure[m.id] = sig
            await publish_exposure(fleet, ex)


def held_by_amount(ledger: L.Ledger) -> list[Payment]:
    return sorted((p for p in ledger.payments.values() if p.status in ("held", "escalated")), key=lambda p: (-p.amount_cents, p.id))


async def release_held(fleet: Any, m: Mandate, now: datetime) -> list[str]:
    released: list[str] = []
    for p in held_by_amount(fleet.ledger):
        stamp = Stamp(mandate_id=m.id, checked_at=now, decision="release", reason=f"{m.id} expired on {m.expires_on or 'window end'}", transcript_excerpt=m.transcript[:120])
        fleet.ledger = L.release(fleet.ledger, p.id, now, stamp)
        agent = fleet.agents[p.agent_id]
        agent.record_stamp(now, p.id, stamp)
        await fleet.publish("payment.released", agent_id=p.agent_id, payment_id=p.id, amount_cents=p.amount_cents, mandate_id=m.id, order=len(released), prior_status=p.status, vendor=p.vendor)
        released.append(p.id)
        agent.plans[p.id] = {"tick": None, "payment_id": p.id, "execute_at": now.isoformat(), "status_template": agent.status_template, "mandate_id": m.id, "released": True}
        await agent.fire(p.id, now)
    return released
