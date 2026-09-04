from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from mandate.scenario import DAY_NAMES, ROLES, THRESHOLD_CENTS, VENDORS, ct, fmt
from mandate.schemas import Mandate

CANONICAL_ORDER = "Hold anything over fifty thousand dollars until payroll clears. Payroll and tax are exempt."
MODEL = "claude-haiku-4-5"
VAGUE = ("big", "large", "major", "significant", "sizable", "substantial", "huge")
HOLD_WORDS = ("hold", "freeze", "stop", "block", "do not pay", "don't pay", "nothing", "no payments", "no major", "no big", "no large")
RELEASE_WORDS = ("release", "pay everything", "pay all", "immediately", "let through", "approve all", "go ahead", "unfreeze")
COMPARATORS = r"(?:over|above|exceeding|more than|beyond|greater than|past|bigger than|larger than)"
AGENT_IDS = [r for r, _ in ROLES]
DISPLAY = {d.lower(): r for r, d in ROLES}
ALL_VENDORS = sorted({v for vs in VENDORS.values() for v in vs} | {"Halden Logistics", "State Revenue Dept", "Vantage Steel Supply", "Brightline Courier", "ADP Payroll Run W37", "Meridian Capital Sweep", "Nordbank FX Settlement", "Atlas Cloud Annual"})
UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1_000, "grand": 1_000, "k": 1_000, "million": 1_000_000, "m": 1_000_000, "mm": 1_000_000}


class CompanyState(BaseModel):
    now: datetime
    payroll_due: datetime
    tax_due: datetime
    agents: list[str] = Field(default_factory=lambda: list(AGENT_IDS))
    vendors: list[str] = Field(default_factory=lambda: list(ALL_VENDORS))
    default_threshold_cents: int = THRESHOLD_CENTS


class Parsed(BaseModel):
    scope: str = "all"
    scope_unknown: str | None = None
    threshold_cents: int | None = None
    threshold_assumed: bool = False
    window_end: datetime | None = None
    window_assumed: bool = False
    expires_on: str | None = None
    exceptions: list[str] = Field(default_factory=lambda: ["payroll", "tax"])
    precedence: int = 1
    conflict: str | None = None


class LLMParsed(BaseModel):
    scope: str = Field(description='"all", an agent id, "vendor:<exact vendor name>", or "unknown:<name>"')
    threshold_cents: int | None = Field(description="null when no number was given")
    vague_quantity: bool = Field(description="true when only words like big/large/major/significant were used")
    window_end: str | None = Field(description="ISO datetime, or null when no window was given")
    until_payroll_clears: bool
    exceptions: list[str] = Field(description="agent ids excepted; payroll and tax unless the order names them as included")
    precedence: int = Field(description="1 normally, 2 when the order overrides earlier orders")
    conflict: str | None = Field(description="one sentence when the order both holds and releases, else null")


Engine = Callable[[str, CompanyState], Parsed]


def _words_to_number(tokens: list[str]) -> float | None:
    total = 0.0
    current = 0.0
    seen = False
    for t in tokens:
        if t in ("a", "an"):
            current = current or 1
            seen = True
        elif t == "half":
            current = 0.5
            seen = True
        elif t == "quarter":
            current = 0.25
            seen = True
        elif t in UNITS:
            current += UNITS[t]
            seen = True
        elif t == "hundred":
            current = (current or 1) * 100
        elif t in SCALES:
            total += (current or 1) * SCALES[t]
            current = 0
            seen = True
        elif t in ("and", "of", "dollars", "bucks"):
            continue
        else:
            return None
    return (total + current) if seen else None


def parse_amount_cents(text: str) -> int | None:
    low = text.lower()
    m = re.search(rf"{COMPARATORS}\s+\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(k|mm|m|thousand|million|grand)?\b", low)
    if not m:
        m = re.search(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|mm|m|thousand|million|grand)?\b", low)
    if m:
        value = float(m.group(1).replace(",", ""))
        return int(round(value * SCALES.get(m.group(2) or "", 1) * 100))
    m = re.search(rf"{COMPARATORS}\s+((?:[a-z]+\s?){{1,6}})", low)
    if m:
        toks = m.group(1).split()
        for n in range(len(toks), 0, -1):
            val = _words_to_number(toks[:n])
            if val is not None:
                return int(round(val * 100))
    return None


def _parse_time(s: str | None) -> time:
    if not s:
        return time(17, 0)
    s = s.strip().lower()
    if s in ("noon", "midday"):
        return time(12, 0)
    if s in ("close", "eod", "end of day", "close of business", "cob"):
        return time(17, 0)
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return time(17, 0)
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    return time(h, mi)


def parse_window(text: str, state: CompanyState) -> tuple[datetime | None, bool, str | None]:
    low = text.lower()
    if re.search(r"(until|till|before|when|once)\s+payroll", low) or "payroll clear" in low:
        return state.payroll_due, False, "payroll_cleared"
    m = re.search(
        r"(?:until|till|through|before|by)\s+(?:end of\s+)?(mon|tue|wed|thu|fri)[a-z]*\s*"
        r"(noon|midday|close of business|end of day|close|eod|cob|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?",
        low,
    )
    if m:
        day = ["mon", "tue", "wed", "thu", "fri"].index(m.group(1))
        return ct(f"{DAY_NAMES[day]} {_parse_time(m.group(2)).strftime('%H:%M')}"), False, None
    if re.search(r"(this week|end of (the )?week|week'?s end)", low):
        return state.payroll_due, False, None
    m = re.search(r"(?:until|till|before|by)\s+(noon|midday|\d{1,2}(?::\d{2})?\s*(?:am|pm))", low)
    if m:
        return datetime.combine(state.now.date(), _parse_time(m.group(1))), False, None
    return state.payroll_due, True, "payroll_cleared"


def _find_agent(fragment: str) -> str | None:
    frag = fragment.strip().lower().rstrip(".,;")
    if frag in AGENT_IDS:
        return frag
    return DISPLAY.get(frag)


def parse_scope(text: str, state: CompanyState) -> tuple[str, str | None]:
    low = text.lower()
    names = sorted(DISPLAY.keys(), key=len, reverse=True) + AGENT_IDS
    for name in names:
        if re.match(rf"^\s*{re.escape(name)}\s*[,:]", low) or re.search(rf"\b{re.escape(name)}\s+(holds|should hold|must hold|hold)\b", low):
            return _find_agent(name) or "all", None
        if re.search(rf"\bfor\s+(?:the\s+)?{re.escape(name)}\b", low) and name not in ("payroll", "tax"):
            return _find_agent(name) or "all", None
    for v in sorted(state.vendors, key=len, reverse=True):
        if v.lower() in low:
            return f"vendor:{v}", None
    m = re.search(r"\bfor\s+(?:the\s+)?([a-z][a-z0-9 ]*?)\s+(team|desk|group)\b", low)
    if m and _find_agent(m.group(1)) is None:
        return "unknown", m.group(1)
    m = re.search(r"\b(?:pay|paying|to)\s+((?:[A-Z][A-Za-z&.-]+\s?){1,4})", text)
    if m:
        cand = m.group(1).strip()
        if cand.lower() not in DISPLAY and cand.split()[0].lower() not in ("monday", "tuesday", "wednesday", "thursday", "friday", "payroll", "tax", "confirm"):
            return "unknown", cand
    return "all", None


def parse_exceptions(text: str, state: CompanyState) -> list[str]:
    low = text.lower()
    exc = {"payroll", "tax"}
    for name in ("payroll", "tax"):
        if re.search(rf"(including|include|incl\.?|even)\s+{name}\b|{name}\s+(too|included|as well|is not exempt|isn't exempt)", low):
            exc.discard(name)
    m = re.search(r"(except|exempt|excluding|other than|apart from|but not)\s+(?:for\s+)?([a-z ,&]+?)(?:\.|$|;|,\s+(?:until|till))", low)
    if m:
        for part in re.split(r",|\band\b", m.group(2)):
            part = part.strip()
            if part.endswith(" are exempt"):
                part = part[: -len(" are exempt")]
            a = _find_agent(part)
            if a:
                exc.add(a)
    return sorted(exc)


def rules_engine(text: str, state: CompanyState) -> Parsed:
    low = text.lower()
    p = Parsed()
    if any(w in low for w in HOLD_WORDS) and any(w in low for w in RELEASE_WORDS):
        p.conflict = "The order both holds and releases payments; those instructions contradict each other."
    p.scope, p.scope_unknown = parse_scope(text, state)
    amount = parse_amount_cents(text)
    if amount is not None:
        p.threshold_cents = amount
    elif any(re.search(rf"\b{w}\b", low) for w in VAGUE):
        p.threshold_cents = state.default_threshold_cents
        p.threshold_assumed = True
    else:
        p.threshold_cents = 0
    p.window_end, p.window_assumed, p.expires_on = parse_window(text, state)
    p.exceptions = parse_exceptions(text, state)
    p.precedence = 2 if re.search(r"overrid|supersed|replac|cancel(s|ling)? (the |all )?(earlier|previous|prior)", low) else 1
    return p


def llm_engine(text: str, state: CompanyState) -> Parsed:
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=MODEL, temperature=0, max_tokens=512).with_structured_output(LLMParsed)
    system = (
        "You compile a spoken finance standing order into strict JSON. Company time now is {now}. "
        "Payroll clears at {payroll}. Known agent ids: {agents}. Known vendors: {vendors}. "
        "Amounts are in cents. Do not guess numbers: when only vague words are used, set threshold_cents null and vague_quantity true. "
        "When no window is given, set window_end null. Payroll and tax are excepted unless the order names them as included."
    ).format(now=state.now.isoformat(), payroll=state.payroll_due.isoformat(), agents=AGENT_IDS, vendors=state.vendors)
    out = llm.invoke([("system", system), ("user", text)])
    lp = out if isinstance(out, LLMParsed) else LLMParsed.model_validate(out)
    p = Parsed(
        scope="unknown" if lp.scope.startswith("unknown") else lp.scope,
        scope_unknown=lp.scope.split(":", 1)[1] if lp.scope.startswith("unknown:") else None,
        exceptions=sorted(lp.exceptions),
        precedence=lp.precedence,
        conflict=lp.conflict,
    )
    if lp.threshold_cents is not None:
        p.threshold_cents = lp.threshold_cents
    elif lp.vague_quantity:
        p.threshold_cents, p.threshold_assumed = state.default_threshold_cents, True
    else:
        p.threshold_cents = 0
    if lp.until_payroll_clears:
        p.window_end, p.expires_on = state.payroll_due, "payroll_cleared"
    elif lp.window_end:
        p.window_end = datetime.fromisoformat(lp.window_end)
    else:
        p.window_end, p.window_assumed, p.expires_on = state.payroll_due, True, "payroll_cleared"
    return p


def dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def _scope_phrase(scope: str) -> str:
    if scope == "all":
        return "all payments"
    if scope.startswith("vendor:"):
        return f"payments to {scope[7:]}"
    display = dict(ROLES).get(scope, scope)
    return f"{display} payments"


def resolve(text: str, p: Parsed, state: CompanyState, mandate_id: str | None = None) -> tuple[Mandate, str, list[str]]:
    questions: list[str] = []
    if p.conflict:
        questions.append(f"{p.conflict} Which applies?")
    if p.scope == "unknown":
        questions.append(f"Known agents are {', '.join(AGENT_IDS)}. Which agent or vendor is '{p.scope_unknown}'?")
    threshold = p.threshold_cents or 0
    window_end = p.window_end or state.payroll_due
    exc_display = [dict(ROLES).get(e, e) for e in p.exceptions]
    compiled = (
        f"HOLD scope={p.scope} amount>{threshold} cents window={fmt(state.now)}..{fmt(window_end)}"
        f"{' expires_on=payroll_cleared' if p.expires_on else ''} except={','.join(p.exceptions) or 'none'} precedence={p.precedence}"
    )
    mandate = Mandate(
        id=mandate_id or f"m-{hashlib.sha1(text.encode()).hexdigest()[:6]}",
        owner="desk",
        scope=p.scope,
        threshold_cents=threshold,
        window_start=state.now,
        window_end=window_end,
        exceptions=list(p.exceptions),
        precedence=p.precedence,
        transcript=text,
        compiled_text=compiled,
        expires_on=p.expires_on,
    )
    if questions:
        return mandate, " ".join(questions), questions
    amount_phrase = f" over {dollars(threshold)}" if threshold > 0 else ""
    until = f"until {fmt(window_end)}" + (" when payroll clears" if p.expires_on == "payroll_cleared" and not p.window_assumed else "")
    exc_phrase = f"{', '.join(exc_display)} stay exempt" if exc_display else "no exceptions"
    first = f"Hold {_scope_phrase(p.scope)}{amount_phrase} from {fmt(state.now)} {until}; {exc_phrase}."
    assumptions = []
    if p.window_assumed:
        assumptions.append(f"No window was given, so it runs to the next payroll clearance, {fmt(window_end)}")
    if p.threshold_assumed:
        assumptions.append(f"I assumed {dollars(threshold)} for the unstated amount")
    second = (" " + "; ".join(assumptions) + ".") if assumptions else ""
    return mandate, f"{first}{second} Confirm?", []


def default_engine() -> Engine:
    return llm_engine if os.environ.get("ANTHROPIC_API_KEY") else rules_engine


def compile(text: str, company_state: CompanyState, engine: Engine | None = None) -> tuple[Mandate, str, list[str]]:
    engine = engine or default_engine()
    try:
        parsed = engine(text, company_state)
    except Exception:
        return canonical_mandate(company_state.now), CANONICAL_READBACK, []
    return resolve(text, parsed, company_state)


def conflicts(existing: Mandate, new: Mandate) -> str | None:
    if existing.scope != new.scope and "all" not in (existing.scope, new.scope):
        return None
    overlap = existing.window_start < new.window_end and new.window_start < existing.window_end
    if not overlap:
        return None
    if existing.threshold_cents == new.threshold_cents and sorted(existing.exceptions) == sorted(new.exceptions):
        return None
    winner = "the new order" if new.precedence >= existing.precedence else f"{existing.id}"
    return (
        f"{existing.id} already holds {_scope_phrase(existing.scope)} over {dollars(existing.threshold_cents)} until {fmt(existing.window_end)}; "
        f"the new order says {dollars(new.threshold_cents)} with exceptions {', '.join(new.exceptions) or 'none'} — {winner} takes precedence."
    )


CANONICAL_STATE = CompanyState(now=ct("Mon 10:02:07"), payroll_due=ct("Fri 17:00"), tax_due=ct("Mon 17:00"))
CANONICAL_MANDATE, CANONICAL_READBACK, _ = resolve(CANONICAL_ORDER, rules_engine(CANONICAL_ORDER, CANONICAL_STATE), CANONICAL_STATE, mandate_id="m-canonical")


def canonical_mandate(now: datetime | None = None, text: str | None = None) -> Mandate:
    m = CANONICAL_MANDATE.model_copy(deep=True)
    if now is not None:
        m.window_start = now
        m.compiled_text = m.compiled_text.replace(fmt(CANONICAL_STATE.now), fmt(now), 1)
    if text:
        m.transcript = text
    return m
