"""Reading the log back: a backwards tail, and a cursor-based forward read.

No index and no database. Filtering a few hundred thousand JSON lines backwards is
milliseconds, and an index is a second thing that can be wrong — and be wrong exactly when
you are reading the log because something else already went wrong.

The two access patterns are different and both are needed:

* **`tail()`** — "the last N records matching this filter". What the logs view opens with,
  and what `?aid=` uses to pull one action's records (R-UI-8). Reads backwards in blocks so
  the cost is proportional to what is returned, not to the file.
* **`read_since(cursor)`** — "what is new since I last asked". What the polled
  `GET /api/logs` uses. **No websocket** (S10): a follower thread plus rotation-inode
  handling is ~150 lines whose failure mode is invisible until you need the log, while this
  is a seek and a read.

Rotation is handled by making it *visible* rather than by pretending it did not happen. A
cursor carries the file's inode and size; if either says the file it was taken from is gone
or was truncated, the page comes back with `reset=True` and the newest records instead. A
byte offset into a rotated file is a wrong answer presented confidently, which is the one
thing a log reader must not do.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

BLOCK = 64 * 1024          # backwards read granularity
MAX_SCAN = 32 * 1024 * 1024  # give up rather than walk a 300 MiB file to satisfy one filter

LogRecord = dict[str, Any]
Predicate = Callable[[LogRecord], bool]


@dataclass
class LogPage:
    """A page of log records plus the cursor to ask for the next one.

    `reset` is not an error — it is the honest report that the file rotated or was
    truncated under the caller, so records between their cursor and this page are gone. A
    UI can say "log rotated" instead of silently showing a gap.
    """
    records: list[LogRecord] = field(default_factory=list)
    cursor: str = ""
    reset: bool = False
    scanned: int = 0            # records examined, so a caller can tell "no matches" from
                                # "nothing there at all"
    truncated: bool = False     # the backwards scan hit MAX_SCAN before `limit` was filled


def _cursor(path: str, offset: int) -> str:
    """`<inode>:<offset>`. The inode is what detects rotation; a bare offset cannot."""
    try:
        return f"{os.stat(path).st_ino}:{offset}"
    except OSError:
        return f"0:{offset}"


def _parse_cursor(value: str) -> tuple[int, int] | None:
    try:
        ino, offset = value.split(":", 1)
        return int(ino), int(offset)
    except (ValueError, AttributeError):
        return None


def _decode(line: bytes) -> LogRecord | None:
    """A JSONL line, or None if it is not one.

    A half-written last line is normal: the file is being appended to while it is read, and
    a record that is not yet complete is simply not yet a record. Dropping it silently is
    correct here — it will parse on the next poll.
    """
    line = line.strip()
    if not line:
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def make_predicate(*, run_id: str | None = None, aid: int | None = None,
                   level: str | None = None, event: str | None = None,
                   logger: str | None = None, device: str | None = None,
                   contains: str | None = None) -> Predicate:
    """The filter `GET /api/logs` exposes. Absent arguments do not constrain.

    `level` is a **minimum** severity, not an equality test: asking for warnings and being
    shown warnings but not errors is the kind of filter that hides the thing you were
    looking for.
    """
    import logging

    floor = getattr(logging, (level or "").upper(), None) if level else None
    needle = contains.lower() if contains else None

    def matches(record: LogRecord) -> bool:
        if run_id is not None and record.get("run_id") != run_id:
            return False
        if aid is not None and record.get("aid") != aid:
            return False
        if event is not None and record.get("event") != event:
            return False
        if device is not None and record.get("device") != device:
            return False
        if logger is not None and not str(record.get("logger", "")).startswith(logger):
            return False
        if floor is not None:
            severity = getattr(logging, str(record.get("level", "")).upper(), 0)
            if severity < floor:
                return False
        if needle is not None and needle not in json.dumps(record).lower():
            return False
        return True

    return matches


def tail(path: str, *, limit: int = 200, match: Predicate | None = None) -> LogPage:
    """The last `limit` matching records, oldest-first within the page.

    Reads backwards in `BLOCK`-sized chunks and stops as soon as `limit` matches are found,
    so pulling one action's records out of a large file costs what that action wrote, not
    what the file holds. Gives up at `MAX_SCAN` and says so via `truncated` rather than
    walking a 300 MiB file to satisfy a filter that may match nothing.
    """
    page = LogPage()
    if not os.path.exists(path):
        page.cursor = _cursor(path, 0)
        return page

    size = os.path.getsize(path)
    page.cursor = _cursor(path, size)
    found: list[LogRecord] = []
    remainder = b""
    position = size
    scanned_bytes = 0

    with open(path, "rb") as fh:
        while position > 0 and len(found) < limit and scanned_bytes < MAX_SCAN:
            step = min(BLOCK, position)
            position -= step
            scanned_bytes += step
            fh.seek(position)
            block = fh.read(step) + remainder
            lines = block.split(b"\n")
            # The first element may be a partial line whose start is in the previous block;
            # hold it over rather than trying to parse half a record.
            remainder = lines[0] if position > 0 else b""
            for line in reversed(lines[1:] if position > 0 else lines):
                record = _decode(line)
                if record is None:
                    continue
                page.scanned += 1
                if match is None or match(record):
                    found.append(record)
                    if len(found) >= limit:
                        break

    page.truncated = len(found) < limit and position > 0
    page.records = list(reversed(found))
    return page


def read_since(path: str, cursor: str, *, limit: int = 500,
               match: Predicate | None = None) -> LogPage:
    """Records appended after `cursor`, oldest-first.

    An empty cursor means "start from the newest page", so a first poll behaves like
    `tail()` rather than replaying the whole file. A cursor whose inode no longer matches,
    or whose offset is past the end, means the file rotated or was truncated: the page
    comes back with `reset=True` and the newest records, because a byte offset into a
    rotated file is a wrong answer presented confidently.
    """
    parsed = _parse_cursor(cursor)
    if parsed is None:
        return tail(path, limit=limit, match=match)
    if not os.path.exists(path):
        return LogPage(cursor=_cursor(path, 0))

    inode, offset = parsed
    stat = os.stat(path)
    if stat.st_ino != inode or offset > stat.st_size:
        page = tail(path, limit=limit, match=match)
        page.reset = True
        return page

    page = LogPage()
    records: list[LogRecord] = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        consumed = offset
        for line in fh:
            if not line.endswith(b"\n"):
                break            # a record still being written; leave the cursor before it
            consumed += len(line)
            record = _decode(line)
            if record is None:
                continue
            page.scanned += 1
            if match is None or match(record):
                records.append(record)
                if len(records) >= limit:
                    break
    page.records = records
    page.cursor = _cursor(path, consumed)
    return page
