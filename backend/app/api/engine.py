"""The engine's HTTP + websocket API — load a plan, run it, watch it, change it (W4).

    POST   /api/engine/plan        load a named plan (handover | startup) -> RunSnapshot
    GET    /api/engine/snapshot    full current state — THE reconnect path
    POST   /api/engine/start       {allow_degraded} -> 409 + PreflightReport when refused
    POST   /api/engine/pause       -> {state}
    POST   /api/engine/resume
    POST   /api/engine/abort
    POST   /api/engine/inject      {after_aid, action} -> 409 + reason on refusal
    GET    /api/engine/preflight   without starting, for the readiness panel
    WS     /ws/engine              the event stream

Four things in here are load-bearing and were each learned the hard way.

**`/ws/engine` is registered on a prefix-less router.** An `APIRouter` prefix applies to
websocket routes too, so hanging it off `/api/engine` would silently rename the path and every
client would see a closed socket with no error. Same reason `/ws/state` sits on its own router
in `api/instruments.py`.

**Frames go out as `send_text(event.model_dump_json())`, never `send_json`.** Starlette's
`send_json` uses stdlib `json`, which emits bare `Infinity` / `NaN` for a non-finite float —
not valid JSON, and `JSON.parse` rejects the whole frame, so one ill-conditioned solve would
break the stream for *every* connected client (B5). Pydantic maps non-finite to `null`, so the
text path is correct by construction. `response_model=RunSnapshot` on the snapshot route buys
the same guarantee over HTTP.

**Ordering is guaranteed here rather than delegated to the client.** `EventSink.emit` allocates
`seq` under its lock but calls subscribers outside it, and the API thread emits as well as the
worker — so two events can reach a subscriber out of order. The subscriber callback therefore
only *wakes* the sender (`loop.call_soon_threadsafe`, because the callback runs on the runner's
worker thread and must never touch the socket); one sender task then drains
`sink.since(last_sent)` in `seq` order. The socket emits strictly increasing `seq`, which makes
the client's "drop `seq <= last_seq`" rule trivially correct.

**`seq` is one monotonic sequence per process, not per run** (`events.PROCESS_SEQUENCE`, B6). A
client keeps one `last_seq` for the socket and never resets it — not on a new run, not on a
`run_id` change. A `run_id` change means "re-fetch the snapshot to rebuild plan state"; a gap
(`seq > last_seq + 1`) means the same. `since()` is a bounded ~2000-event replay, so comparing
against `RunSnapshot.seq` is how a client tells "nothing new" from "fell off the buffer".

The pause -> inject -> resume sequence lives here, in `POST /api/engine/inject`, because
`Runner.inject` deliberately does not do it (R-ENG-12): only the endpoint knows whether the
operator sent one action or three, and only it knows whether the run was already paused — in
which case it must stay paused, because the operator paused it on purpose.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.obs import get_logger

from ..engine.actions import Action, ActionBase
from ..engine.events import ActionRow, ReadinessState, RunSnapshot, RunState
from ..engine.plan import InjectRefused, PreflightReport
from ..engine.runner import StartRefused
from ..services.run_manager import NoRunLoaded, RunActive, run_manager

log = get_logger(__name__)

router = APIRouter(prefix="/api/engine", tags=["engine"])

#: Prefix-less, because an `APIRouter` prefix would rename the websocket path too.
ws_router = APIRouter(tags=["engine"])

#: How long `POST /api/engine/inject` waits for a pause it requested to actually land before
#: inserting anyway. Pause is cooperative: the action in flight runs to completion, and a decap
#: bite or a 430 mm traverse can take seconds. Waiting forever would hang the request; not
#: waiting at all would report `pausing` in a response that has already mutated the plan. So:
#: bounded wait, then insert regardless — which is safe, because an insertion *after* the
#: running action is accepted by design whether it is moving or not (D22).
INJECT_PAUSE_TIMEOUT_S = 5.0

#: Poll interval for that wait. Short enough to feel instant, long enough not to spin.
_PAUSE_POLL_S = 0.02


# --- request / response shapes ------------------------------------------------

class LoadPlanRequest(BaseModel):
    """`handover` is the 19-action workflow; `startup` is the one-action initialization plan
    (D5), which is a plan rather than a boot function so it gets indices, per-action logs and
    pause for free."""
    model_config = ConfigDict(extra="forbid")
    name: Literal["handover", "startup"] = "handover"
    home_after: bool = True
    """`startup` only: home each arm at the end. An arm with no taught HOME warns and is
    skipped, never sent to a guessed pose (R-INIT-4)."""


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_degraded: bool = False
    """R-ENG-2's confirmation path. Advisory problems refuse a bare start — including
    `empty_plan`, which is advisory rather than blocking — and this is the operator saying "I
    have read them". Blocking problems are refused either way."""


class InjectRequest(BaseModel):
    """Exactly one of `action` / `actions`, and the position is an **identity** (`after_aid`),
    never an index: an index would be renumbered out from under the request (R-ENG-4)."""
    model_config = ConfigDict(extra="forbid")
    after_aid: int | None = None
    """`null` or `0` means the very front of the plan — which is refused during a run, since
    the run has already passed it."""
    action: Action | None = None
    actions: list[Action] | None = None
    pause_around: bool | None = None
    """`None` (default) pauses only if the run is `running`, and resumes afterwards only if it
    was this request that paused it. `False` never pauses; `True` pauses even a run that is
    only `pausing`."""

    @model_validator(mode="after")
    def _exactly_one(self) -> "InjectRequest":
        if (self.action is None) == (self.actions is None):
            raise ValueError("send exactly one of 'action' or 'actions'")
        if self.actions is not None and not self.actions:
            raise ValueError("'actions' must not be empty")
        return self

    def items(self) -> list[ActionBase]:
        return list(self.actions) if self.actions is not None else [self.action]  # type: ignore[list-item]


class ProblemOut(BaseModel):
    """One pre-flight problem. `code` is what a UI matches on — never the message text
    (R-LOG-6/R-UI-13) — and a waypoint problem always carries `(device, waypoint)` rather than
    a bare name (R-WP-5)."""
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    aid: int | None = None
    device: str | None = None
    waypoint: str | None = None
    blocking: bool = True


class PreflightOut(BaseModel):
    """Every problem, not the first one: an operator fixing a bench wants the whole list."""
    model_config = ConfigDict(extra="forbid")
    run_id: str = ""
    seq: int = 0
    readiness: ReadinessState = "initializing"
    ok: bool = True
    """True when nothing *blocking* was found. Advisory problems leave `ok` true and
    `readiness` `degraded`, and still refuse a start without `allow_degraded`."""
    reason: str = ""
    problems: list[ProblemOut] = Field(default_factory=list)


class StartOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    state: RunState
    seq: int
    started_at: str = ""
    preflight: PreflightOut


class ControlOut(BaseModel):
    """The answer to pause / resume / abort. `state` is the state *after* the request, and it
    is the authority — `pausing` is a real state and must not be rendered as `paused` (D9)."""
    model_config = ConfigDict(extra="forbid")
    run_id: str
    state: RunState
    seq: int
    changed: bool
    """False when the control had nothing to do (pausing an idle run, resuming an aborted one).
    Not an error: the state says what is true now."""
    detail: str = ""


class InjectOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    state: RunState
    inserted: list[ActionRow] = Field(default_factory=list)
    """The adopted copies, with the `aid` the engine assigned and the index they landed at.
    An injected row inside a live loop iteration inherits that loop's `parent_aid`/`iteration`,
    so this is also how the client learns where it actually went."""
    paused_for_inject: bool = False
    resumed: bool = False
    snapshot: RunSnapshot
    """The whole plan after the insertion, because an injection renumbers every row after it
    (R-ENG-4) and `index` is derived. The socket carries the same thing as `plan_replaced`."""


# --- helpers ------------------------------------------------------------------

def _problems(report: PreflightReport) -> list[ProblemOut]:
    return [ProblemOut(code=p.code, message=p.message, aid=p.aid, device=p.device,
                       waypoint=p.waypoint, blocking=p.blocking) for p in report.problems]


def _preflight_out(report: PreflightReport, *, run_id: str, seq: int) -> PreflightOut:
    return PreflightOut(run_id=run_id, seq=seq, readiness=report.readiness, ok=report.ok,
                        reason=report.reason(), problems=_problems(report))


def _require():
    """The current runner, or 404 with the reason."""
    try:
        return run_manager.require()
    except NoRunLoaded as e:
        raise HTTPException(404, str(e)) from e


def _conflict(error: str, reason: str, **extra: Any) -> HTTPException:
    """Every 409 in this module has the same body: a machine-readable `error`, the reason
    verbatim (a refusal the operator cannot read is a refusal they will retry), and whatever
    the case needs on top."""
    return HTTPException(409, {"error": error, "reason": reason, **extra})


# --- plan ---------------------------------------------------------------------

@router.post("/plan", response_model=RunSnapshot)
def load_plan(body: LoadPlanRequest) -> RunSnapshot:
    """Load a named plan as a **new run**, and return the snapshot to render it from.

    Refused with 409 while a run is `preflight`/`running`/`pausing`/`paused` (B8). A paused run
    is refused too, and that is the important half: its worker thread is alive and still holds
    its device claim — swapping the plan under it leaves a cap half unscrewed and the next run's
    first gripper move refused as busy.

    The response's `run_id` is new on every load. That is the signal a client uses to reset its
    plan state; it never resets `last_seq`, which is process-wide (B6).
    """
    try:
        runner = run_manager.load(body.name, home_after=body.home_after)
    except RunActive as e:
        raise _conflict("run_active", e.reason, state=e.state, run_id=e.run_id) from e
    except KeyError as e:
        raise HTTPException(422, f"unknown plan {body.name!r}") from e
    return runner.snapshot()


@router.get("/snapshot", response_model=RunSnapshot)
def snapshot() -> RunSnapshot:
    """The full current state — plan, per-action state, every result so far, readiness, and
    which devices are simulated. **This is the reconnect path** (D24/R-ENG-18): a browser
    refresh, a second tab or a dropped socket rebuilds everything from here and then applies
    events with a higher `seq`.

    `seq` is read *before* the rows, deliberately, so a client can only ever re-apply an event
    (a no-op, since every state-carrying event is absolute) and never skip one.

    Returns an empty snapshot rather than 404 before any plan is loaded — a client still needs
    `seq` to seed from and `simulated` for the reality banner.
    """
    return run_manager.snapshot()


@router.get("/preflight", response_model=PreflightOut)
def preflight() -> PreflightOut:
    """Pre-flight without starting, for the readiness panel (R-ENG-16/R-INIT-6).

    Publishes readiness as a side effect (a `readiness` event), because the panel and the
    engine must not be able to disagree about it.
    """
    runner = _require()
    report = runner.preflight()
    return _preflight_out(report, run_id=runner.run_id, seq=run_manager.sink.last_seq)


# --- controls -----------------------------------------------------------------

@router.post("/start", response_model=StartOut)
def start(body: StartRequest | None = None) -> StartOut:
    """Pre-flight the whole plan, then run it on the engine's worker thread.

    409 on refusal, carrying **every** pre-flight problem — the UI renders the list, and an
    operator fixing a bench should not have to discover them one attempt at a time. Blocking
    problems are refused outright; advisory ones are refused unless `allow_degraded` confirms
    them (R-ENG-2). Note an empty plan is *advisory*, so a bare start on one is also refused.
    """
    runner = _require()
    request = body or StartRequest()
    # `started_at` is what separates "this run finished" from "pre-flight refused it", which
    # also leaves the state at `failed` (`Runner.start` sets it before raising) — and that one
    # must stay startable, because confirming the advisory problems is the whole point of
    # `allow_degraded`.
    if runner.state in ("complete", "failed", "aborted") and runner.snapshot().started_at:
        # Restarting a finished run would spawn a worker that finds the cursor at the end and
        # exits without executing anything — a "started" run that never moves. Say so instead.
        raise _conflict(
            "run_finished",
            f"this run has already finished ({runner.state}); load the plan again "
            f"(POST /api/engine/plan) to run it from the top"
            + (", or POST /api/engine/resume to continue from where it stopped"
               if runner.state == "failed" else ""),
            run_id=runner.run_id, state=runner.state)
    try:
        report = runner.start(allow_degraded=request.allow_degraded)
    except StartRefused as e:
        raise _conflict(
            "start_refused",
            e.report.reason() or "start refused: the plan has advisory problems that need "
                                 "confirming — resend with allow_degraded=true",
            run_id=runner.run_id, state=runner.state,
            preflight=_preflight_out(e.report, run_id=runner.run_id,
                                     seq=run_manager.sink.last_seq).model_dump(mode="json"),
        ) from e
    except RuntimeError as e:
        # "this run is already executing" — the only RuntimeError `start` raises itself.
        raise _conflict("run_active", str(e), run_id=runner.run_id, state=runner.state) from e
    return StartOut(run_id=runner.run_id, state=runner.state, seq=run_manager.sink.last_seq,
                    started_at=runner.snapshot().started_at,
                    preflight=_preflight_out(report, run_id=runner.run_id,
                                             seq=run_manager.sink.last_seq))


@router.post("/pause", response_model=ControlOut)
def pause() -> ControlOut:
    """Request a cooperative pause (D9/R-ENG-8).

    Returns `pausing` immediately, which is the honest answer while a commanded move finishes —
    this is deliberately not a mid-trajectory stop, that is abort. The run reports `paused` at
    the next boundary, or at once if a handler parks in `ctx.checkpoint()`. A client watches for
    that on the socket (`run_state`, then `run_paused`) or by re-fetching the snapshot; it must
    never render `pausing` as `paused`.
    """
    runner = _require()
    changed = runner.pause()
    return _control(runner, changed,
                    "" if changed else f"nothing to pause: the run is {runner.state}")


@router.post("/resume", response_model=ControlOut)
def resume() -> ControlOut:
    """Continue from where the pause landed — and the recovery path after a failure.

    Deliberately **not** refused for a failure inside the servo loop. The cursor stays on the
    row that failed and `resume()` re-enters the loop: the rest of that iteration runs
    (including anything injected after the failure), then the termination test, then further
    iterations. That is the intended recovery, and it is why the run never steps over the loop
    into the retract that follows it.
    """
    runner = _require()
    changed = runner.resume()
    return _control(runner, changed,
                    "" if changed else f"nothing to resume: the run is {runner.state}")


@router.post("/abort", response_model=ControlOut)
def abort() -> ControlOut:
    """Stop the run — the hard path, distinct from pause (R-ENG-10).

    Sets the abort flag and opens the pause gate, so a handler parked in `checkpoint()` wakes
    and raises `ActionAborted`, which is recorded as `aborted` and never as `failed`. It does
    not stop a move already commanded to a controller: **that is the e-stop**, which is the
    operator's own control (`POST /api/arms/{id}/stop`) and is never gated by anything here.
    """
    runner = _require()
    changed = runner.abort()
    return _control(runner, changed,
                    "" if changed else f"nothing to abort: the run is {runner.state}")


def _control(runner: Any, changed: bool, detail: str) -> ControlOut:
    return ControlOut(run_id=runner.run_id, state=runner.state,
                      seq=run_manager.sink.last_seq, changed=changed, detail=detail)


# --- injection ----------------------------------------------------------------

@router.post("/inject", response_model=InjectOut)
def inject(body: InjectRequest) -> InjectOut:
    """Insert one or more actions after `after_aid`, pausing around it when the run is moving.

    The pause -> inject -> resume sequence is R-ENG-12's and belongs here rather than in
    `Runner.inject`, because only this endpoint knows how many actions the operator sent (one
    pause for the batch, not one per action) and whether the run was already paused — if it was,
    it stays paused, because the operator paused it deliberately.

    Landing **at the cursor is accepted**: that is where a failure leaves the run and it is the
    most useful place to inject. Refusals come back as 409 with the engine's reason verbatim,
    and the engine has already emitted `inject_rejected` to every connected client — the
    operator who tried it may not be the one watching.
    """
    runner = _require()
    want_pause = body.pause_around
    if want_pause is None:
        want_pause = runner.state == "running"
    paused_here = bool(want_pause) and runner.pause(reason="inject")
    if paused_here:
        _await_pause(runner)
    try:
        inserted = runner.inject(body.items(), after_aid=body.after_aid)
    except InjectRefused as e:
        # The `inject_rejected` event has already gone out; do not emit a second one.
        raise _conflict("inject_refused", e.reason, after_aid=body.after_aid,
                        run_id=runner.run_id, state=runner.state) from e
    except Exception as e:
        raise _conflict("inject_refused", str(e), after_aid=body.after_aid,
                        run_id=runner.run_id, state=runner.state) from e
    finally:
        resumed = runner.resume() if paused_here else False
    rows = [runner.plan.row(a) for a in inserted]
    return InjectOut(run_id=runner.run_id, state=runner.state, inserted=rows,
                     paused_for_inject=paused_here, resumed=resumed,
                     snapshot=runner.snapshot())


def _await_pause(runner: Any, timeout: float = INJECT_PAUSE_TIMEOUT_S) -> bool:
    """Wait for a requested pause to land, bounded. True if it did.

    A sync endpoint runs in the threadpool, so this blocks a worker thread and never the event
    loop — the e-stop route stays responsive throughout, which is the property that matters.
    """
    deadline = time.monotonic() + timeout
    while runner.state == "pausing" and time.monotonic() < deadline:
        time.sleep(_PAUSE_POLL_S)
    return runner.state == "paused"


# --- the event stream ---------------------------------------------------------

@ws_router.websocket("/ws/engine")
async def engine_stream(ws: WebSocket, since: int = Query(0, ge=0)) -> None:
    """Every engine event, in strictly increasing `seq`, as one JSON object per frame.

    Each frame is a member of the `Event` union — `type` discriminates, and the client generates
    its types from `event_adapter.json_schema()`. There is no envelope and no hello frame: the
    seed comes from `GET /api/engine/snapshot`, whose `seq` is what to pass as `?since=` here.

    `?since=<seq>` replays the bounded (~2000 event) buffer from that point, which covers a
    brief disconnect without a re-fetch. A `seq` older than the buffer simply starts wherever
    the buffer does, and the client notices the gap (`seq > last_seq + 1`) and re-snapshots —
    which is exactly why the snapshot exists and why the buffer is allowed to be bounded.
    """
    await ws.accept()
    sink = run_manager.sink
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()

    def _on_event(_event: Any) -> None:
        # Runs on the **runner's worker thread**. It must not touch the socket, and it must not
        # carry the event either: `seq` is the ordering authority and arrival order is not
        # delivery order, so the sender re-reads the sink in `seq` order instead.
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:  # loop already closed — the socket is going away anyway
            pass

    sink.subscribe(_on_event)
    watcher = asyncio.create_task(_await_close(ws))
    last_sent = since
    try:
        while not watcher.done():
            # Clear *before* draining: an event that arrives during the drain then re-sets the
            # flag and we loop again. Clearing afterwards would discard that wakeup and the
            # frame would sit in the buffer until the next unrelated event.
            wake.clear()
            for event in sink.since(last_sent):
                await ws.send_text(event.model_dump_json())
                last_sent = event.seq
            waiter = asyncio.ensure_future(wake.wait())
            try:
                await asyncio.wait({waiter, watcher}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sink.unsubscribe(_on_event)
        watcher.cancel()


async def _await_close(ws: WebSocket) -> None:
    """Resolve when the client goes away.

    Nothing on this socket is client-driven, but without a reader a disconnect is only noticed
    on the next send — and a run that has finished emits nothing, so the task would linger for
    the life of the process holding a subscription.
    """
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return


__all__ = ["ControlOut", "InjectOut", "InjectRequest", "LoadPlanRequest", "PreflightOut",
           "StartOut", "StartRequest", "router", "ws_router"]
