"""The process-wide holder for the engine's current run (W4).

One process, one runner at a time, and **one :class:`EventSink` for the whole process** — the
two facts every choice in here follows from.

Why one sink rather than one per run
------------------------------------
`/ws/engine` subscribes to a sink. If each `Runner` brought its own, loading a second plan
would silently orphan every connected socket: the tab would sit there showing a finished run
while the bench moved. So the sink is owned here and *passed into* each runner
(`Runner(..., sink=self.sink)`), which also means:

* `EventSink.since(seq)` replays across a plan swap, so a client that reconnects at the moment
  the plan changed catches up instead of falling back to a snapshot;
* `seq` is the process-wide `events.PROCESS_SEQUENCE` (B6) and a new run continues it. A client
  keeps **one** `last_seq` for the socket, forever, and re-fetches the snapshot when `run_id`
  changes — it never resets `last_seq`.

Why loading a plan during a live run is refused (B8)
---------------------------------------------------
`Runner.start` only refuses if *its own* worker is alive, and a fresh runner knows nothing
about the old one. Replacing the runner while a handler is parked in `ctx.checkpoint()` mid
decap leaves that worker alive forever, holding `left` in `device_claims` — the next run's
first gripper action then raises `DeviceBusy`, is classified retriable, retries, and fails
again. The UI would show a new plan while the bench held a cap half unscrewed. So an active
run refuses the load and says to abort first; and on an accepted load the previous runner is
aborted, joined, and its claims are checked before the new one is built.

Nothing here blocks boot. `boot_init` runs the initialization plan (D5/R-INIT: initialization
is a *plan* on the same runner, so it gets indices, per-action logs and pause for free) on a
daemon thread, so a device that hangs cannot stop the API coming up (R-START-7).
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable, Literal

from core.config import settings
from core.obs import get_logger

from ..engine.events import PlanReplaced, RunSnapshot, RunState
from ..engine.plan import Plan
from ..engine.plans import handover
from ..engine.runner import EventSink, Runner, device_claims
from .device_access import DeviceGateway
from .device_manager import device_manager

log = get_logger(__name__)

PlanName = Literal["handover", "startup"]

#: The named plans `POST /api/engine/plan` will load. Deliberately a closed set: a plan is a
#: sequence of real motions, and "load whatever JSON the client posted" is not a hackathon
#: feature — injection is the reviewed path for adding an action to a plan.
PLAN_BUILDERS: dict[str, Callable[..., list[Any]]] = {
    "handover": handover.build,
    "startup": handover.build_startup,
}

#: Run states in which the engine owns the bench, so the plan must not be swapped under it
#: (B8). `paused` is in the set on purpose: a paused run's worker is *alive* and still holds
#: its device claim — that is the exact leak the guard exists for.
ACTIVE_STATES: frozenset[str] = frozenset({"preflight", "running", "pausing", "paused"})

#: How long to wait for an aborted runner's worker to exit before giving up on it. A handler
#: parked in `checkpoint()` wakes immediately; one blocking in a driver call finishes that
#: call first, and 10 s covers a slow real move.
ABORT_JOIN_TIMEOUT_S = 10.0


class RunActive(RuntimeError):
    """A request that would disturb a live run. The API turns it into 409 + this reason."""

    def __init__(self, reason: str, *, state: RunState, run_id: str) -> None:
        self.reason = reason
        self.state = state
        self.run_id = run_id
        super().__init__(reason)


class NoRunLoaded(RuntimeError):
    """No plan has been loaded yet. The API turns it into 404 + this reason."""


class RunManager:
    """Owns the current :class:`Runner` and the one event sink the socket reads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sink = EventSink()
        self._runner: Runner | None = None

    # --- accessors -----------------------------------------------------------
    @property
    def sink(self) -> EventSink:
        """Stable for the lifetime of the process, so a socket outlives a plan swap."""
        return self._sink

    @property
    def runner(self) -> Runner | None:
        with self._lock:
            return self._runner

    def require(self) -> Runner:
        runner = self.runner
        if runner is None:
            raise NoRunLoaded(
                "no plan is loaded — POST /api/engine/plan {\"name\": \"handover\"} first")
        return runner

    def state(self) -> RunState:
        runner = self.runner
        return runner.state if runner is not None else "idle"

    def is_active(self) -> bool:
        return self.state() in ACTIVE_STATES

    # --- projections ---------------------------------------------------------
    def snapshot(self) -> RunSnapshot:
        """The reconnect path's payload, including before any plan is loaded.

        An empty snapshot rather than a 404, because a client mounting before the operator has
        loaded anything still needs `seq` to seed from and `simulated` for the reality banner
        (D25/D29) — and a UI that has to special-case "no run yet" tends to special-case it by
        showing nothing at all.
        """
        runner = self.runner
        if runner is None:
            return RunSnapshot(seq=self._sink.last_seq, simulated=simulated_map())
        return runner.snapshot()

    # --- plan loading --------------------------------------------------------
    def load(self, name: str, *, home_after: bool = True) -> Runner:
        """Build a named plan, pre-flight it, and make it the current run (B8's guard).

        Emits `plan_replaced` and then `readiness` on the shared sink, so a client that is
        already connected sees the new plan and the new `run_id` without polling. The snapshot
        this returns is current as of a `seq` after both.
        """
        builder = PLAN_BUILDERS.get(name)
        if builder is None:
            raise KeyError(name)
        with self._lock:
            current = self._runner
            if current is not None and current.state in ACTIVE_STATES:
                raise RunActive(
                    f"a run is {current.state} ({current.run_id}); loading a plan now would "
                    f"leave its worker thread alive holding "
                    f"{sorted(device_claims.held()) or 'its devices'} and the next run's first "
                    f"move would be refused as busy. Abort the run first "
                    f"(POST /api/engine/abort).",
                    state=current.state, run_id=current.run_id)
            if current is not None:
                # A finished run: nothing to disturb, but be explicit rather than trusting the
                # state — abort is idempotent and join returns at once for a dead worker.
                current.abort()
                current.join(ABORT_JOIN_TIMEOUT_S)
            held = device_claims.held()
            if held:
                raise RunActive(
                    f"devices are still claimed by the previous run ({held}); a new plan would "
                    f"fail its first move as busy. This is a leaked worker thread, not an "
                    f"operator error — restart the backend.",
                    state=self.state(), run_id=current.run_id if current else "")
            actions = builder(home_after) if name == "startup" else builder()
            plan = Plan(actions, name=name)
            runner = Runner(plan, devices=gateway(), sink=self._sink, name=name)
            self._runner = runner
        # Outside the lock: emitting fans out to subscribers, and a slow consumer must not be
        # able to hold the manager's lock. `seq` still orders them (it is allocated in `emit`).
        self._sink.emit(PlanReplaced, revision=plan.revision, cursor=plan.cursor,
                        actions=plan.row_payload())
        runner.preflight()
        log.info("loaded plan %s as %s (%d actions)", name, runner.run_id, len(plan),
                 extra={"event": "plan_mutated", "run_id": runner.run_id})
        return runner

    # --- lifecycle -----------------------------------------------------------
    def boot_init(self, *, force: bool = False) -> threading.Thread | None:
        """Run the initialization plan at boot, on a daemon thread (D5/R-INIT/R-START-7).

        Initialization is the `startup` plan on the same runner as everything else, so the
        Workflow tab shows it with per-device outcomes and the operator can watch it instead of
        reading the log. Three properties keep it from being a boot hazard:

        * **it is never awaited** — a UVC open can block uninterruptibly on macOS and an arm
          controller can sit in a TCP connect, so the API must come up regardless (R-START-7);
        * **every failure is swallowed here** — `lifecycle.initialize` already records a
          per-device outcome and one device failing never stops the rest (R-INIT-1), and a
          refused pre-flight is logged rather than raised;
        * **it does not run under pytest**, for the same reason `startup_snapshot` does not: a
          test that never asked for a run must not find one live, holding device claims, when
          it asserts. Tests that want it call `boot_init(force=True)`.

        Disable with `HZ_ENGINE_INIT=0`.
        """
        if not force:
            if os.getenv("HZ_ENGINE_INIT", "1").strip().lower() in ("0", "false", "no"):
                log.info("boot initialization disabled (HZ_ENGINE_INIT)")
                return None
            if "pytest" in sys.modules:
                return None

        def _work() -> None:
            try:
                runner = self.load("startup")
                # `allow_degraded`: initialization is what *discovers* a degraded bench, so an
                # advisory problem must not be the thing that stops it from running.
                runner.start(allow_degraded=True)
            except Exception as e:
                log.warning("boot initialization did not start: %s", e,
                            extra={"event": "run_start"})

        thread = threading.Thread(target=_work, name="engine-boot-init", daemon=True)
        thread.start()
        return thread

    def shutdown(self, timeout: float = ABORT_JOIN_TIMEOUT_S) -> None:
        """Stop the current run before the drivers it is holding go away.

        Without this, `device_manager.disconnect_all()` tears down (and brakes) an arm while a
        worker thread is still commanding it.
        """
        with self._lock:
            runner = self._runner
        if runner is None:
            return
        if runner.state in ACTIVE_STATES:
            log.warning("aborting %s (%s) for shutdown", runner.run_id, runner.state,
                        extra={"event": "run_end", "run_id": runner.run_id})
            runner.abort()
        runner.join(timeout)

    def reset(self) -> None:
        """Drop the current run. For tests, and for nothing else."""
        with self._lock:
            runner = self._runner
            self._runner = None
        if runner is not None:
            runner.abort()
            runner.join(ABORT_JOIN_TIMEOUT_S)


def simulated_map() -> dict[str, bool]:
    """Per-device reality from resolved configuration (D25), for a snapshot with no runner."""
    ids = [str(e.get("id")) for e in settings.fleet if e.get("id")]
    return {device: bool(settings.is_simulated(device)) for device in sorted(ids)}


def gateway() -> DeviceGateway:
    """A fresh `DeviceAccess` over the live driver set.

    Built per run rather than cached: `device_manager.load_fleet()` replaces every driver, and
    a gateway captured before that would hand handlers the disconnected ones.
    """
    return DeviceGateway(device_manager, simulated=simulated_map())


#: The one holder. Process-wide for the same reason `device_claims` is: the question "what is
#: the engine doing right now?" has one answer.
run_manager = RunManager()

__all__ = ["ACTIVE_STATES", "NoRunLoaded", "PLAN_BUILDERS", "PlanName", "RunActive",
           "RunManager", "gateway", "run_manager", "simulated_map"]
