from __future__ import annotations

import os
import sys
from typing import Any

import httpx

from mandate import compiler
from mandate.compiler import CANONICAL_ORDER, CompanyState, Engine
from mandate.scenario import fmt
from mandate.schemas import Mandate


class DeskError(Exception):
    pass


class Desk:
    def __init__(self, fleet: Any) -> None:
        self.fleet = fleet
        self.pending: dict[str, tuple[Mandate, str, list[str]]] = {}

    def company_state(self) -> CompanyState:
        sc = self.fleet.scenario
        return CompanyState(
            now=self.fleet.clock.now(),
            payroll_due=sc.company.payroll.due,
            tax_due=sc.company.tax.due,
            agents=[a.id for a in sc.agents],
            vendors=sorted({p.vendor for p in sc.payments}),
        )

    def _stage(self, mandate: Mandate, readback: str, questions: list[str]) -> dict:
        self.pending[mandate.id] = (mandate, readback, questions)
        conflict = None
        for existing in self.fleet.active_mandates():
            conflict = compiler.conflicts(existing, mandate)
            if conflict:
                break
        return {"mandate_id": mandate.id, "readback_text": readback, "questions": questions, "conflict": conflict, "bindable": not questions, "mandate": mandate.model_dump(mode="json")}

    async def compile(self, text: str, engine: Engine | None = None) -> dict:
        mandate, readback, questions = compiler.compile(text, self.company_state(), engine)
        out = self._stage(mandate, readback, questions)
        await self.fleet.publish("desk.compiled", mandate_id=mandate.id, text=text, readback=readback, questions=questions, conflict=out["conflict"])
        return out

    async def inject(self) -> dict:
        text = os.environ.get("CANONICAL_ORDER", CANONICAL_ORDER)
        mandate = compiler.canonical_mandate(self.fleet.clock.now(), text)
        out = self._stage(mandate, compiler.CANONICAL_READBACK, [])
        await self.fleet.publish("desk.compiled", mandate_id=mandate.id, text=text, readback=compiler.CANONICAL_READBACK, questions=[], conflict=out["conflict"], injected=True)
        return out

    async def confirm(self, mandate_id: str) -> Mandate:
        if mandate_id not in self.pending:
            raise DeskError(f"no pending mandate {mandate_id}")
        mandate, _, questions = self.pending[mandate_id]
        if questions:
            raise DeskError("cannot bind: " + " ".join(questions))
        del self.pending[mandate_id]
        now = self.fleet.clock.now()
        await self.fleet.publish("desk.confirmed", mandate_id=mandate_id, at=fmt(now))
        await self.fleet.bind_mandate(mandate, now)
        return mandate

    def cancel(self, mandate_id: str) -> None:
        self.pending.pop(mandate_id, None)


def typed_path(base: str = "http://localhost:8000") -> int:
    """Keyboard-only Desk: type an order, read the read-back, press Enter to confirm. No audio."""
    client = httpx.Client(base_url=base, timeout=30)
    print("MANDATE desk (typed). Empty line to quit. 'inject' sends the canonical order.")
    while True:
        try:
            text = input("order> ").strip()
        except EOFError:
            return 0
        if not text:
            return 0
        r = client.post("/desk/inject") if text == "inject" else client.post("/desk/compile", json={"text": text})
        body = r.json()
        print(body.get("readback_text"))
        if body.get("conflict"):
            print("conflict:", body["conflict"])
        if not body.get("bindable"):
            continue
        ans = input("[Enter]=confirm  n=cancel > ").strip().lower()
        if ans in ("", "y", "yes"):
            c = client.post("/desk/confirm", json={"mandate_id": body["mandate_id"]})
            print("bound" if c.status_code == 200 else f"not bound: {c.text}")
        else:
            print("cancelled")


if __name__ == "__main__":
    sys.exit(typed_path(os.environ.get("MANDATE_API", "http://localhost:8000")))
