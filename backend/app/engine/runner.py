"""The runner: one worker thread, one action wrapper, and the three controls an operator has.

**One thread executes actions; asyncio is only ever the fan-out (D1).** The xArm SDK, cv2 and
pyserial all block, and `move_joints(wait=True)` blocks for seconds while holding no lock of
its own. Making this async would buy nothing and add a failure mode where one missing `await`
freezes the pause control — which is the one control that must never freeze. So: a worker
thread runs the plan, every event is stamped with a monotonic `seq` and pushed into an
:class:`EventSink`, and S8's websocket drains that sink from the event loop.

Everything a handler must not have to remember lives in :meth:`Runner._attempt` (R-LOG-5):
resolve the handler, build the `ActionContext`, bind the log context, time it, catch and
classify what comes out, apply `on_failure`/`max_attempts`, collect artifacts and warnings off
the context, decide `simulated` from **resolved configuration** (D25 — never from a driver's
vendor string, which raises on real drivers and reports "real" for a pure computation in a
fully simulated run), build the `ActionResult`, and emit `action_started`/`action_finished`.
A handler returns `Outputs` and nothing else. That is what makes the logging requirement
structural rather than a convention nobody breaks on purpose.

The three controls, and why they are three
------------------------------------------
* **Pause is cooperative** (D9/R-ENG-8). A request flips the state to `pausing` immediately
  and closes the gate. The action in flight finishes, and the run stops at the next boundary —
  or sooner, if the handler calls :meth:`ActionContext.checkpoint` between its own sub-steps
  (a decap bite, a traverse leg). `pausing` is not a UI nicety: while a multi-second move
  completes, "paused" would be a lie about where the arm is. The moment a handler actually
  parks on the gate the state becomes `paused`, because then nothing *is* moving — see
  :class:`_PauseGate`, which exists to make that distinction honest rather than inferred.
* **Abort is the separate hard path** (R-ENG-10). It sets the abort event and opens the gate so
  a parked handler wakes and raises `ActionAborted`, which becomes `status="aborted"` — never
  `"failed"`. An operator stopping a run must never read as a broken step.
* **Injection targets an identity and may land at the cursor** (D22/R-ENG-12/13). A refusal is
  an `inject_rejected` event carrying the reason, because silently inserting elsewhere is the
  failure R-ENG-13 forbids, and the operator who tried it may not be the one watching.

The loop's termination test is the named predicate `offset_within_threshold` on `watch_slot`,
compared against `threshold_mm`, plus `no_progress_abort` and `max_iterations`. It terminates
on the **remaining offset** and never on inter-view disagreement (Q4/D16): views that never
agree that closely would keep a perfectly converged loop running forever. Four honest
outcomes, no fifth: `converged`, `stalled`, `exhausted`, `aborted`.
"""
from __future__ import annotations

import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Collection

from core.config import settings
from core.obs import log as obs

from .actions import (ActionBase, ActionResult, CheckpointOutputs, ErrorInfo, LogRef, Loop,
                      LoopOutputs, OUTPUTS_FOR_KIND, OutputsBase, Warning_, handler_for)
from .blackboard import PER_CAMERA_SLOTS, Blackboard
from .context import ActionAborted, ActionContext, DeviceAccess
from .events import (ActionFinished, ActionLog, ActionStarted, EventBase, EventSequence,
                     InjectRejected, LoopIteration, PlanReplaced, ReadinessChanged,
                     ReadinessState, RunFinished, RunPaused, RunResumed, RunSnapshot,
                     RunStarted, RunState, RunStateChanged)
from .plan import (ENGINE_KINDS, InjectRefused, MaterializationCapped, Plan, PreflightReport,
                   params_of)

log = obs.get_logger(__name__)

#: How much a loop's remaining offset must shrink for the iteration to count as progress.
#: Below this the change is not distinguishable from detection noise, and counting noise as
#: progress is what turns `no_progress_abort` into a loop that never aborts (O9/R-VIS-7).
NO_PROGRESS_EPSILON_MM = 0.05

#: Per-action ceiling on `action_log` events. The socket is a convenience mirror of the log
#: file, not the log transport (see `ActionLog`): a handler that logs in a tight loop must not
#: be able to drown the event stream, and `GET /api/logs` always has the complete record.
MAX_ACTION_LOG_EVENTS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    """Enough of a JSON coercion for an inputs record. Images never get here — an artifact is
    a path (R-LOG-8) — so this only has to survive pydantic models and numbers."""
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return repr(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _attr(value: Any, name: str) -> Any:
    """Read a field off typed outputs *or* off the plain dict a stub writes.

    The loop's predicate has to work against `OffsetOutputs` in production and against a
    dict in a test that scripts convergence without the vision slice. One accessor, so the
    two cannot diverge.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


# --- errors ------------------------------------------------------------------

class StartRefused(RuntimeError):
    """Pre-flight found something blocking, so the run does not begin (R-ENG-2/16).

    Carries the report, so the caller can render every problem rather than the first one:
    an operator fixing a bench wants the whole list.
    """

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        super().__init__(report.reason() or "start refused")


class NoHandlerError(RuntimeError):
    """No handler is registered for this kind, so the step cannot run (R-ENG-17).

    An exception rather than a skip. A silently skipped step reads downstream as "it ran",
    which is the failure mode that requirement exists to prevent.
    """


class DeviceBusy(RuntimeError):
    """Another action already holds this device's claim."""


# --- events ------------------------------------------------------------------

class EventSink:
    """Stamps, buffers and fans out events. Thread-safe (D1: the worker emits, asyncio reads).

    Two halves of D24 meet here: every event gets a monotonic `seq` from one
    :class:`EventSequence`, and a bounded replay buffer lets a client that reconnects with a
    small gap catch up without a full snapshot. A client whose `seq` is older than the buffer
    re-fetches the snapshot instead — which is why the snapshot exists and why the buffer is
    allowed to be bounded.

    **Subscribers are called on the emitting thread**, which is usually the worker. An asyncio
    consumer must therefore hop the loop itself (`loop.call_soon_threadsafe`); doing that here
    would tie this module to an event loop it deliberately does not know about. Since the API
    thread also emits (an injection refusal, a plan mutation), two events can reach a
    subscriber out of order under contention: **`seq` is the ordering authority**, not arrival.
    """

    def __init__(self, *, run_id: str = "", sequence: EventSequence | None = None,
                 keep: int = 2000) -> None:
        self.run_id = run_id
        self._sequence = sequence or EventSequence()
        self._keep = keep
        self._lock = threading.Lock()
        self._events: list[EventBase] = []
        self._subscribers: list[Callable[[EventBase], None]] = []
        self._last_seq = 0

    def emit(self, event_type: type[EventBase], **fields: Any) -> EventBase:
        with self._lock:
            event = event_type(seq=self._sequence.next(), ts=_now(),
                               run_id=self.run_id, **fields)
            self._events.append(event)
            self._last_seq = event.seq
            if len(self._events) > self._keep:
                del self._events[:len(self._events) - self._keep]
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:  # a broken consumer must not stop the run
                log.exception("event subscriber raised on %s", event.type)
        return event

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def events(self) -> tuple[EventBase, ...]:
        with self._lock:
            return tuple(self._events)

    def since(self, seq: int) -> list[EventBase]:
        """Events after `seq`, oldest first. Empty when there is nothing newer; a caller that
        cannot tell "nothing newer" from "fell off the buffer" compares against
        :attr:`last_seq` and re-fetches the snapshot."""
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    def subscribe(self, fn: Callable[[EventBase], None]) -> Callable[[EventBase], None]:
        with self._lock:
            self._subscribers.append(fn)
        return fn

    def unsubscribe(self, fn: Callable[[EventBase], None]) -> None:
        with self._lock:
            if fn in self._subscribers:
                self._subscribers.remove(fn)


# --- cooperative pause -------------------------------------------------------

class _PauseGate:
    """A `threading.Event` stand-in that tells the runner when a handler actually parks.

    `ActionContext.checkpoint` calls `wait()` on this. The difference between "a pause was
    requested and a move is still finishing" and "the handler has stopped and nothing is
    moving" is the difference between `pausing` and `paused`, and it is only observable from
    inside `wait()`. Without this the state would sit at `pausing` while a handler was parked
    indefinitely — technically true, operationally a lie.

    Set means go. Cleared means pause, which is the polarity `ActionContext` expects
    (`_pause` is "clear while paused").
    """

    def __init__(self, on_park: Callable[[], None], on_release: Callable[[], None]) -> None:
        self._event = threading.Event()
        self._event.set()
        self._on_park = on_park
        self._on_release = on_release

    def wait(self, timeout: float | None = None) -> bool:
        if self._event.is_set():
            return True
        self._on_park()
        try:
            return self._event.wait(timeout)
        finally:
            self._on_release()

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()


# --- device claims -----------------------------------------------------------

class DeviceClaims:
    """Advisory, non-blocking record of which action holds which device.

    Its purpose is to let the teach API refuse hand-jogging an arm the engine is currently
    moving. Two properties are deliberate and must not be "improved":

    * **It never blocks.** A second claim raises :class:`DeviceBusy` immediately rather than
      waiting, so nothing here can ever be the thing an operator's request is queued behind.
    * **The e-stop path must not consult it** (D21/R-ENG-11). `POST /api/arms/{id}/stop`
      already bypasses the teach lock on purpose and carries a "do not fix that" comment.
      Emergency stop has to be reachable *while an action is blocking* — that is the only
      time it matters — so no claim, lock or queue introduced by the engine may gate it. If
      you are adding a check to that route because "the engine owns the arm right now": that
      is the regression D21 exists to prevent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}

    def claim(self, device: str, holder: str) -> None:
        with self._lock:
            current = self._held.get(device)
            if current is not None and current != holder:
                raise DeviceBusy(f"{device!r} is claimed by {current}")
            self._held[device] = holder

    def release(self, device: str, holder: str) -> None:
        with self._lock:
            if self._held.get(device) == holder:
                del self._held[device]

    def holder(self, device: str) -> str | None:
        with self._lock:
            return self._held.get(device)

    def held(self) -> dict[str, str]:
        with self._lock:
            return dict(self._held)


#: Process-wide, because the teach API and the engine are in one process and the question
#: "is the engine moving this arm right now?" has one answer.
device_claims = DeviceClaims()


# --- the runner --------------------------------------------------------------

class Runner:
    """Executes one plan on one worker thread.

    Constructed with a plan and a `DeviceAccess`; `start()` pre-flights and spawns the thread.
    Everything else — pause, resume, abort, inject, snapshot — is called from another thread
    (the API's) and is safe to call at any point in a run.
    """

    def __init__(self, plan: Plan, *, devices: DeviceAccess,
                 run_id: str | None = None, name: str = "",
                 blackboard: Blackboard | None = None,
                 sink: EventSink | None = None,
                 artifact_root: str | None = None,
                 is_simulated: Callable[[str | None], bool] | None = None,
                 known_devices: Collection[str] | None = None,
                 teach_path: str | None = None) -> None:
        self.plan = plan
        self.run_id = run_id or f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.name = name or plan.name or "run"
        self.blackboard = blackboard or Blackboard()
        self.sink = sink or EventSink(run_id=self.run_id)
        self.sink.run_id = self.run_id
        self._devices = devices
        self._artifact_root = artifact_root if artifact_root is not None else settings.artifact_dir
        # D25: resolved configuration, not the driver. `settings.is_simulated(None)` answers
        # for a pure-computation action by reporting the *run's* reality, which is the case
        # the vendor-string approach got wrong in the dangerous direction (R-SIM-6).
        self._is_simulated = is_simulated or settings.is_simulated
        self._known_devices = known_devices
        self._teach_path = teach_path

        self._state: RunState = "idle"
        self._state_lock = threading.RLock()
        self._abort = threading.Event()
        self._gate = _PauseGate(self._on_park, self._on_release)
        self._worker: threading.Thread | None = None
        self._stack: list[int] = []          # aids currently executing, outermost first
        self._halted = False
        self._announced_pause = False
        self._pause_reason: str = "operator"
        self._pause_aid: int | None = None
        self._started = False
        self._started_at = ""
        self._begun = 0.0
        self._readiness: ReadinessState = "initializing"
        self._warnings: list[Warning_] = []
        self._artifact_dir_made = False
        self._log_events = 0

    # --- state ---------------------------------------------------------------
    @property
    def state(self) -> RunState:
        with self._state_lock:
            return self._state

    @property
    def running_aid(self) -> int | None:
        """The innermost action executing right now — a loop's child while it runs, the loop
        itself between its children. This is what the injection rule is checked against."""
        stack = list(self._stack)
        return stack[-1] if stack else None

    @property
    def cursor(self) -> int:
        return self.plan.cursor

    @property
    def readiness(self) -> ReadinessState:
        return self._readiness

    @property
    def warnings(self) -> list[Warning_]:
        return list(self._warnings)

    def _set_state(self, state: RunState) -> None:
        """Transition and announce, but only on a real change.

        Idempotent because two paths set `paused` — the action boundary and a handler parking
        in `checkpoint()` — and a duplicate `run_state` event would make a client's gap
        detection see movement where there was none.
        """
        with self._state_lock:
            if self._state == state:
                return
            self._state = state
        self.sink.emit(RunStateChanged, state=state)

    # --- lifecycle -----------------------------------------------------------
    def preflight(self) -> PreflightReport:
        """Check the plan and publish the result as readiness (R-ENG-16/R-INIT-6)."""
        report = self.plan.preflight(known_devices=self._known_devices,
                                     teach_path=self._teach_path)
        self._readiness = report.readiness
        self._warnings = report.as_warnings()
        self.sink.emit(ReadinessChanged, state=self._readiness, warnings=self._warnings,
                       devices=self.simulated_map())
        return report

    def start(self, *, allow_degraded: bool = False, skip_preflight: bool = False) -> PreflightReport:
        """Pre-flight, then run the plan on a worker thread.

        Refuses on a blocking problem, and on an advisory one unless the caller confirms
        (R-ENG-2: `failed` is refused with a stated reason, `degraded` needs an explicit
        confirmation naming the degradations). The reason travels on `StartRefused.report`,
        so the API can render every problem rather than the first.
        """
        if self._worker is not None and self._worker.is_alive():
            raise RuntimeError("this run is already executing")
        self._set_state("preflight")
        report = PreflightReport() if skip_preflight else self.preflight()
        if report.blocking or (report.advisory and not allow_degraded):
            self._set_state("failed")
            raise StartRefused(report)
        self._begun = time.monotonic()
        self._started_at = _now()
        self._spawn()
        return report

    def _spawn(self) -> None:
        self._worker = threading.Thread(target=self._run, name=f"engine-{self.run_id}",
                                        daemon=True)
        self._worker.start()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker to stop. True when it has."""
        worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def run_to_completion(self, timeout: float | None = 30.0, **start_kwargs: Any) -> RunState:
        """Start and wait — the synchronous form, for tests and for `just` one-shot runs."""
        self.start(**start_kwargs)
        self.join(timeout)
        return self.state

    # --- controls ------------------------------------------------------------
    def pause(self, reason: str = "operator") -> bool:
        """Request a pause (D9). Returns False when there is nothing to pause.

        Takes effect at the next boundary — or at the handler's next `checkpoint()`. The state
        becomes `pausing` immediately, which is the honest answer while a commanded move
        finishes: this is deliberately **not** a mid-trajectory `driver.stop()`. That is
        abort, and it is a different, riskier operation (R-ENG-10).
        """
        if self.state not in ("running", "pausing"):
            return False
        self._pause_reason = reason
        self._pause_aid = self.running_aid
        self._gate.clear()
        self._set_state("pausing")
        return True

    def resume(self) -> bool:
        """Continue from where the pause landed (R-ENG-9).

        Also the recovery path after a failure: R-ENG-15 leaves a halted plan inspectable and
        injectable, so resuming a `failed` run continues at the cursor — which is immediately
        after the action that failed, and therefore exactly where an injected fix lands (D22).
        The worker thread has already exited by then, so it is restarted.
        """
        if self._abort.is_set() or not self._started:
            # Not started means pre-flight refused it: there is nothing to resume, and
            # resuming would run a plan whose blocking problems were never fixed.
            return False
        if self.state not in ("pausing", "paused", "failed"):
            return False
        needs_worker = self._worker is None or not self._worker.is_alive()
        if needs_worker and self.plan.cursor >= len(self.plan):
            return False          # halted on the last action; nothing left to continue into
        self._halted = False
        self._announced_pause = False
        self._gate.set()
        self._set_state("running")
        self.sink.emit(RunResumed)
        if needs_worker:
            self._spawn()
        return True

    def abort(self) -> bool:
        """Stop the run — the hard path, distinct from pause (R-ENG-10).

        Sets the abort flag and opens the gate, so a handler parked in `checkpoint()` wakes
        and raises `ActionAborted`. That becomes `status="aborted"`, never `"failed"`: an
        operator stopping a run is not a broken step.

        It does **not** stop a move already commanded to a controller. Emergency stop is the
        operator's own control and must stay reachable at all times (D21/R-ENG-11) — no claim
        or lock in this module gates it.
        """
        if self.state in ("complete", "aborted"):
            return False
        self._abort.set()
        self._gate.set()          # wake anything parked, so it can raise
        return True

    def inject(self, action: Any, *, after_aid: int | None = None) -> list[ActionBase]:
        """Add an action to the plan after `after_aid` (R-ENG-12/13, D22).

        Landing at the cursor is allowed and is the point of the feature: a failure leaves the
        run exactly there. A long move in flight is not a reason to refuse. Anything that
        cannot be honoured exactly is **refused with a stated reason** — as an
        `inject_rejected` event as well as a raise, because the refusal has to be visible to
        every connected client and the operator who tried it may not be the one watching.

        The run is not paused as a side effect: the caller (S8's endpoint) owns the
        pause/inject/resume sequence R-ENG-12 describes, because only it knows whether the
        operator asked for one action or three.
        """
        reason = self.plan.refusal_for_insert(after_aid, cursor=self.plan.cursor,
                                              running_aid=self.running_aid)
        if reason:
            self.sink.emit(InjectRejected, reason=reason, after_aid=after_aid)
            raise InjectRefused(reason, after_aid)
        try:
            inserted = self.plan.insert_after(after_aid, action, origin="inject")
        except (InjectRefused, MaterializationCapped) as e:
            self.sink.emit(InjectRejected, reason=str(e), after_aid=after_aid)
            raise
        except Exception as e:
            self.sink.emit(InjectRejected, reason=str(e), after_aid=after_aid)
            raise InjectRefused(str(e), after_aid) from e
        log.info("injected %s after aid %s", [a.kind for a in inserted], after_aid,
                 extra={"event": "plan_mutated", "run_id": self.run_id})
        self._emit_plan()
        return inserted

    # --- projections ---------------------------------------------------------
    def simulated_map(self) -> dict[str, bool]:
        """Per-device reality, from resolved configuration (D25/D29).

        On `RunStarted` and on the snapshot both, so a client that never saw the run start
        still knows — unmissably — whether this is real. With simulation the default, that is
        a correctness requirement rather than chrome.
        """
        ids = self._known_devices
        if ids is None:
            ids = [str(e.get("id")) for e in settings.fleet if e.get("id")]
        return {device: bool(self._is_simulated(device)) for device in sorted(ids)}

    def snapshot(self) -> RunSnapshot:
        """The full current state, for a reconnect or a second tab (D24/R-ENG-18).

        `seq` is read **before** the rows, deliberately. If an event lands in between, the rows
        already include its effect and the client will apply it again — and every event that
        carries state (`plan_replaced`, `action_finished`) is absolute rather than a delta, so
        re-applying is a no-op. Reading `seq` afterwards would have the opposite error, which
        is not recoverable: the client would skip an event it never saw.
        """
        return self.plan.snapshot(
            seq=self.sink.last_seq, run_id=self.run_id, state=self.state,
            started_at=self._started_at, readiness=self._readiness,
            warnings=self._warnings, simulated=self.simulated_map())

    def _emit_plan(self) -> None:
        self.sink.emit(PlanReplaced, revision=self.plan.revision, cursor=self.plan.cursor,
                       actions=self.plan.row_payload())

    # --- the worker ----------------------------------------------------------
    def _run(self) -> None:
        if not self._started:
            self._started = True
            self.sink.emit(RunStarted, name=self.name, action_count=len(self.plan),
                           simulated=self.simulated_map())
            self._emit_plan()
        self._set_state("running")
        try:
            while True:
                if not self._boundary():
                    break
                action = self.plan.at(self.plan.cursor)
                if action is None:
                    break
                result = self._execute(action)
                # Advance by identity, not by incrementing: an injection may have renumbered
                # everything after the insertion point while this action ran (R-ENG-4).
                if isinstance(action, Loop):
                    self.plan.cursor = self.plan.region_of(action.aid)[1]
                else:
                    self.plan.cursor = self.plan.index_of(action.aid) + 1
                if result.status == "aborted":
                    # A handler raising `ActionAborted` on its own — a driver that noticed an
                    # e-stop, say — aborts the *run*, not just the step. Setting the flag here
                    # is what keeps the run from reporting `complete` with an aborted action
                    # in it, and it is also what stops a resume walking past the abort.
                    self._abort.set()
                    break
                if self._abort.is_set():
                    break
                if result.status == "failed" and action.on_failure == "halt":
                    self._halted = True
                    break
        except BaseException:
            # A crash in the runner itself is not a plan failure; record it and stop, but
            # never leave the state at `running` with no thread behind it.
            log.exception("engine worker crashed", extra={"run_id": self.run_id})
            self._halted = True
        finally:
            self._finish()

    def _finish(self) -> None:
        if self._abort.is_set():
            status: RunState = "aborted"
        elif self._halted or self.plan.failed():
            status = "failed"
        else:
            status = "complete"
        self._set_state(status)
        self.sink.emit(RunFinished, status=status, completed=self.plan.completed(),
                       failed=self.plan.failed(),
                       duration_ms=int((time.monotonic() - self._begun) * 1000))

    def _boundary(self) -> bool:
        """The action boundary: honour a pause here, and report an abort. False = stop.

        This is the boundary D9 names. A pause request that arrived mid-action lands here,
        after the action in flight has finished — never in the middle of a trajectory the
        controller is already executing.
        """
        if self._abort.is_set():
            return False
        if not self._gate.is_set():
            self._announce_pause()
            self._gate.wait()
            if self._abort.is_set():
                return False
            self._set_state("running")
        return True

    def _announce_pause(self) -> None:
        if self._announced_pause:
            return
        self._announced_pause = True
        self._set_state("paused")
        self.sink.emit(RunPaused, reason=self._pause_reason,  # type: ignore[arg-type]
                       aid=self._pause_aid if self._pause_aid is not None else self.running_aid)

    def _on_park(self) -> None:
        """A handler has stopped inside `checkpoint()`. Nothing is moving, so say `paused`."""
        self._announce_pause()

    def _on_release(self) -> None:
        if not self._abort.is_set() and self._gate.is_set():
            self._set_state("running")

    # --- one action ----------------------------------------------------------
    def _execute(self, action: ActionBase) -> ActionResult:
        """Run one action, with bounded retries (R-ENG-15).

        The attempt count is `max_attempts` — bounded and recorded per attempt, because a
        retry loop around a move that fails for a structural reason is a machine repeating a
        mistake. `on_failure="retry"` asks for at least one retry, so it is not a no-op
        against the default `max_attempts=1`; the field's own ceiling still applies.
        """
        attempts = action.max_attempts
        if action.on_failure == "retry":
            attempts = max(attempts, 2)
        result: ActionResult | None = None
        for attempt in range(1, attempts + 1):
            result = self._attempt(action, attempt)
            self.plan.record_result(result)
            self.sink.emit(ActionFinished, aid=action.aid, index=action.index, result=result)
            if result.status != "failed" or self._abort.is_set():
                break
            if attempt < attempts:
                log.warning("attempt %d/%d of aid %d failed, retrying: %s", attempt, attempts,
                            action.aid, result.error.message if result.error else "")
        assert result is not None
        if result.status == "failed" and action.on_failure == "halt":
            # R-ENG-15: the failure is recorded on the action and the run stops there with the
            # plan inspectable and injectable. `continue` is the deliberate opt-out, per
            # action, and must not produce a pause event for a run that is still going.
            self.sink.emit(RunPaused, reason="action_failed", aid=action.aid)
        return result

    def _attempt(self, action: ActionBase, attempt: int) -> ActionResult:
        """One attempt at one action: the wrapper a handler cannot forget (R-LOG-5)."""
        self.plan.set_state(action.aid, "running")
        self._stack.append(action.aid)
        self._log_events = 0
        started_at = _now()
        began = time.monotonic()
        simulated = bool(self._is_simulated(action.device))
        inputs = self._inputs_for(action)
        status: str = "complete"
        outputs: OutputsBase | None = None
        error: ErrorInfo | None = None
        holder = f"aid{action.aid}"

        with obs.action_context(run_id=self.run_id, aid=action.aid, index=action.index,
                               action_kind=action.kind, device=action.device):
            logger = obs.get_logger(f"engine.action.{action.kind}")
            ctx = ActionContext(
                run_id=self.run_id, action=action, devices=self._devices,
                blackboard=self.blackboard, log=logger, simulated=simulated,
                artifact_dir=self._artifact_dir(),
                _abort=self._abort, _pause=self._gate,
                _on_progress=lambda msg, fields, aid=action.aid: self._emit_action_log(aid, msg),
            )
            self.sink.emit(ActionStarted, aid=action.aid, index=action.index,
                           kind=action.kind, device=action.device, attempt=attempt,
                           inputs=inputs)
            logger.info("start %s", action.label or action.kind,
                        extra={"event": "action_start", "attempt": attempt, "inputs": inputs,
                               "simulated": simulated})
            try:
                claimed = self._claim(action, holder)
                try:
                    outputs = self._dispatch(action, ctx)
                finally:
                    if claimed:
                        device_claims.release(str(action.device), holder)
                outputs = self._check_outputs(action, outputs)
                status, error = self._grade(action, outputs)
            except ActionAborted as e:
                # The separate hard path (R-ENG-10). Never "failed": the action did not
                # break, it was stopped.
                status = "aborted"
                error = ErrorInfo(type="ActionAborted", message=str(e), retriable=False,
                                  device=action.device)
                logger.warning("aborted: %s", e, extra={"event": "action_error"})
            except Exception as e:
                status = "failed"
                error = ErrorInfo(type=type(e).__name__, message=str(e) or repr(e),
                                  retriable=isinstance(e, (TimeoutError, DeviceBusy)),
                                  device=action.device)
                logger.exception("failed: %s", e, extra={"event": "action_error"})
            finally:
                self._stack.pop()

            duration_ms = int((time.monotonic() - began) * 1000)
            result = ActionResult(
                aid=action.aid, index=action.index, kind=action.kind, device=action.device,
                status=status,  # type: ignore[arg-type]
                attempt=attempt, started_at=started_at, finished_at=_now(),
                duration_ms=duration_ms, simulated=simulated, inputs=inputs,
                outputs=outputs, artifacts=ctx.collected_artifacts(),
                warnings=ctx.collected_warnings(), error=error,
                log_ref=LogRef(run_id=self.run_id, aid=action.aid))
            logger.info("%s in %d ms", status,
                        duration_ms, extra={"event": "action_output", "status": status,
                                            "outputs": _jsonable(outputs)})
        return result

    def _dispatch(self, action: ActionBase, ctx: ActionContext) -> OutputsBase:
        """Resolve and call. Control flow is the engine's; everything else is a handler's."""
        if action.kind in ENGINE_KINDS:
            if isinstance(action, Loop):
                return self._run_loop(action, ctx)
            return self._run_checkpoint(action, ctx)
        fn = handler_for(action.kind)
        if fn is None:
            raise NoHandlerError(
                f"no handler registered for {action.kind}, so this step cannot run. Reported "
                f"rather than skipped: a silently skipped step reads downstream as 'it ran' "
                f"(R-ENG-17).")
        return fn(action, ctx)

    def _check_outputs(self, action: ActionBase, outputs: Any) -> OutputsBase:
        """A handler returning the wrong model is a bug that must not reach the UI silently."""
        expected = OUTPUTS_FOR_KIND.get(action.kind)
        if expected is None:                       # unreachable while OUTPUTS_FOR_KIND is total
            raise NoHandlerError(f"{action.kind} has no outputs model")
        if not isinstance(outputs, expected):
            raise TypeError(f"{action.kind} handler returned {type(outputs).__name__}, "
                            f"expected {expected.__name__}")
        return outputs

    def _grade(self, action: ActionBase, outputs: OutputsBase) -> tuple[str, ErrorInfo | None]:
        """Turn outputs into a status, for the kinds where "returned" is not "succeeded".

        A loop is the case that matters: it returns normally whatever happened, and only its
        `outcome` says whether the tube got aligned. `stalled` and `exhausted` are failures of
        the run — the default `on_failure="halt"` then stops it with the plan inspectable —
        while `aborted` stays the operator's own path.
        """
        if isinstance(outputs, LoopOutputs) and outputs.outcome != "converged":
            if outputs.outcome == "aborted" and self._abort.is_set():
                return "aborted", ErrorInfo(
                    type="ActionAborted", message="loop aborted at operator request",
                    device=action.device)
            detail = ("a step inside the loop failed" if self._halted else
                      f"remaining offset {outputs.final_magnitude_mm} mm against a "
                      f"{outputs.threshold_mm} mm threshold")
            return "failed", ErrorInfo(
                type="LoopNotConverged",
                message=f"loop {outputs.outcome} after {outputs.iterations} iteration(s); "
                        f"{detail}",
                retriable=False, device=action.device)
        return "complete", None

    def _claim(self, action: ActionBase, holder: str) -> bool:
        if not action.device:
            return False
        device_claims.claim(str(action.device), holder)
        return True

    def _inputs_for(self, action: ActionBase) -> dict[str, Any]:
        """What this action was asked to do, for the record and the log (R-LOG-1).

        `from_slot` names where the numbers come from, and the slot's value is recorded
        alongside — so the record shows the offset the move was derived from, not only the
        slot name. Resolving the slot into `dx/dy/dz` stays the handler's job: the runner
        does not know what a given slot's value means for a given device.
        """
        inputs = params_of(action)
        inputs["label"] = action.label
        slot = getattr(action, "from_slot", None)
        if slot:
            try:
                value = self.blackboard.peek(
                    slot, device=action.device if slot in PER_CAMERA_SLOTS else None)
            except Exception:
                value = None
            inputs["from_slot_value"] = _jsonable(value)
        return inputs

    def _artifact_dir(self) -> str:
        """This run's artifact directory, created on first use.

        One directory per run rather than per action: an action's artifacts are already
        attributable through `Artifact.label`/`camera` and the result they hang off, and a
        directory per action leaves hundreds of empty ones behind a servo loop.
        """
        path = os.path.join(self._artifact_root, self.run_id)
        if not self._artifact_dir_made:
            try:
                os.makedirs(path, exist_ok=True)
                self._artifact_dir_made = True
            except OSError as e:
                log.warning("cannot create artifact dir %s: %s", path, e)
        return path

    def _emit_action_log(self, aid: int, message: str) -> None:
        if self._log_events >= MAX_ACTION_LOG_EVENTS:
            return
        self._log_events += 1
        self.sink.emit(ActionLog, aid=aid, level="INFO", msg=message)

    # --- control flow kinds --------------------------------------------------
    def _run_checkpoint(self, action: ActionBase, ctx: ActionContext) -> CheckpointOutputs:
        """A pause the plan asked for, not the pause button (D9).

        Implemented through the same gate as the button, so there is one pause mechanism and
        one place it can be wrong. `acknowledged` is True once the operator resumed.
        """
        message = getattr(action, "message", "") or "checkpoint"
        self._pause_reason = "checkpoint"
        self._pause_aid = action.aid
        self._gate.clear()
        self._set_state("pausing")
        ctx.progress(f"checkpoint: {message}")
        ctx.checkpoint()          # parks on the gate; raises ActionAborted on abort
        return CheckpointOutputs(acknowledged=True, message=message)

    def _run_loop(self, loop: Loop, ctx: ActionContext) -> LoopOutputs:
        """Materialize and run a bounded loop (D8/D14), terminating on the remaining offset.

        Every iteration's body becomes real indexed rows before it runs, so the workflow tab
        can show what iteration 4 saw (R-VIS-8). The bounds are all three of D14's: the loop's
        `max_iterations`, the global materialized ceiling, and the refusal of nested loops —
        the first of them to bite ends the loop as `exhausted` rather than growing the plan.

        Termination is `offset_within_threshold` on `watch_slot` against `threshold_mm`
        (Q4/D16). Inter-view disagreement is never a gate: a loop gated on views agreeing can
        run forever after it has converged.
        """
        iteration = 0
        materialized = 0
        best: float | None = None
        no_progress = 0
        magnitude: float | None = None
        outcome = "aborted"

        while True:
            if self._abort.is_set():
                outcome = "aborted"
                break
            if not self._boundary():
                outcome = "aborted"
                break
            if iteration >= loop.max_iterations:
                outcome = "exhausted"
                break
            try:
                copies = self.plan.materialize_iteration(loop.aid, iteration + 1)
            except MaterializationCapped as e:
                ctx.log.warning("loop bounded: %s", e, extra={"event": "plan_mutated"})
                outcome = "exhausted"
                break
            iteration += 1
            materialized += len(copies)
            self._emit_plan()

            # Drive off "the first row in this loop's region that has not run", re-read every
            # step, so an action injected into this iteration is executed rather than stepped
            # over — a skipped injection is a silent skip wearing an accepted request's
            # clothes (R-ENG-17).
            while True:
                nxt = self.plan.next_unrun_in_region(loop.aid)
                if nxt is None:
                    break
                if not self._boundary():
                    break
                self.plan.cursor = self.plan.index_of(nxt.aid)
                child = self._execute(nxt)
                if child.status == "aborted":
                    break
                if child.status == "failed" and nxt.on_failure == "halt":
                    self._halted = True
                    break

            if self._abort.is_set():
                outcome = "aborted"
                break
            if self._halted:
                outcome = "aborted"
                break

            magnitude, sigma = self._watch(loop, ctx)
            improving = (magnitude is not None
                         and (best is None or magnitude < best - NO_PROGRESS_EPSILON_MM))
            self.sink.emit(LoopIteration, aid=loop.aid, iteration=iteration,
                           magnitude_mm=magnitude, sigma_mm=sigma,
                           threshold_mm=loop.threshold_mm, improving=improving)
            if magnitude is not None and magnitude <= loop.threshold_mm:
                outcome = "converged"
                break
            if improving:
                no_progress = 0
                best = magnitude
            else:
                no_progress += 1
                if no_progress >= loop.no_progress_abort:
                    outcome = "stalled"
                    break

        return LoopOutputs(outcome=outcome,  # type: ignore[arg-type]
                           iterations=iteration, materialized=materialized,
                           final_magnitude_mm=magnitude, threshold_mm=loop.threshold_mm)

    def _watch(self, loop: Loop, ctx: ActionContext) -> tuple[float | None, dict[str, float]]:
        """The remaining offset the loop terminates on, and its reported uncertainty.

        `None` means "not observed", which is **not** zero: an unobserved offset must never
        read as a converged one, so it counts as no progress and the loop stalls rather than
        declaring success (R-VIS-4/O9).
        """
        if loop.watch_slot in PER_CAMERA_SLOTS:
            # The three per-camera slots hold one value per view; there is no single
            # magnitude to compare. Refusing to guess which view to believe is D15.
            ctx.warn("watch_slot_per_camera",
                     f"loop watches {loop.watch_slot!r}, which is per-camera and has no "
                     f"single magnitude; the loop cannot terminate on it")
            return None, {}
        value = self.blackboard.peek(loop.watch_slot)
        sigma_raw = _attr(value, "sigma_mm") or {}
        sigma = {str(k): float(v) for k, v in sigma_raw.items()
                 if isinstance(v, (int, float))} if isinstance(sigma_raw, dict) else {}
        magnitude = _attr(value, "magnitude_mm")
        if magnitude is None:
            residual = _attr(value, "residual_offset_mm")
            if isinstance(residual, dict) and residual:
                # An axis reported as `None` is unobservable, and `None` is not 0.0 — treating
                # it as zero is what turns "nobody can see the z offset" into "z is already
                # aligned", and the loop would converge on an offset it never measured
                # (R-VIS-4). So one missing axis makes the whole magnitude unknown.
                if all(isinstance(v, (int, float)) for v in residual.values()):
                    magnitude = math.sqrt(sum(float(v) ** 2 for v in residual.values()))
        if magnitude is None:
            return None, sigma
        try:
            magnitude = float(magnitude)
        except (TypeError, ValueError):
            return None, sigma
        if not math.isfinite(magnitude):
            return None, sigma
        return magnitude, sigma


__all__ = [
    "DeviceBusy", "DeviceClaims", "EventSink", "MAX_ACTION_LOG_EVENTS",
    "NO_PROGRESS_EPSILON_MM", "NoHandlerError", "Runner", "StartRefused", "device_claims",
]
