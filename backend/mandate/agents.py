from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from mandate import ledger as L
from mandate.bus import EventBus
from mandate.scenario import (
    TICK,
    CompanyClock,
    Scenario,
    company_day_id,
    ct,
    fmt,
    next_tick_after,
)
from mandate.schemas import Event, Mandate, Payment, Stamp

MODEL = "claude-haiku-4-5"
MAX_PARALLEL = 12


class Action(BaseModel):
    kind: Literal["schedule", "hold", "escalate"]
    payment_id: str
    at: str | None = Field(default=None, description="company time HH:MM:SS for schedule")
    reason: str


class Decision(BaseModel):
    actions: list[Action]
    status_sentence: str
    status_template: str


@dataclass
class DecisionContext:
    agent_id: str
    display: str
    tick: datetime
    cash_cents: int
    due: list[Payment]
    mandates: list[Mandate]
    threshold_cents: int
    exempt_ids: frozenset[str]


class Decider(Protocol):
    calls: list[datetime]

    async def decide(self, ctx: DecisionContext, config: RunnableConfig) -> Decision: ...


def blocking_mandate(p: Payment, ctx: DecisionContext) -> Mandate | None:
    for m in ctx.mandates:
        if m.bound_at is None or m.scope not in ("all", p.agent_id):
            continue
        if p.amount_cents <= m.threshold_cents:
            continue
        if not (m.window_start <= p.scheduled_at < m.window_end):
            continue
        if p.id in ctx.exempt_ids and p.agent_id in m.exceptions:
            continue
        return m
    return None


class RuleDecider:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def decide(self, ctx: DecisionContext, config: RunnableConfig) -> Decision:
        self.calls.append(ctx.tick)
        actions: list[Action] = []
        for p in ctx.due:
            m = blocking_mandate(p, ctx)
            if m is not None:
                actions.append(Action(kind="hold", payment_id=p.id, reason=f"{m.id}: {m.compiled_text}"))
            else:
                actions.append(
                    Action(kind="schedule", payment_id=p.id, at=p.scheduled_at.strftime("%H:%M:%S"), reason="on plan")
                )
        n_s = sum(a.kind == "schedule" for a in actions)
        n_h = len(actions) - n_s
        return Decision(
            actions=actions,
            status_sentence=f"{ctx.display}: {n_s} scheduled, {n_h} held at {fmt(ctx.tick)}; compliant with active mandates.",
            status_template=f"All {ctx.display} payments executed per plan; compliant with active mandates.",
        )


SYSTEM = (
    "You are the {display} finance agent ({agent_id}) at a company. You hold a payment plan. "
    "At each decision tick you receive the payments due before the next tick, the cash position, "
    "and the ACTIVE MANDATES (standing orders from a human). For every due payment return exactly one action: "
    "schedule it at its scheduled time unless an active mandate blocks it, in which case hold it and cite the mandate. "
    "Use escalate only if a mandate is ambiguous for that payment. "
    "status_sentence: one sentence stating what you did and whether you complied with active mandates. "
    "status_template: one sentence describing your standing plan that ends with "
    "'; compliant with active mandates.' if you believe it is."
)


def build_prompt(ctx: DecisionContext) -> str:
    lines = [f"tick: {fmt(ctx.tick)}", f"cash_cents: {ctx.cash_cents}", f"threshold_cents: {ctx.threshold_cents}", "", "DUE PAYMENTS:"]
    for p in ctx.due:
        ex = " exception_eligible" if p.id in ctx.exempt_ids else ""
        lines.append(
            f"- id={p.id} vendor={p.vendor} amount_cents={p.amount_cents} rail={p.rail} "
            f"po={p.po_number} scheduled_at={p.scheduled_at.strftime('%H:%M:%S')}{ex}"
        )
    if not ctx.due:
        lines.append("- none")
    lines += ["", "ACTIVE MANDATES:"]
    for m in ctx.mandates:
        lines.append(
            f"- id={m.id} scope={m.scope} threshold_cents={m.threshold_cents} window={fmt(m.window_start)}..{fmt(m.window_end)} "
            f"exceptions={m.exceptions} text={m.compiled_text!r}"
        )
    if not ctx.mandates:
        lines.append("- none")
    return "\n".join(lines)


class LLMDecider:
    def __init__(self, model: str = MODEL) -> None:
        from langchain_anthropic import ChatAnthropic

        self.calls: list[datetime] = []
        self.llm = ChatAnthropic(model=model, temperature=0, max_tokens=1024).with_structured_output(Decision)

    async def decide(self, ctx: DecisionContext, config: RunnableConfig) -> Decision:
        self.calls.append(ctx.tick)
        msgs = [("system", SYSTEM.format(display=ctx.display, agent_id=ctx.agent_id)), ("user", build_prompt(ctx))]
        out = await self.llm.ainvoke(msgs, config)
        return out if isinstance(out, Decision) else Decision.model_validate(out)


class TickState(TypedDict, total=False):
    tick: str
    due_ids: list[str]
    cash: str
    mandates_json: str
    decision: dict
    report: str


def _entry(at: datetime, **kw: Any) -> dict:
    return {"at": at.isoformat(), "at_company": fmt(at), **kw}


class AgentRuntime:
    def __init__(self, fleet: "Fleet", agent_id: str, display: str, handler: Any = None) -> None:
        self.fleet = fleet
        self.id = agent_id
        self.display = display
        self.handler = handler
        self.queue: list[tuple[datetime, str]] = []
        self.plans: dict[str, dict] = {}
        self.status_template = f"{display} has not planned yet."
        self.mandates_seen_count = 0
        self.record: dict[str, list] = {
            "plans": [],
            "tool_calls": [],
            "reports": [],
            "stamps": [],
            "mandates_seen": [],
        }
        self.tools = self._make_tools()
        self.graph = self._build_graph()
        if handler is not None:
            from prismtrace import wrap_langgraph

            self.graph = wrap_langgraph(self.graph, handler)

    def now(self) -> datetime:
        return self.fleet.clock.now()

    def _log_tool(self, name: str, args: dict, result: str) -> None:
        self.record["tool_calls"].append(_entry(self.now(), tool=name, args=args, result=result))

    def config(self, tick: datetime | None) -> RunnableConfig:
        cfg: RunnableConfig = {
            "callbacks": [self.handler] if self.handler is not None else [],
            "metadata": {
                "company_day_id": company_day_id(self.now()),
                "mandate_id": self.fleet.current_mandate_id(),
                "run_version": self.fleet.run_version,
                "tick": fmt(tick) if tick else None,
                "seed": self.fleet.scenario.seed,
                "agent_id": self.id,  # UNVERIFIED whether spans-ingest metadata.agent_id is read server-side
            },
        }
        return cfg

    def _make_tools(self) -> dict[str, BaseTool]:
        agent = self
        fleet = self.fleet

        @tool
        async def read_cash() -> str:
            """Read the current cash balance in cents."""
            result = str(L.balance(fleet.ledger))
            agent._log_tool("read_cash", {}, result)
            return result

        @tool
        async def read_mandates() -> str:
            """Read the active mandates as JSON."""
            active = [m for m in fleet.mandates if m.bound_at is not None]
            result = json.dumps([m.model_dump(mode="json") for m in active])
            for m in active:
                agent.record["mandates_seen"].append(_entry(agent.now(), mandate_id=m.id, text=m.compiled_text))
            agent.mandates_seen_count = len(fleet.mandates)
            agent._log_tool("read_mandates", {}, result)
            return result

        @tool
        async def schedule_payment(payment_id: str, at: str) -> str:
            """Enqueue payment_id for execution at company time at (HH:MM:SS, today)."""
            p = fleet.ledger.payments[payment_id]
            when = datetime.combine(p.scheduled_at.date(), ct(at).time())
            tick = agent.now()
            plan = {
                "tick": fmt(tick),
                "payment_id": payment_id,
                "execute_at": fmt(when),
                "status_template": None,
                "mandate_id": fleet.current_mandate_id(),
            }
            agent.plans[payment_id] = plan
            agent.queue.append((when, payment_id))
            agent.record["plans"].append(_entry(tick, **plan))
            result = f"scheduled {payment_id} at {fmt(when)}"
            agent._log_tool("schedule_payment", {"payment_id": payment_id, "at": at}, result)
            await fleet.publish("payment.scheduled", agent_id=agent.id, payment_id=payment_id, at=fmt(when))
            return result

        @tool
        async def execute_payment(payment_id: str) -> str:
            """Execute payment_id now."""
            # V1-RACE: no mandate check here and no re-read of mandates. The Executor calls this
            # from the plan formed at the last tick. ap_west scheduled Halden Logistics at the 10:00
            # tick with no mandate in context; the order binds at 10:02:07; this fires at 10:03:14
            # and pays $51,000 against the bound mandate, then reports the stale template.
            now = agent.now()
            plan = agent.plans.get(payment_id, {})
            stamp = Stamp(
                mandate_id=plan.get("mandate_id") or "none",
                checked_at=now,
                decision="allow",
                reason=f"scheduled at tick {plan.get('tick', 'unknown')} without re-check",
                transcript_excerpt=plan.get("status_template") or agent.status_template,
            )
            fleet.ledger = L.execute(fleet.ledger, payment_id, now, stamp)
            p = fleet.ledger.payments[payment_id]
            agent.record["stamps"].append(_entry(now, payment_id=payment_id, **stamp.model_dump(mode="json")))
            result = f"executed {payment_id} {p.vendor} {p.amount_cents} {p.rail} at {fmt(now)}"
            agent._log_tool("execute_payment", {"payment_id": payment_id}, result)
            await fleet.publish(
                "payment.executed",
                agent_id=agent.id,
                payment_id=payment_id,
                vendor=p.vendor,
                amount_cents=p.amount_cents,
                mandate_id=stamp.mandate_id,
                over_threshold=p.amount_cents > fleet.scenario.threshold_cents,
            )
            return result

        @tool
        async def hold_payment(payment_id: str, reason: str) -> str:
            """Hold payment_id and record why."""
            now = agent.now()
            stamp = Stamp(
                mandate_id=fleet.current_mandate_id() or "none",
                checked_at=now,
                decision="hold",
                reason=reason,
                transcript_excerpt=reason,
            )
            fleet.ledger = L.hold(fleet.ledger, payment_id, now, stamp)
            agent.record["stamps"].append(_entry(now, payment_id=payment_id, **stamp.model_dump(mode="json")))
            result = f"held {payment_id}: {reason}"
            agent._log_tool("hold_payment", {"payment_id": payment_id, "reason": reason}, result)
            await fleet.publish("payment.held", agent_id=agent.id, payment_id=payment_id, reason=reason)
            return result

        @tool
        async def escalate(payment_id: str, reason: str) -> str:
            """Escalate payment_id to a human."""
            now = agent.now()
            stamp = Stamp(
                mandate_id=fleet.current_mandate_id() or "none",
                checked_at=now,
                decision="escalate",
                reason=reason,
                transcript_excerpt=reason,
            )
            fleet.ledger = L.escalate(fleet.ledger, payment_id, now, stamp)
            agent.record["stamps"].append(_entry(now, payment_id=payment_id, **stamp.model_dump(mode="json")))
            result = f"escalated {payment_id}: {reason}"
            agent._log_tool("escalate", {"payment_id": payment_id, "reason": reason}, result)
            await fleet.publish("payment.escalated", agent_id=agent.id, payment_id=payment_id, reason=reason)
            return result

        @tool
        async def report_status(sentence: str) -> str:
            """Report a one-sentence status."""
            now = agent.now()
            agent.record["reports"].append(_entry(now, sentence=sentence, source="tick"))
            agent._log_tool("report_status", {"sentence": sentence}, "ok")
            await fleet.publish("status.report", agent_id=agent.id, sentence=sentence, source="tick")
            return "ok"

        return {
            t.name: t
            for t in (read_cash, read_mandates, schedule_payment, execute_payment, hold_payment, escalate, report_status)
        }

    def _build_graph(self):
        agent = self
        fleet = self.fleet

        async def read_state(state: TickState, config: RunnableConfig) -> TickState:
            return {"cash": await agent.tools["read_cash"].ainvoke({}, config)}

        async def read_mandates(state: TickState, config: RunnableConfig) -> TickState:
            return {"mandates_json": await agent.tools["read_mandates"].ainvoke({}, config)}

        async def decide(state: TickState, config: RunnableConfig) -> TickState:
            # V1: mandates enter the agent's context here, at the start of the decision step, and nowhere else.
            ids = {m["id"] for m in json.loads(state["mandates_json"])}
            ctx = DecisionContext(
                agent_id=agent.id,
                display=agent.display,
                tick=ct(state["tick"]),
                cash_cents=int(state["cash"]),
                due=[fleet.ledger.payments[i] for i in state["due_ids"]],
                mandates=[m for m in fleet.mandates if m.id in ids],
                threshold_cents=fleet.scenario.threshold_cents,
                exempt_ids=fleet.exempt_ids,
            )
            decision = await fleet.decider.decide(ctx, config)
            return {"decision": decision.model_dump()}

        async def act(state: TickState, config: RunnableConfig) -> TickState:
            d = Decision.model_validate(state["decision"])
            for a in d.actions:
                if a.kind == "schedule":
                    p = fleet.ledger.payments[a.payment_id]
                    await agent.tools["schedule_payment"].ainvoke(
                        {"payment_id": a.payment_id, "at": a.at or p.scheduled_at.strftime("%H:%M:%S")}, config
                    )
                elif a.kind == "hold":
                    await agent.tools["hold_payment"].ainvoke({"payment_id": a.payment_id, "reason": a.reason}, config)
                else:
                    await agent.tools["escalate"].ainvoke({"payment_id": a.payment_id, "reason": a.reason}, config)
            return {}

        async def report(state: TickState, config: RunnableConfig) -> TickState:
            d = Decision.model_validate(state["decision"])
            agent.status_template = d.status_template
            for a in d.actions:
                if a.kind == "schedule" and a.payment_id in agent.plans:
                    agent.plans[a.payment_id]["status_template"] = d.status_template
            return {"report": await agent.tools["report_status"].ainvoke({"sentence": d.status_sentence}, config)}

        g = StateGraph(TickState)
        g.add_node("read_state", read_state)
        g.add_node("read_mandates", read_mandates)
        g.add_node("decide", decide)
        g.add_node("act", act)
        g.add_node("report", report)
        g.add_edge(START, "read_state")
        g.add_edge("read_state", "read_mandates")
        g.add_edge("read_mandates", "decide")
        g.add_edge("decide", "act")
        g.add_edge("act", "report")
        g.add_edge("report", END)
        return g.compile()

    def due_at(self, tick: datetime) -> list[str]:
        queued = {pid for _, pid in self.queue}
        return [
            p.id
            for p in self.fleet.ledger.payments.values()
            if p.agent_id == self.id
            and p.status == "planned"
            and p.id not in queued
            and p.id not in self.plans
            and tick <= p.scheduled_at < tick + TICK
        ]

    async def run_tick(self, tick: datetime) -> None:
        due = self.due_at(tick)
        if not due and self.mandates_seen_count == len(self.fleet.mandates):
            return
        state: TickState = {"tick": fmt(tick), "due_ids": due}
        await self.graph.ainvoke(state, self.config(tick))

    async def fire(self, payment_id: str, at: datetime) -> None:
        plan = self.plans.get(payment_id, {})
        tick = ct(plan["tick"]) if plan.get("tick") else None
        config = self.config(tick)
        trace_id = getattr(self.handler, "trace_id", None)
        await self.tools["execute_payment"].ainvoke({"payment_id": payment_id}, config)
        if self.handler is not None:
            self.handler.flush()
        sentence = plan.get("status_template") or self.status_template
        self.record["reports"].append(
            _entry(at, sentence=sentence, source="executor", scheduled_at_tick=plan.get("tick"), prism_trace_id=trace_id)
        )
        await self.fleet.publish(
            "status.report", agent_id=self.id, sentence=sentence, source="executor", scheduled_at_tick=plan.get("tick")
        )


class Fleet:
    def __init__(
        self,
        scenario: Scenario,
        clock: CompanyClock,
        bus: EventBus,
        decider: Decider | None = None,
        session_id: str = "mandate-v1-01",
        run_version: str = "v1",
        handlers: dict[str, Any] | None = None,
    ) -> None:
        self.scenario = scenario
        self.clock = clock
        self.bus = bus
        self.decider: Decider = decider or LLMDecider()
        self.session_id = session_id
        self.run_version = run_version
        self.ledger = L.from_scenario(scenario)
        self.mandates: list[Mandate] = []
        self.pending_mandates: list[tuple[datetime, Mandate]] = []
        self.exempt_ids = frozenset(scenario.exception_eligible)
        self.sem = asyncio.Semaphore(MAX_PARALLEL)
        handlers = handlers or {}
        self.agents = {a.id: AgentRuntime(self, a.id, a.display, handlers.get(a.id)) for a in scenario.agents}
        self.next_tick = next_tick_after(clock.now())

    def current_mandate_id(self) -> str | None:
        active = [m for m in self.mandates if m.bound_at is not None]
        return active[-1].id if active else None

    async def publish(self, type: str, **payload: Any) -> None:
        now = self.clock.now()
        await self.bus.publish(Event(type=type, ts_wall=datetime.now(), ts_company=now, payload=payload))

    def schedule_mandate(self, mandate: Mandate, at: datetime | str) -> None:
        self.pending_mandates.append((ct(at), mandate))

    async def bind_mandate(self, mandate: Mandate, at: datetime | None = None) -> None:
        mandate.bound_at = at or self.clock.now()
        self.mandates.append(mandate)
        await self.publish("mandate.bound", mandate_id=mandate.id, text=mandate.compiled_text, at=fmt(mandate.bound_at))

    async def _fire_due(self, now: datetime) -> None:
        for agent in self.agents.values():
            ready = sorted([q for q in agent.queue if q[0] <= now])
            agent.queue = [q for q in agent.queue if q[0] > now]
            for at, pid in ready:
                await agent.fire(pid, at)

    async def _tick(self, tick: datetime) -> None:
        await self.publish("tick", at=fmt(tick))

        async def one(agent: AgentRuntime) -> None:
            async with self.sem:
                await agent.run_tick(tick)

        await asyncio.gather(*(one(a) for a in self.agents.values()))

    async def run(self, until: datetime | str) -> None:
        until = ct(until)
        while True:
            times = [self.next_tick]
            times += [at for a in self.agents.values() for at, _ in a.queue]
            times += [at for at, _ in self.pending_mandates]
            t = min(times)
            if t > until:
                break
            await self.clock.sleep_until(t)
            now = self.clock.now()
            for at, m in sorted(self.pending_mandates, key=lambda x: x[0]):
                if at <= now:
                    await self.bind_mandate(m, at)
            self.pending_mandates = [(at, m) for at, m in self.pending_mandates if at > now]
            await self._fire_due(now)
            if self.next_tick <= now:
                tick = self.next_tick
                self.next_tick = next_tick_after(tick)
                await self._tick(tick)
                await self._fire_due(now)

    def get_record(self, agent_id: str, as_of: datetime | str | None = None) -> dict:
        agent = self.agents[agent_id]
        rec = copy.deepcopy(agent.record)
        if as_of is not None:
            cutoff = ct(as_of).isoformat()
            rec = {k: [e for e in v if e["at"] <= cutoff] for k, v in rec.items()}
        return {"agent_id": agent.id, "display": agent.display, "session_id": self.session_id, **rec}
