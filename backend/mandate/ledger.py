from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from mandate.scenario import Scenario
from mandate.schemas import Payment, Stamp


class Ledger(BaseModel):
    model_config = ConfigDict(frozen=True)

    cash_cents: int
    payments: dict[str, Payment]


def from_scenario(scenario: Scenario) -> Ledger:
    return Ledger(
        cash_cents=scenario.company.cash_cents,
        payments={p.id: p.model_copy(deep=True) for p in scenario.payments},
    )


def _update(ledger: Ledger, payment_id: str, allowed: tuple[str, ...], **changes) -> tuple[Ledger, Payment]:
    p = ledger.payments[payment_id]
    if p.status not in allowed:
        raise ValueError(f"{payment_id} is {p.status}; expected one of {allowed}")
    np = p.model_copy(update=changes)
    payments = dict(ledger.payments)
    payments[payment_id] = np
    return Ledger(cash_cents=ledger.cash_cents, payments=payments), np


def begin(ledger: Ledger, payment_id: str) -> Ledger:
    new, _ = _update(ledger, payment_id, ("planned",), status="executing")
    return new


def execute(ledger: Ledger, payment_id: str, at: datetime, stamp: Stamp | None = None) -> Ledger:
    new, p = _update(
        ledger, payment_id, ("planned", "executing"), status="executed", executed_at=at, stamp=stamp
    )
    return Ledger(cash_cents=new.cash_cents - p.amount_cents, payments=new.payments)


def hold(ledger: Ledger, payment_id: str, at: datetime, stamp: Stamp | None = None) -> Ledger:
    new, _ = _update(ledger, payment_id, ("planned", "executing", "escalated"), status="held", stamp=stamp)
    return new


def escalate(ledger: Ledger, payment_id: str, at: datetime, stamp: Stamp | None = None) -> Ledger:
    new, _ = _update(ledger, payment_id, ("planned", "executing", "held"), status="escalated", stamp=stamp)
    return new


def release(ledger: Ledger, payment_id: str, at: datetime, stamp: Stamp | None = None) -> Ledger:
    new, _ = _update(ledger, payment_id, ("held", "escalated"), status="planned", stamp=stamp)
    return new


def balance(ledger: Ledger) -> int:
    return ledger.cash_cents


def committed_before(ledger: Ledger, at: datetime, exclude: frozenset[str] = frozenset()) -> int:
    return sum(
        p.amount_cents
        for p in ledger.payments.values()
        if p.status in ("planned", "executing") and p.scheduled_at < at and p.id not in exclude
    )


def payroll_at_risk(
    ledger: Ledger, payroll_cents: int, payroll_due: datetime, exclude: frozenset[str] = frozenset()
) -> bool:
    return balance(ledger) - committed_before(ledger, payroll_due, exclude) < payroll_cents
