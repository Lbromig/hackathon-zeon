"""`GET /api/runs/{run_id}/artifacts/{name}` — serve a file an action produced.

The overlays the Workflow tab shows next to a solved offset, and the frames a
`camera.snapshot` wrote. Artifacts are referenced by path and never inlined into a record
(R-LOG-8), so something has to serve the file; this is that something, and it is the only
route in the backend that turns client-supplied text into a filesystem path.

Which is the whole reason it looks like this:

* **The run directory is derived, not given.** `settings.artifact_dir / run_id`, with `run_id`
  required to be a single path segment. `Runner._artifact_dir` uses exactly the same join, so
  the two cannot drift.
* **`name` is a basename, not a path.** No separator, no `..`, no drive letter, and the
  resolved file must still be inside the run directory after `realpath` — which is what closes
  the symlink route that a purely textual check leaves open.
* **Every rejection is a 404**, not a 403. "That name is not allowed" and "that file is not
  there" are the same answer to a client, and the difference is only useful to someone probing.

`Artifact.url` is empty today (`ActionContext.artifact` never sets it), so a client builds the
URL from `run_id` plus `os.path.basename(artifact.path)`. That is stated here because it is the
contract the Workflow tab depends on.
"""
from __future__ import annotations

import mimetypes
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.config import settings
from core.obs import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

#: Artifacts are written once and never rewritten under the same name — a fresh capture gets a
#: fresh file — so the browser may keep them. Without this, a twelve-iteration servo loop's
#: overlay strip re-fetches two dozen images on every re-render.
_CACHE_CONTROL = "public, max-age=3600"


def _one_segment(value: str) -> bool:
    """True when `value` is a plain name: no separator, no traversal, no root."""
    if not value or value in (".", ".."):
        return False
    if value != os.path.basename(value):
        return False
    return not ("/" in value or "\\" in value or os.path.isabs(value))


def artifact_path(run_id: str, name: str) -> str:
    """The absolute path of one artifact, or raise 404.

    Separate from the route so the traversal rules are testable without a client, and so any
    future consumer (a zip of a run, say) cannot re-derive them slightly differently.
    """
    if not _one_segment(run_id) or not _one_segment(name):
        raise HTTPException(404, "no such artifact")
    root = os.path.realpath(settings.artifact_dir)
    run_dir = os.path.realpath(os.path.join(root, run_id))
    if run_dir != root and not run_dir.startswith(root + os.sep):
        raise HTTPException(404, "no such artifact")
    path = os.path.realpath(os.path.join(run_dir, name))
    # After resolution, not before: a symlink inside the run directory pointing at
    # /etc/passwd passes every textual check there is.
    if not path.startswith(run_dir + os.sep) or not os.path.isfile(path):
        raise HTTPException(404, "no such artifact")
    return path


@router.get("/{run_id}/artifacts/{name}")
def artifact(run_id: str, name: str) -> FileResponse:
    """One artifact of one run, by basename. 404 for anything that is not a file inside it."""
    path = artifact_path(run_id, name)
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type,
                        headers={"Cache-Control": _CACHE_CONTROL})


__all__ = ["artifact_path", "router"]
