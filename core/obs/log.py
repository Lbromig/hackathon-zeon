"""One structured log file, with every record attributable to its run and its action.

Three requirements at once (R-LOG-1/2/3): every action's inputs and outputs are in the
file, the frontend can read it, and a record can be attributed to the action that emitted
it. That is why the format is JSONL rather than text — structured inputs/outputs in a text
line means inventing a serialization, and a parser for it, that JSON already is. The
counter-argument ("you cannot read it with `tail`") is answered by also emitting a human
line to the console **through the same records**, so the two cannot disagree.

**The one thing to get right: context is bound on the HANDLERS, not on the root logger.**

`logging.Logger.addFilter` is not applied to records that *propagate* from a child logger —
`Logger.handle` only runs the filters of the logger the record was created on. So
`root.addFilter(ContextFilter())` stamps nothing at all for `logging.getLogger(
"engine.handlers.arm")`, which is every real caller. The whole per-action log drill-down
would return zero records while looking implemented. `Handler.handle` *does* run its own
filters on every record it receives, propagated or not. Hence: one `ContextFilter` per
handler. (D23 / review B7. There is a test for exactly this, because the broken version
is silent.)

The context itself lives in `contextvars`, which are per-thread — and the engine's runner
*is* one thread (D1) — so a handler that calls into `drivers/` gets driver-level records
stamped with the action that caused them for free. That is what makes "expand an action to
see the logs it produced" exact rather than approximate.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterator

# --- the bound context -------------------------------------------------------

# Fields stamped onto every record. `aid` is the action's stable identity (never its
# display index, which injection renumbers — R-ENG-4), and `index` is carried alongside
# only so a human reading the file does not have to join against the plan.
LOG_CONTEXT_FIELDS: tuple[str, ...] = ("run_id", "aid", "index", "action_kind", "device")

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("hz_run_id", default=None)
_aid: contextvars.ContextVar[int | None] = contextvars.ContextVar("hz_aid", default=None)
_index: contextvars.ContextVar[int | None] = contextvars.ContextVar("hz_index", default=None)
_kind: contextvars.ContextVar[str | None] = contextvars.ContextVar("hz_action_kind", default=None)
_device: contextvars.ContextVar[str | None] = contextvars.ContextVar("hz_device", default=None)

_VARS = {
    "run_id": _run_id, "aid": _aid, "index": _index,
    "action_kind": _kind, "device": _device,
}

# Process-monotonic record counter. Not a substitute for the event protocol's `seq` (D24):
# this one exists so the log tail can hand a client a stable "you have seen up to here"
# marker within one process lifetime, and so two records written in the same millisecond
# have a defined order.
_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def bind(**fields: Any) -> dict[str, Any]:
    """Set context fields, returning the previous values so a caller can restore them.

    Prefer `action_context`; this is the primitive it is built from, for the runner's
    set-at-the-top-of-each-attempt / reset-in-a-finally pattern.
    """
    previous: dict[str, Any] = {}
    for name, value in fields.items():
        var = _VARS.get(name)
        if var is None:
            raise KeyError(f"{name!r} is not a log context field; expected {LOG_CONTEXT_FIELDS}")
        previous[name] = var.get()
        var.set(value)
    return previous


def clear() -> None:
    """Drop all context. Records emitted after this carry no run or action."""
    for var in _VARS.values():
        var.set(None)


def current_context() -> dict[str, Any]:
    """The context as it would be stamped right now. For tests and for `ActionResult`."""
    return {name: var.get() for name, var in _VARS.items() if var.get() is not None}


@contextlib.contextmanager
def action_context(**fields: Any) -> Iterator[None]:
    """Bind context for the duration of a block, restoring it afterwards.

    Restores rather than clears, so nesting works: an action inside a run keeps the run's
    `run_id` when the action's own block exits.
    """
    previous = bind(**fields)
    try:
        yield
    finally:
        for name, value in previous.items():
            _VARS[name].set(value)


class ContextFilter(logging.Filter):
    """Stamps the bound context and a sequence number onto every record it sees.

    Attached to **handlers**, never to a logger — see the module docstring. Returns True
    always: it is a decorator, not a gate.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _VARS.items():
            value = var.get()
            # setattr unconditionally, including None, so the formatter and any downstream
            # handler see a uniform record shape rather than having to use getattr defaults.
            if not hasattr(record, name) or getattr(record, name) is None:
                setattr(record, name, value)
        if not hasattr(record, "seq"):
            record.seq = _next_seq()
        return True


# --- formatting --------------------------------------------------------------

# Attributes stdlib puts on every record. Anything *else* on a record is caller-supplied
# `extra=` and is merged into the JSON object, which is how an action's typed inputs and
# outputs get into the file (R-LOG-1) without a bespoke encoding.
_STANDARD = frozenset((
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "thread",
    "threadName", "taskName",
))

# Reserved `event` values, so a consumer can filter structurally instead of by message
# text. A record with no `event` is an ordinary diagnostic line.
EVENTS: tuple[str, ...] = (
    "run_start", "run_end", "action_start", "action_output", "action_error",
    "action_warning", "plan_mutated", "readiness", "device_state", "camera_event",
)


def _json_safe(value: Any) -> Any:
    """Make a value survive `json.dumps`, non-finite floats included.

    A NaN raised inside a log call is a log line that eats an exception — and NaN is
    exactly what a failed detection or a bad readback produces, so it reaches the logger
    precisely when the log matters most. Reusing `main._json_safe`'s lesson.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if hasattr(value, "model_dump"):          # a pydantic model, e.g. typed outputs
        try:
            return _json_safe(value.model_dump(mode="json"))
        except Exception:
            pass
    return repr(value)


class JsonlFormatter(logging.Formatter):
    """One JSON object per line: the record, its bound context, and any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
                          .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "seq": getattr(record, "seq", 0),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name in LOG_CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        for key, value in record.__dict__.items():
            # Context fields are handled above and are omitted when unset — the filter
            # setattrs them as None for a uniform record shape, and a record that carried
            # `"run_id": null` would make "no run" indistinguishable from a bug.
            if key in LOG_CONTEXT_FIELDS or key in _STANDARD or key in payload:
                continue
            if key == "seq" or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=repr)


class ConsoleFormatter(logging.Formatter):
    """A human line carrying the same context, for the terminal.

    Deliberately derived from the same record as the JSONL line rather than from a second
    log call, so the two can never disagree about what happened.
    """

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        bits = [stamp, f"{record.levelname:<7}", record.name]
        aid = getattr(record, "aid", None)
        if aid is not None:
            bits.append(f"[#{getattr(record, 'index', '?')}/aid{aid}]")
        device = getattr(record, "device", None)
        if device:
            bits.append(f"({device})")
        line = " ".join(bits) + " " + record.getMessage()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# --- setup -------------------------------------------------------------------

_configured = False
_lock = threading.Lock()


def configure(*, path: str, level: str = "INFO", console: bool = True,
              max_bytes: int = 64 * 1024 * 1024, backup_count: int = 5,
              force: bool = False) -> str:
    """Install the JSONL file handler (+ optional console) on the root logger.

    Called once, early, from the app lifespan — and from tests, which pass `force=True`
    with a tmp path. Returns the resolved log path.

    Takes the path as an argument rather than reading `core.config`: this module has to be
    importable by `core.config` itself without a cycle, and an explicit path is also what
    lets a test point it somewhere harmless.

    Idempotent by default. Repeated `configure()` calls (uvicorn `--reload` re-executes the
    lifespan) would otherwise stack handlers and write every record N times, which reads as
    a bug in whatever produced the records.
    """
    global _configured
    with _lock:
        if _configured and not force:
            return path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_hz_owned", False):
                root.removeHandler(handler)
                handler.close()

        # Size-based, not time-based: an image-heavy run produces a burst, and a daily
        # rotation either loses a busy afternoon or keeps a month of idle files (R-LOG-7).
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        file_handler.setFormatter(JsonlFormatter())
        _install(root, file_handler)

        if console:
            stream = logging.StreamHandler()
            stream.setFormatter(ConsoleFormatter())
            _install(root, stream)

        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        # uvicorn/asyncio access logs are noise in a bench log and would dominate the
        # per-action view. They stay reachable at DEBUG.
        for noisy in ("uvicorn.access", "asyncio", "multipart", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True
        return path


def _install(root: logging.Logger, handler: logging.Handler) -> None:
    """Attach a handler with its OWN context filter, and mark it ours.

    The filter goes on the handler because a logger-level filter is not applied to records
    propagated from child loggers — the whole point of D23. `_hz_owned` lets a re-configure
    replace our handlers without touching one a test or pytest's caplog installed.
    """
    handler.addFilter(ContextFilter())
    handler._hz_owned = True                      # type: ignore[attr-defined]
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """A module logger. Exists so callers do not have to import `logging` to get one, and
    so the import points at this module — which is where the conventions are written down."""
    return logging.getLogger(name)
