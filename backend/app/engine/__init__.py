"""The execution engine's frozen contracts.

`actions` (the `Action` union, typed per-kind outputs, `ActionResult`, the handler
registry), `events` (the websocket protocol, with a monotonic sequence number) and
`blackboard` (exactly five named slots) were frozen at the end of Wave 0. Seven parallel
slices import them concurrently; a change here invalidates other slices' assumptions and is
the one thing that cannot be merged cleanly. If a slice needs one of these changed, it stops
and reports it rather than editing across the boundary.

`plan.py` and `runner.py` hold the behaviour: an indexed, mutable `Plan` and the single-worker
`Runner` that executes it with pause / resume / inject / abort. They are re-exported here so a
caller does not have to know which module a name lives in.

Import order matters: `plan` and `runner` depend on the contracts above, so the contract
imports come first. `context` is imported after them because `runner` constructs it.
"""
from .actions import (ACTION_MODELS, ACTION_KINDS, Action, ActionBase, ActionResult,
                      Artifact, ErrorInfo, LogRef, MAX_MATERIALIZED_ACTIONS, OUTPUTS_FOR_KIND,
                      Outputs, SlotName, SpeedTier, Warning_, handler, handler_for,
                      registered_kinds, validate_action)
from .blackboard import SLOT_NAMES, Blackboard, SlotEmpty
from .context import ActionAborted, ActionContext, DeviceAccess
from .events import (Event, EventBase, EventSequence, RunSnapshot, ActionState,
                     RunState, event_adapter)
from .plan import (Plan, PlanError, PreflightProblem, PreflightReport, InjectRefused,
                   MaterializationCapped, NestedLoopRefused, UnknownActionError, preflight)
from .runner import (DeviceBusy, EventSink, NoHandlerError, Runner, StartRefused,
                     device_claims)

__all__ = [
    # actions
    "Action", "ActionBase", "ACTION_MODELS", "ACTION_KINDS", "validate_action",
    "Outputs", "OUTPUTS_FOR_KIND", "ActionResult", "Artifact", "ErrorInfo", "Warning_",
    "LogRef", "SpeedTier", "SlotName", "MAX_MATERIALIZED_ACTIONS",
    "handler", "handler_for", "registered_kinds",
    # events
    "Event", "EventBase", "EventSequence", "RunSnapshot", "ActionState", "RunState",
    "event_adapter",
    # blackboard
    "Blackboard", "SLOT_NAMES", "SlotEmpty",
    # context (the runner <-> handler seam)
    "ActionContext", "ActionAborted", "DeviceAccess",
    # plan
    "Plan", "preflight", "PreflightReport", "PreflightProblem", "PlanError",
    "InjectRefused", "MaterializationCapped", "NestedLoopRefused", "UnknownActionError",
    # runner
    "Runner", "EventSink", "StartRefused", "NoHandlerError", "DeviceBusy", "device_claims",
]
