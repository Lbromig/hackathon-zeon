"""The handler package — importing it is what registers every action handler.

`actions.handler(kind)` builds a registry at import time, and `actions.handler_for(kind)`
returns ``None`` for a kind nobody claimed. R-ENG-17 turns that ``None`` into "this step
cannot run" rather than a silent skip, so *when* the modules get imported is load-bearing:
the runner's pre-flight has to be able to see the whole registry, and the readiness report
has to be able to say an engine is incomplete instead of failing at step 7. Importing this
package is that moment.

Slice ownership (see `docs/v2/WORK_BREAKDOWN.md`)
------------------------------------------------
`arm` and `lifecycle` are S2's and are imported outright. The other three modules belong to
other Wave-1 slices — `camera` to S3, `liquid_handler` to S4, `vision` to S5 — and may not
exist yet. They are picked up when they land, without an edit to this file, because a slice
being blocked on a merge to a shared ``__init__`` is exactly the cross-slice coupling the
work breakdown exists to prevent.

The presence check is `find_spec`, deliberately, **not** a bare ``except ImportError``. A
module that exists but has a broken import inside it must fail loudly: swallowing that would
un-register a handler and turn every action of its kind into "cannot run", which reads as an
unimplemented engine rather than as the bug it is.
"""
from __future__ import annotations

import importlib
import importlib.util

from . import arm, lifecycle  # noqa: F401  -- imported for the registration side effect

#: Handler modules owned by the other Wave-1 slices, in `ACTION_KINDS` order.
PENDING_MODULES: tuple[str, ...] = ("camera", "liquid_handler", "vision")

#: Which of those were actually found and imported. Read by the readiness report, so
#: "the engine has no vision handlers yet" is answerable without catching an exception.
LOADED_MODULES: tuple[str, ...] = ("arm", "lifecycle")

for _name in PENDING_MODULES:
    if importlib.util.find_spec(f"{__name__}.{_name}") is not None:
        importlib.import_module(f"{__name__}.{_name}")
        LOADED_MODULES += (_name,)

__all__ = ["arm", "lifecycle", "PENDING_MODULES", "LOADED_MODULES"]
