"""The event protocol — **FROZEN at the end of Wave 0**. Types only.

One socket, one envelope, and **a monotonically increasing sequence number** (D24). The `seq`
is not decoration: without it a frontend that reconnects mid-run cannot tell whether it
missed anything, and therefore cannot know whether its view is a true picture of the run or a
partial one that happens to look complete. With it, the client compares the first `seq` it
receives against the last it saw, and re-fetches the snapshot if there is a gap (review B8,
R-ENG-18).

The companion half of D24 is `RunSnapshot`: the **full current state** — plan, per-action
state, results so far, readiness — served from an endpoint so a reconnect (or a browser
refresh, or opening a second tab) reconstructs everything without having observed the earlier
events. An event stream alone cannot do that; it only carries deltas.

S8 owns the endpoint and the fan-out. This file is the shape both ends agree on.
"""
from __future__ import annotations

import itertools
import threading
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .actions import ActionResult, ActionState, Warning_

RunState = Literal["idle", "preflight", "running", "pausing", "paused", "complete",
                   "failed", "aborted"]
"""`pausing` is a real state, not a UI nicety (D9/R-ENG-8). Pause is cooperative and lands at
the next action boundary, so between the request and the boundary the honest answer is
"pausing" — a UI claiming an instant stop while a multi-second move finishes is lying about
where the arm is. Do not let this be "improved" into a mid-trajectory `driver.stop()`; abort
stays the separate hard path (R-ENG-10)."""

ReadinessState = Literal["initializing", "ready", "degraded", "failed"]
"""R-INIT-6. Start is refused on `failed`; on `degraded` it needs explicit operator
confirmation naming the degradations (R-ENG-2)."""


class EventSequence:
    """Hands out monotonically increasing sequence numbers, thread-safely.

    Thread-safe because the runner thread emits action events while the asyncio side emits
    control events (D1: one worker thread, asyncio only for fan-out). Two emitters plus a
    bare `+= 1` is a duplicated `seq`, and a duplicated `seq` silently defeats the gap
    detection this whole mechanism exists for.

    Per run, not per process: a client's "last seen" is only meaningful within one run, and a
    process-global counter would make a fresh run look like it had already missed events.
    """

    def __init__(self, start: int = 0) -> None:
        self._counter = itertools.count(start + 1)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            return next(self._counter)


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int
    """Monotonic within the run, starting at 1. A client that sees `seq` jump has missed
    events and must re-fetch the snapshot rather than render a partial plan (D24)."""
    ts: str = ""
    run_id: str = ""


class RunStarted(EventBase):
    type: Literal["run_started"] = "run_started"
    name: str = ""
    action_count: int = 0
    simulated: dict[str, bool] = Field(default_factory=dict)
    """Per device, from the resolved config (D25). Sent up front and persistently visible in
    the UI, because with simulation the default (D29) a simulated run must never be
    mistakable for a real one — a correctness requirement, not chrome."""


class PlanReplaced(EventBase):
    """The whole plan, after any mutation: injection, loop materialization, or the initial
    load. Sent whole rather than as a diff because `index` is *derived* and an injection
    renumbers everything after the insertion point (R-ENG-4) — a diff of derived values is a
    second implementation of the renumbering rule, on the client, in TypeScript.

    `revision` increments per mutation so a client can tell two plans apart; `cursor` is the
    index of the action about to run."""
    type: Literal["plan_replaced"] = "plan_replaced"
    revision: int = 0
    cursor: int = 0
    actions: list[dict[str, Any]] = Field(default_factory=list)
    """Each entry: `aid`, `index`, `kind`, `device`, `label`, `speed`, `state`, `parent_aid`,
    `iteration`, `origin`, `params`. Rows for the UI, not full `Action` models — `params`
    carries the kind-specific fields."""


class ActionStarted(EventBase):
    type: Literal["action_started"] = "action_started"
    aid: int
    index: int
    kind: str
    device: str | None = None
    attempt: int = 1
    inputs: dict[str, Any] = Field(default_factory=dict)


class ActionLog(EventBase):
    """A **convenience mirror** of the log file for the action currently running.

    Capped per action on the socket; the full record always comes from `GET /api/logs`. This
    keeps "expand the live action to see its logs" instant without making the engine socket
    the log transport — the file is the transport, and it survives a disconnect."""
    type: Literal["action_log"] = "action_log"
    aid: int
    level: str = "INFO"
    msg: str = ""


class ActionFinished(EventBase):
    type: Literal["action_finished"] = "action_finished"
    aid: int
    index: int
    result: ActionResult
    """The whole typed result — status, outputs, artifacts, warnings, error, `simulated`,
    `log_ref`. One event carries everything the plan row needs to render (R-UI-4)."""


class RunPaused(EventBase):
    type: Literal["run_paused"] = "run_paused"
    reason: Literal["operator", "action_failed", "checkpoint", "inject"] = "operator"
    aid: int | None = None
    """Set when a specific action caused it, so the UI can point at the row."""


class RunResumed(EventBase):
    type: Literal["run_resumed"] = "run_resumed"


class RunStateChanged(EventBase):
    """State transitions that are not a pause or a finish — notably `running` -> `pausing`,
    which is what makes D9's intermediate state visible instead of inferred."""
    type: Literal["run_state"] = "run_state"
    state: RunState


class LoopIteration(EventBase):
    """One materialized iteration of `control.loop`, with the numbers that decide whether it
    continues. Emitted alongside the materialized actions so the UI can show iteration 4's
    metrics without joining across its rows (R-VIS-8)."""
    type: Literal["loop_iteration"] = "loop_iteration"
    aid: int
    iteration: int
    magnitude_mm: float | None = None
    sigma_mm: dict[str, float] = Field(default_factory=dict)
    threshold_mm: float | None = None
    improving: bool = True


class RunFinished(EventBase):
    type: Literal["run_finished"] = "run_finished"
    status: Literal["complete", "failed", "aborted"] = "complete"
    completed: int = 0
    failed: int = 0
    duration_ms: int = 0


class ReadinessChanged(EventBase):
    """R-INIT-6 / R-UI-13. Warnings are structured (`Warning_.code`), so the readiness panel
    matches on a code rather than on message text — including R-INIT-4's `home_not_defined`,
    which must be reachable programmatically and not only visible in a terminal."""
    type: Literal["readiness"] = "readiness"
    state: ReadinessState = "initializing"
    warnings: list[Warning_] = Field(default_factory=list)
    devices: dict[str, Any] = Field(default_factory=dict)


class InjectRejected(EventBase):
    """An injection the engine refused, with the reason (D22/R-ENG-13).

    An event rather than only an HTTP error, because the refusal has to be visible to every
    connected client — the operator who tried it may not be the one watching. **Refusing with
    a stated reason is required behaviour**: silently inserting somewhere else is the failure
    mode R-ENG-13 forbids. And landing at the cursor must *not* be refused — that is exactly
    where a failure leaves the run, so it is the most useful place to inject."""
    type: Literal["inject_rejected"] = "inject_rejected"
    reason: str = ""
    after_aid: int | None = None


Event = Annotated[
    Union[
        RunStarted, PlanReplaced, ActionStarted, ActionLog, ActionFinished,
        RunPaused, RunResumed, RunStateChanged, LoopIteration, RunFinished,
        ReadinessChanged, InjectRejected,
    ],
    Field(discriminator="type"),
]

EVENT_MODELS: tuple[type[EventBase], ...] = (
    RunStarted, PlanReplaced, ActionStarted, ActionLog, ActionFinished,
    RunPaused, RunResumed, RunStateChanged, LoopIteration, RunFinished,
    ReadinessChanged, InjectRejected,
)

EVENT_TYPES: tuple[str, ...] = tuple(
    m.model_fields["type"].default for m in EVENT_MODELS      # type: ignore[misc]
)

event_adapter: TypeAdapter[Any] = TypeAdapter(Event)
"""Parses an event from its JSON form. Used by tests and by any consumer that wants the
typed model back; the frontend generates its TypeScript from `event_adapter.json_schema()`
rather than hand-writing it."""


class ActionRow(BaseModel):
    """One plan row as the UI needs it: identity, derived index, state, and outputs so far."""
    model_config = ConfigDict(extra="forbid")
    aid: int
    index: int
    kind: str
    device: str | None = None
    label: str = ""
    speed: str = "medium"
    state: ActionState = "planned"
    parent_aid: int | None = None
    iteration: int | None = None
    origin: str = "plan"
    params: dict[str, Any] = Field(default_factory=dict)
    result: ActionResult | None = None


class RunSnapshot(BaseModel):
    """The full current state of a run — the other half of D24.

    Served from an endpoint (S8's `GET /api/engine/run`) so a websocket reconnect, a browser
    refresh, or a second tab reconstructs everything: the plan, every action's state, the
    results recorded so far, the readiness warnings, and which devices are simulated. Without
    this, a client that connects mid-run can only show the future.

    `seq` is the sequence number this snapshot is current as of. A client applies subsequent
    events with a higher `seq` and discards lower ones, which is what makes the
    snapshot-then-stream handover race-free.
    """
    model_config = ConfigDict(extra="forbid")
    seq: int = 0
    run_id: str = ""
    name: str = ""
    state: RunState = "idle"
    revision: int = 0
    cursor: int = 0
    started_at: str = ""
    actions: list[ActionRow] = Field(default_factory=list)
    readiness: ReadinessState = "initializing"
    warnings: list[Warning_] = Field(default_factory=list)
    simulated: dict[str, bool] = Field(default_factory=dict)
    """Per device (D25/D29). Present on the snapshot as well as on `RunStarted` so a client
    that never saw the run start still knows, unmissably, whether this is real."""
