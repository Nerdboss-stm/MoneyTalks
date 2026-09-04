from datetime import datetime

from mandate.bus import EventBus
from mandate.schemas import Event


async def test_publish_reaches_two_subscribers(tmp_path):
    bus = EventBus(sink=tmp_path / "events.jsonl")
    got_a: list[Event] = []
    got_b: list[Event] = []

    def a(e: Event) -> None:
        got_a.append(e)

    async def b(e: Event) -> None:
        got_b.append(e)

    bus.subscribe(a)
    bus.subscribe(b)

    t = datetime(2026, 9, 5, 11, 0, 0)
    e = Event(type="tick", ts_wall=t, ts_company=t, payload={"tick": 1})
    await bus.publish(e)

    assert got_a == [e]
    assert got_b == [e]
    assert (tmp_path / "events.jsonl").read_text().count("\n") == 1
