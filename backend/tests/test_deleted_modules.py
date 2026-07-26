"""The deletion gate: nothing may import a module the v2 scope reduction removed.

Why this is a test and not a code review. The deletion pass removed ~40 % of the
application surface, and the failure mode it can leave behind is not a broken import —
Python only raises on an import that actually *executes*. A stale
`from core.worldmodel import ...` inside a lazily-imported branch, a `try:` fallback, or a
frontend `fetch("/api/worldmodel")` sits there compiling fine and fails on the bench. So
the check is a grep over the source tree, run by pytest, per
`IMPLEMENTATION_PLAN.md` Phase 1 exit criteria ("verified by a grep gate in CI, not by eye").

It is also the guard against scope creeping back (`§0.3`): re-adding any of these is now a
deliberate act that turns the suite red, not an accident.
"""
from __future__ import annotations

import os
import re

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# --- what was deleted --------------------------------------------------------

# Python modules/packages removed in the deletion pass. Written as import paths; the
# patterns below match both `import a.b` and `from a.b import c` forms.
DELETED_PYTHON_MODULES: tuple[str, ...] = (
    # digital twin / world model
    "core.worldmodel",
    "core.kinematics",
    "core.perception.fusion",
    "core.perception.projection",
    "backend.app.services.twin",
    "backend.app.services.twin_fusion",
    "backend.app.services.kinematics",
    # calibration pipeline + the world-map renderer it fed
    "core.calibration",
    "core.viz",
    "backend.app.api.calibration",
    # twin-based verification
    "core.verification",
    # the three superseded executors
    "backend.app.agent",
    "backend.app.api.agent",
    "backend.app.workflows",
    "backend.app.api.workflow",
    "core.sequences",
    "backend.app.api.sequences",
    # planners and path teaching
    "core.motion.pick_place",
    "core.motion.adapters",
    "core.motion.path_teach",
    "core.teach_paths",
    "backend.app.services.path_recorder",
    # bench diagnostics
    "scripts.validate_camera",
    "scripts.validate_motion",
    "scripts.find_joint_limit",
    "scripts.gripper_sequence",
)

# Frontend modules removed. Matched as import specifiers, so a relative path from any
# depth still hits (`../../api/worldmodel`, `./SequenceBuilder.vue`, ...).
DELETED_FRONTEND_MODULES: tuple[str, ...] = (
    "api/sequences",
    "api/worldmodel",
    "components/worldmap/WorldMapTab.vue",
    "components/InstrumentPanel.vue",
    "components/WorkflowRunner.vue",
    "teach/PathTeach.vue",
    "teach/SequenceBuilder.vue",
    "teach/TeachChecklist.vue",
    "composables/useWorkflow",
    "PathTeach.vue",
    "SequenceBuilder.vue",
    "TeachChecklist.vue",
)

# HTTP routes removed with their routers. A frontend call to one of these compiles and
# type-checks perfectly and then 404s at runtime, which is exactly what a grep catches
# and a type-checker does not.
DELETED_ROUTES: tuple[str, ...] = (
    "/api/worldmodel",
    "/api/sequences",
    "/api/workflow",
    "/api/agent",
    "/ws/calibrate",
    "/ws/workflow",
    "/ws/agent",
    "/paths",
    "worldmap.html",
)

# Directories searched. `temp/` and `docs/` are excluded on purpose: recorded fixtures and
# the historical design documents legitimately mention what was deleted, and a gate that
# forbids *writing about* a deletion is a gate people delete.
SEARCH_ROOTS: tuple[str, ...] = ("backend", "core", "drivers", "scripts", "frontend/src")
SOURCE_SUFFIXES: tuple[str, ...] = (".py", ".ts", ".vue", ".js")
# This file names every deleted module by design.
SELF = os.path.abspath(__file__)


def _source_files() -> list[str]:
    out: list[str] = []
    for root in SEARCH_ROOTS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, root)):
            dirnames[:] = [d for d in dirnames
                           if d not in {"__pycache__", "node_modules", ".venv", "dist"}]
            for name in filenames:
                if name.endswith(SOURCE_SUFFIXES):
                    path = os.path.join(dirpath, name)
                    if os.path.abspath(path) != SELF:
                        out.append(path)
    return out


def _hits(pattern: re.Pattern[str]) -> list[str]:
    """[`relpath:lineno: line`] for every match across the searched tree."""
    found: list[str] = []
    for path in _source_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if pattern.search(line):
                    rel = os.path.relpath(path, REPO_ROOT)
                    found.append(f"{rel}:{lineno}: {line.strip()}")
    return found


@pytest.mark.parametrize("module", DELETED_PYTHON_MODULES)
def test_no_python_importer_survives(module: str):
    """`import <module>`, `from <module> import ...`, and `from <module>.x import ...`.

    Matches the dotted path only in an import position, so a *comment* explaining why the
    module went away is allowed — the point is to catch live references, not to make the
    deletion unmentionable.
    """
    dotted = re.escape(module)
    pattern = re.compile(rf"^\s*(?:from\s+{dotted}(?:\.\w+)*\s+import\b"
                         rf"|import\s+{dotted}\b)")
    hits = _hits(pattern)
    assert not hits, f"deleted module {module!r} still imported:\n  " + "\n  ".join(hits)


@pytest.mark.parametrize("module", DELETED_FRONTEND_MODULES)
def test_no_frontend_importer_survives(module: str):
    """A deleted `.vue`/`.ts` module still named in an `import ... from "..."`."""
    pattern = re.compile(rf"""(?:import|from)\s[^\n]*['"][^'"]*{re.escape(module)}['"]""")
    hits = _hits(pattern)
    assert not hits, f"deleted frontend module {module!r} still imported:\n  " + "\n  ".join(hits)


@pytest.mark.parametrize("route", DELETED_ROUTES)
def test_no_caller_of_a_deleted_route_survives(route: str):
    """A deleted endpoint referenced in a string literal, front end or back.

    This is the half a type-checker cannot see: `fetch("/api/worldmodel/scene")` is
    perfectly well-typed and unconditionally 404s.
    """
    pattern = re.compile(rf"""['"`][^'"`]*{re.escape(route)}""")
    hits = _hits(pattern)
    assert not hits, f"deleted route {route!r} still referenced:\n  " + "\n  ".join(hits)


def test_the_gate_can_actually_fail():
    """A gate that cannot fail is decoration.

    All three patterns are re-run against something that is very much alive, so a typo in
    a regex — or an empty file list — cannot make every case above pass vacuously. This is
    the test that has to be kept honest if the patterns are ever edited.
    """
    assert len(_source_files()) > 20, "the file walk found almost nothing; the roots are wrong"

    live_import = re.compile(r"^\s*(?:from\s+core\.perception(?:\.\w+)*\s+import\b"
                             r"|import\s+core\.perception\b)")
    assert _hits(live_import), "the python-import pattern matches nothing at all"

    live_frontend = re.compile(r"""(?:import|from)\s[^\n]*['"][^'"]*CameraView\.vue['"]""")
    assert _hits(live_frontend), "the frontend-import pattern matches nothing at all"

    live_route = re.compile(r"""['"`][^'"`]*/api/cameras""")
    assert _hits(live_route), "the route pattern matches nothing at all"
