"""Observability: the one structured log, and how to read it back.

`log` configures it (one JSONL file, one writer, context bound on the handlers).
`tail` reads it backwards with a cursor, which is what `GET /api/logs` serves.
"""
from .log import (LOG_CONTEXT_FIELDS, ContextFilter, JsonlFormatter, action_context,
                  bind, clear, configure, current_context, get_logger)
from .tail import LogPage, LogRecord, read_since, tail

__all__ = [
    "configure", "get_logger", "bind", "clear", "action_context", "current_context",
    "ContextFilter", "JsonlFormatter", "LOG_CONTEXT_FIELDS",
    "tail", "read_since", "LogPage", "LogRecord",
]
