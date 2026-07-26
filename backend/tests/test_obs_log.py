"""The log substrate: context binding (D23), JSONL shape, and the tail/cursor reader.

The load-bearing test is `test_context_reaches_a_child_loggers_records`. The naive
implementation — `root.addFilter(ContextFilter())` — passes every test that logs on the
root logger and fails only for child loggers, which is every real caller. It fails
*silently*: records are written, they just carry no `run_id`/`aid`, so the per-action log
drill-down returns nothing while looking implemented (review B7). So that case is asserted
directly, and the broken arrangement is asserted to be broken, so nobody "simplifies" this
back.
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from core.obs import log as obs
from core.obs.tail import LogPage, make_predicate, read_since, tail


@pytest.fixture
def logfile(tmp_path):
    """A configured logger writing to a temp file. Restores the root logger afterwards."""
    path = tmp_path / "zeon.jsonl"
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    obs.configure(path=str(path), level="DEBUG", console=False, force=True)
    yield path
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    obs.clear()


def records(path) -> list[dict]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- D23: context is bound on the HANDLERS -----------------------------------

def test_context_reaches_a_child_loggers_records(logfile):
    """The whole of D23 in one assertion.

    A record created on `engine.handlers.arm` and *propagated* to the root's handlers must
    still carry the run and action stamps. This is what a logger-level filter cannot do.
    """
    with obs.action_context(run_id="r_1", aid=12, index=11,
                            action_kind="arm.waypoint", device="right"):
        logging.getLogger("engine.handlers.arm").info("moving to APPROACH_RACK")

    (entry,) = records(logfile)
    assert entry["logger"] == "engine.handlers.arm"
    assert entry["run_id"] == "r_1" and entry["aid"] == 12 and entry["index"] == 11
    assert entry["action_kind"] == "arm.waypoint" and entry["device"] == "right"


def test_a_logger_level_filter_would_not_have_worked():
    """Pins the reason D23 exists, so the handler filter is not 'simplified' onto the root.

    `Logger.handle` runs only the filters of the logger the record was created on, so a
    filter on the root never sees a propagated record. If a future CPython changed that,
    this test failing is the signal to revisit D23 — not a reason to delete it.
    """
    root = logging.getLogger()
    child = logging.getLogger("test.d23.child")
    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):        # no formatting; we only care about the attributes
            seen.append(record)

    class Stamp(logging.Filter):
        def filter(self, record):
            record.stamped = True
            return True

    handler = Capture()
    saved, saved_level = list(root.handlers), root.level
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.addFilter(Stamp())
    try:
        child.info("propagated")
        assert seen, "the record did not reach the root handler at all"
        assert not hasattr(seen[0], "stamped"), (
            "a root *logger* filter now stamps propagated records — re-read D23 before "
            "moving the ContextFilter off the handlers")
    finally:
        root.removeFilter(next(f for f in root.filters if isinstance(f, Stamp)))
        root.handlers = saved
        root.setLevel(saved_level)


def test_context_is_restored_not_cleared_on_exit(logfile):
    """Nesting: leaving an action's block must not drop the run it belongs to."""
    with obs.action_context(run_id="r_1"):
        with obs.action_context(aid=3, action_kind="camera.snapshot"):
            assert obs.current_context()["aid"] == 3
        assert obs.current_context() == {"run_id": "r_1"}
        logging.getLogger("engine").info("between actions")

    (entry,) = records(logfile)
    assert entry["run_id"] == "r_1" and "aid" not in entry


def test_records_outside_any_action_carry_no_stamps(logfile):
    """Absence has to be honest: a boot-time record must not inherit a stale run id."""
    logging.getLogger("backend.app.main").info("starting up")
    (entry,) = records(logfile)
    assert not ({"run_id", "aid", "action_kind"} & set(entry))


def test_an_unknown_context_field_is_refused():
    """A typo'd field would bind nothing and stamp nothing, silently."""
    with pytest.raises(KeyError):
        obs.bind(run_di="r_1")


def test_explicit_extra_wins_over_the_bound_context(logfile):
    """The camera supervisor re-emits a child's records (D7), so it must be able to say
    which device they came from rather than inheriting the parent's action context."""
    with obs.action_context(device="right"):
        logging.getLogger("camera.supervisor").info("child said hello",
                                                    extra={"device": "handover_cam"})
    (entry,) = records(logfile)
    assert entry["device"] == "handover_cam"


# --- JSONL shape -------------------------------------------------------------

def test_extra_fields_are_merged_so_inputs_and_outputs_land_in_the_file(logfile):
    """R-LOG-1: the action wrapper logs typed inputs and outputs; they must survive as
    structure, not as a stringified dict nobody can filter on."""
    logging.getLogger("engine.runner").info(
        "offset computed",
        extra={"event": "action_output",
               "inputs": {"frame_ref": {"slot": "frame"}},
               "outputs": {"kind": "offset", "offset_mm": {"x": 3.9, "y": None, "z": -6.6}},
               "duration_ms": 812})
    (entry,) = records(logfile)
    assert entry["event"] == "action_output"
    assert entry["outputs"]["offset_mm"]["z"] == -6.6
    assert entry["inputs"]["frame_ref"]["slot"] == "frame"
    assert entry["duration_ms"] == 812


def test_non_finite_floats_do_not_eat_the_record(logfile):
    """A NaN raised inside `json.dumps` is a log line that swallows an exception — and NaN
    is what a failed detection produces, so it arrives exactly when the log matters."""
    logging.getLogger("engine").warning("bad readback",
                                        extra={"outputs": {"z": float("nan"),
                                                           "y": float("inf")}})
    (entry,) = records(logfile)
    assert entry["outputs"]["z"] == "nan" and entry["outputs"]["y"] == "inf"


def test_every_record_is_one_json_object_with_ts_level_logger_and_seq(logfile):
    logging.getLogger("a.b").error("first")
    logging.getLogger("c").debug("second")
    got = records(logfile)
    assert [r["msg"] for r in got] == ["first", "second"]
    assert [r["level"] for r in got] == ["ERROR", "DEBUG"]
    assert [r["logger"] for r in got] == ["a.b", "c"]
    assert got[0]["seq"] < got[1]["seq"]            # monotonic, so same-ms order is defined
    assert got[0]["ts"].endswith("Z")


def test_an_exception_is_recorded_as_text_not_lost(logfile):
    try:
        raise DriverStub("brakes not all engaged")
    except DriverStub:
        logging.getLogger("drivers.xarm").exception("disconnect failed")
    (entry,) = records(logfile)
    assert "brakes not all engaged" in entry["exc"]


class DriverStub(Exception):
    pass


def test_configure_is_idempotent(logfile):
    """uvicorn --reload re-runs the lifespan. Stacked handlers write every record twice,
    which reads as a bug in whatever produced the records."""
    before = len(logging.getLogger().handlers)
    obs.configure(path=str(logfile), console=False)          # no force: should no-op
    assert len(logging.getLogger().handlers) == before
    logging.getLogger("x").info("once")
    assert len(records(logfile)) == 1


# --- the tail / cursor reader -------------------------------------------------

def write(path, *entries: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_tail_returns_the_last_n_oldest_first(tmp_path):
    path = tmp_path / "log.jsonl"
    write(path, *({"seq": i, "msg": str(i), "level": "INFO"} for i in range(500)))
    page = tail(str(path), limit=3)
    assert [r["seq"] for r in page.records] == [497, 498, 499]


def test_tail_of_a_missing_file_is_empty_not_an_error(tmp_path):
    page = tail(str(tmp_path / "nope.jsonl"))
    assert page.records == [] and isinstance(page, LogPage)


def test_a_half_written_last_line_is_skipped_not_fatal(tmp_path):
    """The file is appended to while it is read; an incomplete record is not yet a record."""
    path = tmp_path / "log.jsonl"
    write(path, {"seq": 1, "msg": "complete"})
    with open(path, "a") as fh:
        fh.write('{"seq": 2, "msg": "half')
    page = tail(str(path))
    assert [r["seq"] for r in page.records] == [1]


def test_records_spanning_a_block_boundary_are_not_split(tmp_path):
    """The backwards reader holds over a partial first line; a record longer than a block,
    or straddling one, must still parse."""
    path = tmp_path / "log.jsonl"
    write(path, *({"seq": i, "msg": "x" * 900} for i in range(400)))   # > 64 KiB total
    page = tail(str(path), limit=400)
    assert [r["seq"] for r in page.records] == list(range(400))


def test_filtering_by_action_pulls_one_actions_records(tmp_path):
    """R-UI-8: "the logs this action produced" is this query."""
    path = tmp_path / "log.jsonl"
    write(path,
          {"seq": 1, "run_id": "r", "aid": 1, "level": "INFO", "msg": "a"},
          {"seq": 2, "run_id": "r", "aid": 2, "level": "INFO", "msg": "b"},
          {"seq": 3, "run_id": "r", "aid": 1, "level": "ERROR", "msg": "c"},
          {"seq": 4, "run_id": "other", "aid": 1, "level": "INFO", "msg": "d"})
    page = tail(str(path), match=make_predicate(run_id="r", aid=1))
    assert [r["msg"] for r in page.records] == ["a", "c"]


def test_level_is_a_minimum_not_an_equality(tmp_path):
    """Asking for warnings and not being shown errors is a filter that hides the thing you
    were looking for."""
    path = tmp_path / "log.jsonl"
    write(path, {"seq": 1, "level": "DEBUG"}, {"seq": 2, "level": "WARNING"},
          {"seq": 3, "level": "ERROR"})
    page = tail(str(path), match=make_predicate(level="warning"))
    assert [r["seq"] for r in page.records] == [2, 3]


def test_cursor_returns_only_what_is_new(tmp_path):
    path = tmp_path / "log.jsonl"
    write(path, {"seq": 1}, {"seq": 2})
    first = tail(str(path))
    assert [r["seq"] for r in first.records] == [1, 2]

    write(path, {"seq": 3})
    second = read_since(str(path), first.cursor)
    assert [r["seq"] for r in second.records] == [3]
    assert not second.reset

    third = read_since(str(path), second.cursor)
    assert third.records == [] and third.cursor == second.cursor


def test_an_empty_cursor_starts_from_the_newest_page(tmp_path):
    """A first poll must not replay the whole file."""
    path = tmp_path / "log.jsonl"
    write(path, *({"seq": i} for i in range(100)))
    page = read_since(str(path), "", limit=5)
    assert [r["seq"] for r in page.records] == [95, 96, 97, 98, 99]


def test_rotation_is_reported_rather_than_silently_skipping_records(tmp_path):
    """A byte offset into a rotated file is a wrong answer presented confidently. The
    caller has to be able to say "log rotated" instead of showing a gap (R-LOG-7)."""
    path = tmp_path / "log.jsonl"
    write(path, {"seq": 1}, {"seq": 2})
    cursor = tail(str(path)).cursor

    os.rename(path, tmp_path / "log.jsonl.1")          # what RotatingFileHandler does
    write(path, {"seq": 3})

    page = read_since(str(path), cursor)
    assert page.reset is True
    assert [r["seq"] for r in page.records] == [3]


def test_truncation_is_also_a_reset(tmp_path):
    path = tmp_path / "log.jsonl"
    write(path, *({"seq": i} for i in range(50)))
    cursor = tail(str(path)).cursor
    path.write_text("")
    assert read_since(str(path), cursor).reset is True


def test_a_corrupt_cursor_degrades_to_a_tail(tmp_path):
    path = tmp_path / "log.jsonl"
    write(path, {"seq": 1})
    page = read_since(str(path), "not-a-cursor")
    assert [r["seq"] for r in page.records] == [1]


# --- R-LOG-6: no diagnostics may go to stdout ---------------------------------

def test_no_print_diagnostics_remain():
    """R-LOG-6: a warning that matters operationally must be reachable programmatically.

    A `print` is unreachable from the readiness state and the log view, and it is invisible
    in a `just backend` that someone closed the terminal on. The one allowed exception is a
    `__main__` CLI block, where stdout *is* the output — those are listed explicitly rather
    than pattern-matched, so a new one has to be justified here.
    """
    import re

    roots = ("backend", "core", "drivers")
    allowed = {
        # `python -m core.perception.fiducials <image>` prints its findings for a human.
        os.path.join("core", "perception", "fiducials.py"),
    }
    offenders: list[str] = []
    for root in roots:
        base = os.path.join(REPO_ROOT, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, REPO_ROOT)
                if rel in allowed or rel.startswith(os.path.join("backend", "tests")):
                    continue
                with open(path, encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if re.match(r"^\s*print\(", line):
                            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "print() instead of a logger:\n  " + "\n  ".join(offenders)


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_the_reader_reads_what_the_writer_wrote(logfile):
    """End to end through the real formatter, so a change to either side is caught here
    rather than in the frontend."""
    with obs.action_context(run_id="r_9", aid=4, action_kind="vision.identify"):
        logging.getLogger("engine.handlers.vision").info("tip found", extra={"score": 0.8})
    for handler in logging.getLogger().handlers:
        handler.flush()

    page = tail(str(logfile), match=make_predicate(run_id="r_9", aid=4))
    (entry,) = page.records
    assert entry["msg"] == "tip found" and entry["score"] == 0.8
    assert entry["action_kind"] == "vision.identify"
