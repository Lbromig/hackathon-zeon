"""`GET /api/logs` — the frontend's read path onto the single log file (R-LOG-4).

Polled with a cursor, **no websocket** (review S10). A log follower thread plus
rotation-inode handling is ~150 lines whose failure mode is invisible until the moment you
need the log; this is a seek and a read. The Logs tab polls, and an expanded action asks
for `?aid=` once.

Two shapes come out of one endpoint because they are the same query with a different
starting point:

* no `cursor` — the newest `limit` records matching the filter. What the Logs tab opens
  with, and what "show me this action's records" is.
* a `cursor` from a previous response — only what has been appended since.

`reset` in the response is not an error: it says the file rotated or was truncated under
the caller, so records between their cursor and this page are gone. A UI can say "log
rotated" instead of silently rendering a gap.
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.config import settings
from core.obs import get_logger
from core.obs.tail import make_predicate, read_since, tail

log = get_logger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])

# Bounded so one request cannot ask the server to parse the whole file into memory.
MAX_LIMIT = 2000


class LogsResponse(BaseModel):
    """A page of records, oldest-first, plus the cursor for the next poll."""
    records: list[dict] = Field(default_factory=list)
    cursor: str = ""
    reset: bool = False
    scanned: int = 0
    truncated: bool = False
    path: str = ""


@router.get("", response_model=LogsResponse)
def read_logs(
    cursor: str = Query("", description="cursor from a previous response; empty = newest page"),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
    run_id: str | None = Query(None),
    aid: int | None = Query(None, description="action identity — the stable one, not the index"),
    level: str | None = Query(None, description="minimum severity, not an equality test"),
    event: str | None = Query(None, description="structural filter, e.g. action_output"),
    logger: str | None = Query(None, description="logger-name prefix, e.g. engine.handlers"),
    device: str | None = Query(None),
    contains: str | None = Query(None, description="case-insensitive substring over the record"),
) -> LogsResponse:
    match = make_predicate(run_id=run_id, aid=aid, level=level, event=event,
                           logger=logger, device=device, contains=contains)
    path = settings.log_file
    page = read_since(path, cursor, limit=limit, match=match) if cursor \
        else tail(path, limit=limit, match=match)
    return LogsResponse(records=page.records, cursor=page.cursor, reset=page.reset,
                        scanned=page.scanned, truncated=page.truncated, path=path)
