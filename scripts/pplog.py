#!/usr/bin/env python3
"""Pretty-print JSONL log records from stdin. Backs `just logs`.

The log file is JSONL because R-LOG-1/3 need structured inputs, outputs and per-action
attribution, and because `GET /api/logs` filters on fields rather than on a text format that
will change. The usual objection to that is "you cannot read it with `tail`", and this is the
answer — 30 lines, not a change of format.

A line that is not JSON is printed verbatim rather than dropped: if something is writing
non-JSON into the log, that is exactly what you want to see.
"""
from __future__ import annotations

import json
import sys

CONTEXT = ("run_id", "aid", "index", "action_kind", "device")


def render(record: dict) -> str:
    stamp = str(record.get("ts", ""))[11:23]           # HH:MM:SS.mmm
    head = f"{stamp} {record.get('level', ''):<7} {record.get('logger', '')}"
    aid = record.get("aid")
    if aid is not None:
        head += f" [#{record.get('index', '?')}/aid{aid}]"
    if record.get("device"):
        head += f" ({record['device']})"
    line = f"{head} {record.get('msg', '')}"
    # Everything that is not the envelope or the context is caller-supplied structure —
    # an action's inputs and outputs. Show it, compactly, on a continuation line.
    extra = {k: v for k, v in record.items()
             if k not in CONTEXT and k not in ("ts", "level", "logger", "msg", "seq", "exc")}
    if extra:
        line += "\n    " + json.dumps(extra, ensure_ascii=False)[:400]
    if record.get("exc"):
        line += "\n" + str(record["exc"])
    return line


def main() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            print(raw)               # not JSON: show it, do not hide it
            continue
        print(render(record) if isinstance(record, dict) else raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
