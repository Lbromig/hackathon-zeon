"""`seq` across two runs in one process — the ordering authority the socket rule depends on.

§3.3 of the design tells a client: "if `seq <= last`: drop (a re-apply is a no-op, a skip is
not)". That rule is only sound if `seq` never goes backwards on the socket, and one socket
carries every run in the process. While the sequence restarted at 1 per run it did go backwards:
the review measured 117 events for a 4-iteration handover, so an operator who ran the plan and
then ran it again had the whole of run B up to roughly the decap dropped by the client's own
rule — a frozen plan and a stale run id while both arms moved.

So the sequence is process-wide (`PROCESS_SEQUENCE`), and these tests pin the two halves a client
codes against: ordering comes from `seq` alone, identity comes from `run_id`, and the two are not
the same question.
"""
from __future__ import annotations

from backend.app.engine import actions as A
from backend.app.engine.events import EventSequence, RunResumed
from backend.app.engine.plan import Plan
from backend.app.engine.runner import EventSink, Runner

FLEET = {"ot"}


class _Devices:
    def get(self, device_id: str):
        if device_id not in FLEET:
            raise KeyError(device_id)
        return object()

    def require_liquid_handler(self, device_id: str):
        return self.get(device_id)

    def is_simulated(self, device_id: str) -> bool:
        return True


def _move(dz: float = 1.0) -> dict:
    return {"kind": "lh.move_relative", "device": "ot", "dz": dz}


def _runner(tmp_path, monkeypatch) -> Runner:
    monkeypatch.setitem(A._HANDLERS, "lh.move_relative",
                        lambda a, c: A.LHMoveOutputs(applied_mm={"z": a.dz}))
    runner = Runner(Plan([_move(1.0), _move(2.0)], name="handover"), devices=_Devices(),
                    known_devices=FLEET, artifact_root=str(tmp_path))
    assert runner.run_to_completion(timeout=5.0) == "complete"
    return runner


def test_a_second_run_continues_the_sequence_instead_of_restarting_at_one(tmp_path,
                                                                         monkeypatch):
    """The failure the client's drop rule caused, as a test. Run B's first event must be newer
    than everything run A ever emitted, or "drop `seq <= last`" discards run B."""
    first = _runner(tmp_path, monkeypatch)
    second = _runner(tmp_path, monkeypatch)
    assert first.run_id != second.run_id

    a = [e.seq for e in first.sink.events()]
    b = [e.seq for e in second.sink.events()]
    assert a and b
    assert b[0] > a[-1], "run B starts after run A ends"
    combined = a + b
    assert combined == sorted(combined) and len(set(combined)) == len(combined)


def test_the_documented_drop_rule_keeps_every_event_of_a_second_run(tmp_path, monkeypatch):
    """The client's rule, executed literally against two runs on one socket. Nothing may be
    dropped, because nothing was re-delivered."""
    first = _runner(tmp_path, monkeypatch)
    second = _runner(tmp_path, monkeypatch)

    last = 0
    applied = []
    for event in list(first.sink.events()) + list(second.sink.events()):
        if event.seq <= last:            # the documented rule, verbatim
            continue
        last = event.seq
        applied.append(event)
    assert len(applied) == len(first.sink.events()) + len(second.sink.events())
    assert {e.run_id for e in applied} == {first.run_id, second.run_id}


def test_the_snapshot_is_what_a_client_seeds_last_seq_from(tmp_path, monkeypatch):
    """Not zero, and not 1. `run_id` changing means "re-fetch the snapshot and rebuild the
    plan"; it never means "reset the ordering", which is the distinction B6 turned on."""
    first = _runner(tmp_path, monkeypatch)
    second = _runner(tmp_path, monkeypatch)
    snap = second.snapshot()
    assert snap.run_id == second.run_id
    assert snap.seq == second.sink.last_seq > first.sink.last_seq
    assert second.sink.since(snap.seq) == []


def test_an_explicit_sequence_is_still_isolated_and_starts_at_one():
    """The escape hatch, for a test that wants absolute numbers. Process-wide is the default,
    not the only option."""
    from backend.app.engine.events import PROCESS_SEQUENCE

    sink = EventSink(run_id="r_1", sequence=EventSequence())
    assert sink.last_seq == 0
    assert sink.emit(RunResumed).seq == 1
    assert sink.emit(RunResumed).seq == 2
    assert PROCESS_SEQUENCE.next() > 2, "and the process sequence is somewhere else entirely"
