from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Rail = Literal["ACH", "WIRE", "CARD"]
PaymentStatus = Literal["planned", "executing", "executed", "held", "escalated"]
Decision = Literal["allow", "hold", "escalate", "release"]


class Stamp(BaseModel):
    mandate_id: str
    checked_at: datetime
    decision: Decision
    reason: str
    transcript_excerpt: str


class Payment(BaseModel):
    id: str
    agent_id: str
    vendor: str
    amount_cents: int = Field(ge=0)
    rail: Rail
    po_number: str
    settle_date: date
    scheduled_at: datetime
    executed_at: datetime | None = None
    status: PaymentStatus = "planned"
    stamp: Stamp | None = None


class Plan(BaseModel):
    agent_id: str
    payments: list[Payment] = Field(default_factory=list)


class Mandate(BaseModel):
    id: str
    owner: str
    scope: str
    threshold_cents: int = Field(ge=0)
    window_start: datetime
    window_end: datetime
    exceptions: list[str] = Field(default_factory=list)
    precedence: int = 0
    transcript: str
    compiled_text: str
    bound_at: datetime | None = None
    expires_on: str | None = None


class Event(BaseModel):
    type: str
    ts_wall: datetime
    ts_company: datetime
    payload: dict = Field(default_factory=dict)
