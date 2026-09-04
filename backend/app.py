import asyncio
import logging
import sys

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from mandate.bus import bus
from mandate.schemas import Event

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()

app = FastAPI(title="mandate")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    queue: asyncio.Queue[Event] = asyncio.Queue()

    def on_event(event: Event) -> None:
        queue.put_nowait(event)

    unsubscribe = bus.subscribe(on_event)
    log.info("ws.connect", client=str(ws.client))
    try:
        while True:
            event = await queue.get()
            await ws.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        log.info("ws.disconnect", client=str(ws.client))
    finally:
        unsubscribe()
