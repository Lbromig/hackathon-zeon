"""What an action handler is given, and the only way it may reach the outside world.

`actions.Handler` is deliberately loose (`Callable[..., OutputsBase]`) because the action
*models* must stay importable without the runner. This module pins the other half of that
signature: every handler is called as ``fn(action, ctx)`` where ``ctx`` is an
:class:`ActionContext`.

The point of routing everything through one object is that the runner keeps the
responsibilities a handler must not be able to forget:

* **logging** — ``ctx.log`` is already bound to this run and this action, so a handler
  cannot emit an unattributable record, and `GET /api/logs?aid=` finds it (R-LOG-5, D23).
* **pause/abort** — a handler that takes seconds must call :meth:`ActionContext.checkpoint`
  at its own natural boundaries. That is what makes pause cooperative *inside* a decap
  ratchet or a loop body rather than only between actions (D9).
* **speed** — tiers resolve here, against the device's own soft limits, so no handler
  invents a raw speed number (R-ENG-14).
* **artifacts** — recorded through ``ctx.artifact`` so the file lands under the run and
  shows up on the right action in the UI.

Handlers return an ``Outputs`` model. They do **not** build ``ActionResult``, emit events,
catch their own errors, or decide retries — the runner does all of that (§2.4).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from core import speeds

from .actions import Artifact, Warning_

if TYPE_CHECKING:  # pragma: no cover - typing only
    from drivers.capabilities.arm import ArmDriver
    from drivers.capabilities.liquid_handler import LiquidHandlerDriver

    from .actions import ActionBase
    from .blackboard import Blackboard


class ActionAborted(RuntimeError):
    """Raised out of :meth:`ActionContext.checkpoint` when the run is aborting.

    Distinct from a handler's own failure: the action did not fail, it was stopped. The
    runner turns this into ``status="aborted"`` rather than ``"failed"``, so an operator
    abort is never reported as a broken step.
    """


class DeviceAccess(Protocol):
    """The slice of the device manager a handler may use.

    A Protocol rather than the concrete manager so handler tests can pass a stub with two
    mock arms and nothing else.
    """

    def get(self, device_id: str) -> Any:
        """The driver for ``device_id``. Raises ``KeyError`` when it is not in the fleet."""

    def require_arm(self, device_id: str) -> "ArmDriver":
        """``get`` plus the assertion that it is an arm, so a handler need not check."""

    def require_liquid_handler(self, device_id: str) -> "LiquidHandlerDriver":
        """``get`` plus the assertion that it is a liquid handler."""

    def is_simulated(self, device_id: str) -> bool:
        """True when this device is backed by a mock/replay driver (D25)."""


@dataclass
class ActionContext:
    """Everything one action execution is allowed to touch."""

    run_id: str
    action: "ActionBase"
    devices: DeviceAccess
    blackboard: "Blackboard"
    log: logging.Logger
    #: True when *this action* ran against simulated devices, or when it is a pure
    #: computation in a simulated run. Set by the runner from resolved sim config — never
    #: inferred by a handler from a driver's vendor string, which is wrong for
    #: compute-only actions and raises on real drivers (D25).
    simulated: bool = False
    #: Directory this action's artifacts belong in. The runner creates it lazily.
    artifact_dir: str = ""

    _artifacts: list[Artifact] = field(default_factory=list, repr=False)
    _warnings: list[Warning_] = field(default_factory=list, repr=False)
    _abort: Any = None          # threading.Event, set on abort
    _pause: Any = None          # threading.Event, clear while paused
    _on_progress: Any = None    # Callable[[str, dict], None], set by the runner

    # --- cooperative control ------------------------------------------------
    def checkpoint(self) -> None:
        """A safe point to pause or abort. Call it between sub-steps of a long action.

        Blocks while the run is paused; raises :class:`ActionAborted` when it is aborting.
        Pause is deliberately *not* a mid-trajectory stop — a handler should call this
        between a decap bite and the next, or between loop iterations, never in the middle
        of a commanded move that the controller is already executing (D9).
        """
        if self._abort is not None and self._abort.is_set():
            raise ActionAborted(f"{self.action.kind} aborted at operator request")
        if self._pause is not None:
            self._pause.wait()
        if self._abort is not None and self._abort.is_set():
            raise ActionAborted(f"{self.action.kind} aborted at operator request")

    @property
    def aborting(self) -> bool:
        """True when an abort has been requested, without raising. For cleanup paths."""
        return self._abort is not None and self._abort.is_set()

    # --- reporting ----------------------------------------------------------
    def progress(self, message: str, **fields: Any) -> None:
        """Report intra-action progress: one waypoint of a traverse, one decap bite.

        Becomes an ``action_log`` event on the wire and an INFO record in the log file, so
        the UI can show movement inside a long action instead of a frozen row.
        """
        self.log.info(message, extra=fields or None)
        if self._on_progress is not None:
            self._on_progress(message, fields)

    def warn(self, code: str, message: str, *, device: str | None = None) -> Warning_:
        """Attach a non-fatal warning to this action's result.

        For the things that must be *reachable* rather than merely printed — an arm with no
        taught home, a position that is dead-reckoned rather than measured (R-LOG-6).
        """
        w = Warning_(code=code, message=message, device=device or self.action.device)
        self._warnings.append(w)
        self.log.warning("%s: %s", code, message, extra={"warning_code": code})
        return w

    def artifact(self, kind: str, path: str, *, camera: str | None = None,
                 label: str = "") -> Artifact:
        """Record a file this action produced (a frame, an overlay, a JSON dump)."""
        a = Artifact(kind=kind, path=path, camera=camera, label=label)
        self._artifacts.append(a)
        return a

    # --- resolved inputs ----------------------------------------------------
    def arm_speeds(self, device_id: str | None = None) -> speeds.Speeds:
        """This action's speed tier resolved for an arm, clamped by its soft limits.

        Reads ``limits`` off the driver so the clamp is the arm's own, not a global guess.
        """
        dev = device_id or self.action.device or ""
        limits = getattr(self.devices.get(dev), "limits", None) if dev else None
        return speeds.for_arm(getattr(self.action, "speed", None), limits)

    def lh_speeds(self, device_id: str | None = None) -> speeds.Speeds:
        """This action's speed tier resolved for a liquid handler."""
        return speeds.for_liquid_handler(getattr(self.action, "speed", None))

    # --- what the runner collects afterwards --------------------------------
    def collected_artifacts(self) -> list[Artifact]:
        return list(self._artifacts)

    def collected_warnings(self) -> list[Warning_]:
        return list(self._warnings)


__all__ = ["ActionAborted", "ActionContext", "DeviceAccess"]
