import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from mandate.schemas import Event

Subscriber = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self, sink: Path | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._sink = sink
        self._lock = asyncio.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        if self._sink is not None:
            line = event.model_dump_json() + "\n"
            async with self._lock:
                with self._sink.open("a", encoding="utf-8") as f:
                    f.write(line)
        results = []
        for fn in list(self._subscribers):
            r = fn(event)
            if asyncio.iscoroutine(r):
                results.append(r)
        if results:
            await asyncio.gather(*results)


bus = EventBus()
