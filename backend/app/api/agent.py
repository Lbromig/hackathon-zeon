"""Agent orchestration endpoint: streams the P0 normal-mode loop over a websocket.

Runs the (blocking) engine off the event loop in a worker thread and forwards each
event to the client, mirroring ``/ws/workflow``. The engine uses the deterministic
:class:`RuleBasedPolicy` so it runs with no API key and no hardware.

Human-in-the-loop checkpoints are supported: when the engine emits a ``checkpoint``
event it blocks until the client sends a message (any JSON, e.g. ``{"action": "continue"}``)
back over the same socket. Disable pausing with ``?checkpoints=none`` for a hands-off run.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.engine import DEFAULT_CHECKPOINTS, GOAL, Engine
from ..agent.policy import RuleBasedPolicy
from ..agent.tools import Toolbox
from ..services.device_manager import device_manager

router = APIRouter(tags=["agent"])


class _CheckpointGate:
    """Cross-thread gate: engine thread blocks in ``wait``; event loop calls ``release``."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def wait(self, _checkpoint: dict[str, Any]) -> None:
        self._event.wait()
        self._event.clear()

    def release(self) -> None:
        self._event.set()


def _parse_checkpoints(raw: str | None) -> frozenset[str]:
    if raw is None or raw == "default":
        return DEFAULT_CHECKPOINTS
    if raw in ("", "none"):
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


@router.get("/api/agent/goal")
def goal() -> dict:
    return {"goal": GOAL, "checkpoints": sorted(DEFAULT_CHECKPOINTS)}


@router.websocket("/ws/agent")
async def run_agent(ws: WebSocket) -> None:
    """Run the P0 selector loop (RuleBasedPolicy), streaming step/verify events."""
    await ws.accept()

    checkpoints = _parse_checkpoints(ws.query_params.get("checkpoints"))
    gate = _CheckpointGate()
    engine = Engine(
        Toolbox(device_manager),
        policy=RuleBasedPolicy(),
        checkpoints=checkpoints,
        checkpoint_gate=gate.wait,
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()

    def _worker() -> None:
        try:
            for event in engine.run():
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # surface engine errors instead of a silent hang
            loop.call_soon_threadsafe(queue.put_nowait, {"phase": "error", "error": str(exc)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    worker = asyncio.create_task(asyncio.to_thread(_worker))
    try:
        while True:
            event = await queue.get()
            if event is sentinel:
                break
            await ws.send_json(event)
            if event.get("phase") == "checkpoint":
                await ws.receive_json()   # wait for the human to approve/continue
                gate.release()
        # The engine yields its own terminal event ("done"/"stopped"/"failed"),
        # matching uncap_aspirate consumers — no extra "done" needed here.
    except WebSocketDisconnect:
        gate.release()  # unblock the worker so it can finish
    finally:
        await worker
