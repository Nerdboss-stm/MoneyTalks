from __future__ import annotations

import asyncio
import json
import random
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mandate.schemas import Payment, Plan, Rail

MANDATE_SEED = 7
BASE_MONDAY = date(2026, 9, 7)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]
THRESHOLD_CENTS = 5_000_000
TICK = timedelta(minutes=15)
DAY_OPEN = time(9, 0)
DAY_CLOSE = time(19, 0)
RAIL_LAG_DAYS: dict[str, int] = {"WIRE": 0, "CARD": 1, "ACH": 2}

ROLES: list[tuple[str, str]] = [
    ("treasury", "Treasury"),
    ("ap_east", "AP East"),
    ("ap_west", "AP West"),
    ("procurement", "Procurement"),
    ("payroll", "Payroll"),
    ("tax", "Tax"),
    ("saas_renewals", "Renewals"),
    ("expenses", "Expenses"),
    ("controller_a", "Controller A"),
    ("controller_b", "Controller B"),
    ("collections", "Collections"),
    ("fx", "FX"),
]

VENDORS: dict[str, list[str]] = {
    "treasury": ["First Harbor Bank Fees", "Cobalt Treasury Services", "Meridian Custody Fee"],
    "ap_east": ["Eastfield Packaging", "Brightline Courier", "Pinecrest Office Supply", "Northwind Freight"],
    "ap_west": ["Sierra Pallet Co", "Westbay Janitorial", "Cascade Print Works", "Redwood Fleet Fuel"],
    "procurement": ["Orbital Components", "Keystone Fasteners", "Delta Tooling", "Summit Raw Materials"],
    "payroll": ["Benefits Trust Remit", "401k Custodian Sweep", "Garnishment Clearing"],
    "tax": ["County Assessor", "Franchise Tax Board", "Sales Tax Remit"],
    "saas_renewals": ["Lumen Analytics", "Figment Design", "Relay CRM", "Vaultspace Storage"],
    "expenses": ["Corporate Card Settlement", "Travel Desk Remit", "Expense Reimbursement Batch"],
    "controller_a": ["Audit Partners LLP", "Bond Insurance Premium", "Ledger Archive Storage"],
    "controller_b": ["Ledgerline Advisory", "Registrar Filing Fee", "Notary Services Inc"],
    "collections": ["Chargeback Reserve", "Collections Agency Fee", "Dispute Recovery LLC"],
    "fx": ["Euro Hedge Margin", "Yen Forward Settle", "GBP Spot Settle"],
}


def ct(s: str | datetime) -> datetime:
    if isinstance(s, datetime):
        return s
    parts = s.strip().split()
    day = 0
    if len(parts) == 2:
        day = DAY_NAMES.index(parts[0][:3].title())
        parts = parts[1:]
    hms = [int(x) for x in parts[0].split(":")]
    while len(hms) < 3:
        hms.append(0)
    return datetime.combine(BASE_MONDAY + timedelta(days=day), time(*hms))


def fmt(t: datetime) -> str:
    return f"{DAY_NAMES[(t.date() - BASE_MONDAY).days]} {t:%H:%M:%S}"


def company_day_id(t: datetime) -> str:
    return t.date().isoformat()


def business_days_after(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def next_tick_after(t: datetime) -> datetime:
    day = t.date()
    if t.time() < DAY_OPEN:
        return datetime.combine(day, DAY_OPEN)
    floor = t.replace(second=0, microsecond=0)
    floor = floor.replace(minute=(floor.minute // 15) * 15)
    nxt = floor + TICK if floor <= t else floor
    if nxt.time() > DAY_CLOSE:
        return datetime.combine(day + timedelta(days=1), DAY_OPEN)
    return nxt


class Obligation(BaseModel):
    name: str
    agent_id: str
    amount_cents: int
    due: datetime
    exception_eligible: bool = True


class Company(BaseModel):
    cash_cents: int
    payroll: Obligation
    tax: Obligation


class AgentSpec(BaseModel):
    id: str
    display: str


class Scenario(BaseModel):
    seed: int
    company: Company
    agents: list[AgentSpec]
    payments: list[Payment]
    threshold_cents: int = THRESHOLD_CENTS
    window_start: datetime
    window_end: datetime
    exception_eligible: list[str] = Field(default_factory=list)

    def plans(self) -> dict[str, Plan]:
        out = {a.id: Plan(agent_id=a.id) for a in self.agents}
        for p in self.payments:
            out[p.agent_id].payments.append(p)
        return out

    def by_id(self) -> dict[str, Payment]:
        return {p.id: p for p in self.payments}

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _payment(
    agent_id: str, vendor: str, amount: int, rail: Rail, po: str, at: datetime
) -> Payment:
    return Payment(
        id="",
        agent_id=agent_id,
        vendor=vendor,
        amount_cents=amount,
        rail=rail,
        po_number=po,
        settle_date=business_days_after(at.date(), RAIL_LAG_DAYS[rail]),
        scheduled_at=at,
    )


def build_scenario(seed: int = MANDATE_SEED) -> Scenario:
    rng = random.Random(seed)
    used_po: set[str] = {"4471"}

    def po() -> str:
        while True:
            n = f"{rng.randint(1000, 9999)}"
            if n not in used_po:
                used_po.add(n)
                return f"PO-{n}"

    fixed = [
        _payment("ap_west", "Halden Logistics", 5_100_000, "ACH", "PO-4471", ct("Mon 10:03:14")),
        _payment("tax", "State Revenue Dept", 8_600_000, "WIRE", po(), ct("Mon 15:30:00")),
        _payment("procurement", "Vantage Steel Supply", 6_400_000, "WIRE", po(), ct("Tue 11:00:00")),
        _payment("payroll", "ADP Payroll Run W37", 41_200_000, "WIRE", po(), ct("Fri 16:30:00")),
        _payment("treasury", "Meridian Capital Sweep", 12_500_000, "WIRE", po(), ct("Fri 17:20:00")),
        _payment("fx", "Nordbank FX Settlement", 18_000_000, "WIRE", po(), ct("Fri 17:45:00")),
        _payment("saas_renewals", "Atlas Cloud Annual", 5_900_000, "ACH", po(), ct("Fri 18:30:00")),
    ]

    small: list[Payment] = []
    roles = [r for r, _ in ROLES]
    for i in range(40 - len(fixed)):
        agent_id = roles[i % len(roles)]
        vendor = rng.choice(VENDORS[agent_id])
        amount = rng.randrange(200_000, 4_990_000, 1_000)
        rail: Rail = rng.choices(["ACH", "WIRE", "CARD"], weights=[5, 3, 2])[0]
        day = rng.randrange(0, 5)
        minute = rng.randrange(9 * 60 + 5, 18 * 60 + 50)
        second = rng.randrange(0, 60)
        at = datetime.combine(BASE_MONDAY + timedelta(days=day), time(minute // 60, minute % 60, second))
        small.append(_payment(agent_id, vendor, amount, rail, po(), at))

    payments = sorted(fixed + small, key=lambda p: (p.scheduled_at, p.agent_id, p.vendor))
    for i, p in enumerate(payments, start=1):
        p.id = f"p-{i:03d}"

    by_vendor = {p.vendor: p for p in payments}
    company = Company(
        cash_cents=124_000_000,
        payroll=Obligation(name="payroll", agent_id="payroll", amount_cents=41_200_000, due=ct("Fri 17:00:00")),
        tax=Obligation(name="tax", agent_id="tax", amount_cents=8_600_000, due=ct("Mon 17:00:00")),
    )
    return Scenario(
        seed=seed,
        company=company,
        agents=[AgentSpec(id=r, display=d) for r, d in ROLES],
        payments=payments,
        window_start=ct("Mon 10:02:07"),
        window_end=ct("Fri 17:00:00"),
        exception_eligible=[by_vendor["ADP Payroll Run W37"].id, by_vendor["State Revenue Dept"].id],
    )


def export_scenario(path: Path, seed: int = MANDATE_SEED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_scenario(seed).to_json(), encoding="utf-8")
    return path


ClockMode = Literal["stepped", "realtime", "speed"]


class CompanyClock:
    def __init__(self, start: datetime, mode: ClockMode = "stepped", speed: float = 1.0) -> None:
        self.mode = mode
        self.speed = 1.0 if mode == "realtime" else speed
        self._start = start
        self._now = start
        self._t0 = _time.monotonic()

    def now(self) -> datetime:
        if self.mode == "stepped":
            return self._now
        return self._start + timedelta(seconds=(_time.monotonic() - self._t0) * self.speed)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self.now() + delta
        return self._now

    async def sleep_until(self, t: datetime) -> datetime:
        if self.mode == "stepped":
            if t > self._now:
                self._now = t
            return self._now
        remaining = (t - self.now()).total_seconds() / self.speed
        if remaining > 0:
            await asyncio.sleep(remaining)
        return self.now()


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[2] / "shared" / "fixtures" / "scenario_seed7.json"
    print(export_scenario(out))
