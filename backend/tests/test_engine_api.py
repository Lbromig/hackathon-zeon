"""The engine's HTTP + websocket API, driven through a real simulated run (W4).

These are integration tests on purpose. Every claim in the API's contract is about behaviour
across a thread boundary — a run on the engine's worker thread, an operator control arriving on
the API's, and a socket draining the sink from the event loop — and none of it can be checked
against a mock without checking the mock instead.

The properties under test, and why each one is here:

* **snapshot then events reconstructs the run.** That handover is the whole reconnect story
  (D24): `seq` is read before the rows, every state-carrying event is absolute, so a client can
  only ever re-apply and never skip.
* **the wire is strict JSON.** `send_json` would emit bare `NaN` / `Infinity` for a non-finite
  float, `JSON.parse` rejects the frame, and one ill-conditioned solve would break the stream
  for every connected client (B5).
* **`pausing` is reported before `paused`.** Pause is cooperative; claiming an instant stop
  while a move finishes is a lie about where the arm is (D9).
* **a refused start returns every problem.** An operator fixing a bench wants the whole list.
* **injection at the cursor is accepted, and into the past is refused with its reason.** The
  cursor is where a failure leaves the run, so it is the one place injection must work; the past
  is where an accepted injection would never run (R-ENG-13).
* **a plan swap during a live run is refused.** Replacing the runner under a live worker leaks
  a thread still holding a device claim, and the next run's first move fails as busy (B8).
"""
from __future__ import annotations

import json
import math
import os
import time

import pytest
from fastapi.testclient import TestClient

import drivers.mock  # noqa: F401  -- registers the mock driver types
from core.config import settings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOCK_FLEET = os.path.join(REPO_ROOT, "fleet.mock.json")

#: What the reference run reaches on the mock bench: 19 authored actions plus three
#: materialized servo iterations (8 rows each) = 43 rows, 41 of them complete. The loop stalls
#: because the mock world offers no detectable tip/tube pair, which halts the run with the final
#: retract never reached. Asserted as a floor rather than an equality so a better sim world
#: (which would converge the loop and complete all 43) does not read as a regression.
REFERENCE_ROWS = 43
REFERENCE_COMPLETE = 41


def _reject_constant(name: str):  # pragma: no cover - only called on a malformed frame
    raise AssertionError(
        f"the socket sent bare {name}, which is not JSON — `JSON.parse` rejects the whole "
        f"frame, so one non-finite float would break the stream for every connected client. "
        f"Frames must go out as send_text(model_dump_json()), never send_json() (B5).")


def strict_loads(text: str) -> dict:
    """`json.loads` with `NaN` / `Infinity` / `-Infinity` treated as the errors they are."""
    return json.loads(text, parse_constant=_reject_constant)


def _pose(name: str, i: int) -> dict:
    return {"name": name,
            "pose": {"x": 200.0 + i, "y": 10.0 + i, "z": 300.0 + i,
                     "roll": 180.0, "pitch": 0.0, "yaw": 0.0},
            "joints": [0.0, 1.0 + i, -2.0, 0.0, 3.0, 0.0],
            "gripper_width": None,
            "note": "engine api test",
            "saved_at": "2026-01-01T00:00:00+00:00"}


@pytest.fixture
def taught_handover(isolated_teach_poses):
    """A bench where every waypoint the handover plan visits has been taught.

    Derived from the plan (`handover.waypoints_used()`), not hand-listed, so a waypoint added to
    the workflow cannot be silently missing here.
    """
    from backend.app.engine.plans import handover

    library: dict[str, dict] = {}
    for i, (device, name) in enumerate(handover.waypoints_used()):
        library.setdefault(device, {})[name] = _pose(name, i)
    isolated_teach_poses.write_text(json.dumps(library))
    return isolated_teach_poses


@pytest.fixture
def client(monkeypatch, tmp_path):
    """The app with the mock fleet, artifacts in a tmp dir, and no run left behind.

    `run_manager` is process-wide (like `device_claims`), so every test resets it — a runner
    surviving into the next test would be a live worker thread nobody asked for.
    """
    from backend.app.main import app
    from backend.app.services.device_manager import device_manager
    from backend.app.services.run_manager import run_manager

    with open(MOCK_FLEET) as f:
        monkeypatch.setattr(settings, "fleet", json.load(f))
    monkeypatch.setattr(settings, "artifact_dir", str(tmp_path / "runs"))
    run_manager.reset()
    with TestClient(app) as c:      # the lifespan loads the fleet
        # The handlers need connected drivers, and on the real bench that is what the boot
        # initialization plan does. Done directly here so a handler test failure cannot be
        # mistaken for an API failure.
        device_manager.connect_all()
        yield c
    run_manager.reset()


def snapshot(client) -> dict:
    r = client.get("/api/engine/snapshot")
    assert r.status_code == 200, r.text
    return r.json()


def wait_for(client, predicate, *, timeout: float = 60.0, what: str = "condition") -> dict:
    """Poll the snapshot until `predicate(snap)`. Returns the snapshot that satisfied it."""
    deadline = time.monotonic() + timeout
    snap = snapshot(client)
    while not predicate(snap):
        assert time.monotonic() < deadline, (
            f"timed out waiting for {what}; state={snap['state']} cursor={snap['cursor']} "
            f"rows={len(snap['actions'])}")
        time.sleep(0.02)
        snap = snapshot(client)
    return snap


def _assert_finite(value) -> None:
    if isinstance(value, float):
        assert math.isfinite(value), value
    elif isinstance(value, dict):
        for item in value.values():
            _assert_finite(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite(item)


def states_by_aid(snap: dict) -> dict[int, str]:
    return {row["aid"]: row["state"] for row in snap["actions"]}


# --- wiring -------------------------------------------------------------------

def _mounted_paths(app) -> set[str]:
    """Every path the app actually serves, flattened out of the included routers."""
    paths: set[str] = set()
    pending = list(app.routes)
    while pending:
        route = pending.pop()
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            pending.extend(inner.routes)
    return paths


def test_the_engine_socket_is_registered_prefix_less():
    """An APIRouter prefix applies to websocket routes too, and a renamed path shows up as
    nothing but a closed socket — no error, no log line. Assert the path, not the router."""
    from backend.app.main import app

    paths = _mounted_paths(app)
    assert "/ws/engine" in paths
    assert "/api/engine/ws/engine" not in paths
    assert {"/api/engine/plan", "/api/engine/snapshot", "/api/engine/start",
            "/api/engine/pause", "/api/engine/resume", "/api/engine/abort",
            "/api/engine/inject", "/api/engine/preflight",
            "/api/runs/{run_id}/artifacts/{name}"} <= paths


def test_importing_the_app_registers_every_action_handler():
    """`main.py` must import the handler package: the import *is* the registration.

    Without it `handler_for` returns None for every kind, pre-flight reports `no_handler`
    fourteen times, and readiness is `failed` for every plan — an engine that looks
    unimplemented because of a missing import.
    """
    import backend.app.main  # noqa: F401
    from backend.app.engine.actions import ACTION_KINDS, handler_for
    from backend.app.engine.plan import ENGINE_KINDS

    unhandled = [k for k in ACTION_KINDS if k not in ENGINE_KINDS and handler_for(k) is None]
    assert unhandled == []


def test_the_snapshot_answers_before_any_plan_is_loaded(client):
    """The reconnect path must work at mount, not only mid-run — a client that has to special
    case "no run yet" tends to special case it by rendering nothing."""
    snap = snapshot(client)
    assert snap["state"] == "idle"
    assert snap["actions"] == []
    assert snap["run_id"] == ""
    # The reality banner is a correctness requirement, not chrome (D25/D29), so it is present
    # before there is anything to run.
    assert snap["simulated"] and all(snap["simulated"].values())


# --- loading a plan -----------------------------------------------------------

def test_loading_the_handover_plan_returns_the_whole_plan_ready_to_render(client,
                                                                         taught_handover):
    snap = client.post("/api/engine/plan", json={"name": "handover"}).json()
    assert snap["name"] == "handover"
    assert snap["state"] == "idle"
    assert len(snap["actions"]) == 19
    assert snap["readiness"] == "ready"
    assert snap["cursor"] == 0
    assert snap["run_id"]
    kinds = [row["kind"] for row in snap["actions"]]
    assert kinds[0] == "arm.gripper" and kinds[-2] == "control.loop"
    # Rows carry what the tab renders from: derived index, identity, origin, and the
    # kind-specific params.
    first = snap["actions"][0]
    assert first["index"] == 0 and first["origin"] == "plan" and first["state"] == "planned"
    assert first["params"]["state"] == "open"


def test_a_second_load_is_a_new_run_and_the_sequence_never_restarts(client, taught_handover):
    """`run_id` is what a client keys plan state on; `seq` is process-wide and monotonic across
    runs, so a client resets neither its socket nor its `last_seq` (B6)."""
    first = client.post("/api/engine/plan", json={"name": "handover"}).json()
    second = client.post("/api/engine/plan", json={"name": "handover"}).json()
    assert second["run_id"] != first["run_id"]
    assert second["seq"] > first["seq"]


def test_an_unknown_plan_name_is_a_422(client):
    assert client.post("/api/engine/plan", json={"name": "nope"}).status_code == 422


# --- pre-flight and a refused start -------------------------------------------

def test_a_refused_start_returns_every_preflight_problem(client, isolated_teach_poses):
    """Nothing is taught, so the plan cannot run — and the answer must be the whole list.

    A refusal naming one problem at a time turns fixing a bench into a sequence of failed
    attempts, and the pairs must be `(device, waypoint)`: "TUBE is not taught" is the wrong
    diagnosis when it is taught on the other arm (R-WP-5).
    """
    from backend.app.engine.plans import handover

    visited = handover.waypoints_used()
    loaded = client.post("/api/engine/plan", json={"name": "handover"}).json()
    assert loaded["readiness"] == "failed"

    r = client.get("/api/engine/preflight")
    report = r.json()
    assert r.status_code == 200
    assert report["readiness"] == "failed" and report["ok"] is False
    untaught = [p for p in report["problems"] if p["code"] == "waypoint_not_taught"]
    assert {(p["device"], p["waypoint"]) for p in untaught} == set(visited)
    assert all(p["device"] and p["waypoint"] and p["blocking"] for p in untaught)

    r = client.post("/api/engine/start", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "start_refused"
    assert detail["preflight"]["readiness"] == "failed"
    assert len(detail["preflight"]["problems"]) == len(report["problems"])
    # The reason names every blocking problem, so an operator reading only the toast still
    # learns the size of the job.
    assert detail["reason"].count("is not taught") == len(visited)
    assert snapshot(client)["state"] == "failed"
    # And nothing started.
    assert all(s == "planned" for s in states_by_aid(snapshot(client)).values())


def test_an_advisory_only_plan_needs_the_operator_to_confirm(client, monkeypatch):
    """R-ENG-2's confirmation path, on the one advisory problem a named plan can have.

    An empty plan is advisory rather than blocking — it is not a defect — so a bare start is
    refused and `allow_degraded` is the operator saying "I have read that".
    """
    from backend.app.services import run_manager as rm

    monkeypatch.setitem(rm.PLAN_BUILDERS, "startup", lambda *a, **k: [])
    loaded = client.post("/api/engine/plan", json={"name": "startup"}).json()
    assert loaded["readiness"] == "degraded"

    r = client.post("/api/engine/start", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "start_refused"
    assert [p["code"] for p in detail["preflight"]["problems"]] == ["empty_plan"]
    assert detail["preflight"]["ok"] is True        # nothing *blocking*
    assert "allow_degraded" in detail["reason"]

    r = client.post("/api/engine/start", json={"allow_degraded": True})
    assert r.status_code == 200, r.text
    assert r.json()["preflight"]["readiness"] == "degraded"


def test_start_on_a_finished_run_says_so_instead_of_pretending(client, monkeypatch):
    """A restart would spawn a worker that finds the cursor at the end and exits — a run that
    reports "started" and never moves."""
    from backend.app.services import run_manager as rm

    monkeypatch.setitem(rm.PLAN_BUILDERS, "startup", lambda *a, **k: [])
    client.post("/api/engine/plan", json={"name": "startup"})
    client.post("/api/engine/start", json={"allow_degraded": True})
    wait_for(client, lambda s: s["state"] == "complete", what="the empty run to finish")

    r = client.post("/api/engine/start", json={"allow_degraded": True})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "run_finished"


def test_preflight_without_a_plan_is_a_404(client):
    r = client.get("/api/engine/preflight")
    assert r.status_code == 404
    assert "POST /api/engine/plan" in r.json()["detail"]


# --- the event stream ---------------------------------------------------------

def _drain_live(ws, *, since: int, stop_type: str = "run_finished", limit: int = 400):
    """Read frames until `stop_type` arrives. Every frame is checked as strict JSON and for
    strictly increasing `seq`, because those two are the client's whole contract."""
    events = []
    last = since
    for _ in range(limit):
        event = strict_loads(ws.receive_text())
        assert event["seq"] > last, f"seq went backwards: {last} -> {event['seq']}"
        last = event["seq"]
        events.append(event)
        if event["type"] == stop_type:
            return events
    raise AssertionError(f"never saw {stop_type} in {limit} frames")


def test_snapshot_then_events_reconstructs_the_run(client, taught_handover):
    """The reconnect handover, on the initialization plan because it is one action.

    A client seeds from the snapshot, remembers its `seq`, and applies everything after it. The
    result must equal what the server says at the end — with results keyed by `aid`, never by
    index, because an injection renumbers indices (D4).
    """
    seed = client.post("/api/engine/plan", json={"name": "startup"}).json()
    with client.websocket_connect(f"/ws/engine?since={seed['seq']}") as ws:
        started = client.post("/api/engine/start", json={"allow_degraded": True})
        assert started.status_code == 200, started.text
        events = _drain_live(ws, since=seed["seq"])

    types = [e["type"] for e in events]
    # `start` announces `preflight` before it announces the run, and publishes readiness from
    # the same call — a client learns the plan was checked before anything moved.
    assert types[0] == "run_state" and events[0]["state"] == "preflight"
    assert "readiness" in types
    assert "run_started" in types and types[-1] == "run_finished"
    assert "action_started" in types and "action_finished" in types
    assert {e["run_id"] for e in events} == {seed["run_id"]}
    # The `simulated` map is on `run_started` as well as on the snapshot, so a client that
    # joined mid-run and one that saw the start agree about whether this is real.
    run_started = next(e for e in events if e["type"] == "run_started")
    assert run_started["simulated"] and all(run_started["simulated"].values())

    rebuilt = states_by_aid(seed)
    for event in events:
        if event["type"] == "plan_replaced":
            fresh = {row["aid"]: row["state"] for row in event["actions"]}
            # Keep what we already know per aid: `plan_replaced` carries no results.
            rebuilt = {aid: rebuilt.get(aid, state) for aid, state in fresh.items()}
        elif event["type"] == "action_finished":
            rebuilt[event["aid"]] = event["result"]["status"]
    final = wait_for(client, lambda s: s["state"] in ("complete", "failed", "aborted"),
                     what="the initialization run to finish")
    assert rebuilt == states_by_aid(final)
    assert final["state"] == "complete"


def test_the_socket_replays_from_a_sequence_number(client, taught_handover):
    """`?since=` is the small-gap path: a client that dropped its socket for two seconds
    catches up from the bounded buffer instead of re-fetching everything."""
    seed = client.post("/api/engine/plan", json={"name": "startup"}).json()
    client.post("/api/engine/start", json={"allow_degraded": True})
    final = wait_for(client, lambda s: s["state"] in ("complete", "failed"),
                     what="the initialization run to finish")

    with client.websocket_connect(f"/ws/engine?since={seed['seq']}") as ws:
        events = []
        while not events or events[-1]["seq"] < final["seq"]:
            events.append(strict_loads(ws.receive_text()))
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert events[-1]["seq"] == final["seq"]
    assert all(e["seq"] > seed["seq"] for e in events)


def test_a_non_finite_float_leaves_the_socket_as_null_not_as_bare_nan(client):
    """B5, at the transport. `EventBase` does not forbid non-finite floats, `send_json` would
    emit bare `NaN` / `Infinity`, and `JSON.parse` rejects the frame — killing the stream for
    every connected client rather than mis-rendering one row. `model_dump_json` maps them to
    `null`, which is why the wire path is `send_text`."""
    from backend.app.engine.events import LoopIteration
    from backend.app.services.run_manager import run_manager

    before = run_manager.sink.last_seq
    run_manager.sink.emit(LoopIteration, aid=1, iteration=1, magnitude_mm=float("nan"),
                          sigma_mm={"z": float("inf")}, threshold_mm=1.5)
    with client.websocket_connect(f"/ws/engine?since={before}") as ws:
        frame = ws.receive_text()
    assert "NaN" not in frame and "Infinity" not in frame
    event = strict_loads(frame)
    assert event["type"] == "loop_iteration"
    assert event["magnitude_mm"] is None
    assert event["sigma_mm"]["z"] is None


# --- pause, resume, inject ----------------------------------------------------

def test_pause_reports_pausing_first_and_paused_only_when_it_has_stopped(client,
                                                                        taught_handover):
    """D9. `pausing` is the honest answer while a commanded move finishes; a UI that renders it
    as `paused` is lying about where the arm is. Resume then completes the run."""
    client.post("/api/engine/plan", json={"name": "handover"})
    client.post("/api/engine/start", json={})
    wait_for(client, lambda s: s["cursor"] >= 3, what="the run to get past a few actions")

    paused = client.post("/api/engine/pause").json()
    assert paused["changed"] is True
    assert paused["state"] in ("pausing", "paused")

    snap = wait_for(client, lambda s: s["state"] == "paused", what="the pause to land")
    # It stopped at a boundary, not mid-plan-with-everything-done: later rows are untouched.
    assert any(row["state"] == "planned" for row in snap["actions"])
    assert snap["cursor"] < len(snap["actions"])
    # Pausing twice is not an error and not a second pause.
    again = client.post("/api/engine/pause").json()
    assert again["changed"] is False and again["state"] == "paused"

    resumed = client.post("/api/engine/resume").json()
    assert resumed["changed"] is True and resumed["state"] == "running"
    wait_for(client, lambda s: s["state"] in ("complete", "failed"),
             what="the resumed run to finish")


def test_abort_stops_the_run_and_never_reports_it_as_a_failure(client, taught_handover):
    from backend.app.engine.runner import device_claims

    client.post("/api/engine/plan", json={"name": "handover"})
    client.post("/api/engine/start", json={})
    wait_for(client, lambda s: s["cursor"] >= 2, what="the run to start moving")
    assert client.post("/api/engine/abort").json()["changed"] is True

    snap = wait_for(client, lambda s: s["state"] == "aborted", what="the abort to land")
    # Actions never reached stay `planned`, not `skipped`, and nothing is left claimed.
    assert any(row["state"] == "planned" for row in snap["actions"])
    assert device_claims.held() == {}
    assert client.post("/api/engine/resume").json()["changed"] is False


def test_injection_lands_at_the_cursor_and_is_refused_in_the_past(client, taught_handover):
    """The two halves of R-ENG-13/D22 in one run, because they are one rule.

    Injecting **after the action the run stopped on** makes the new action the next step, and it
    is accepted whether that action is parked in a `checkpoint()` or the run is sitting at a
    boundary — a long move in flight is not a reason to refuse (D22). Before the cursor is the
    past, and an injection is never relocated to somewhere it was not asked for: refused, with
    the reason, which the UI renders verbatim.
    """
    client.post("/api/engine/plan", json={"name": "handover"})
    client.post("/api/engine/start", json={})
    wait_for(client, lambda s: s["cursor"] >= 3, what="the run to get past a few actions")
    client.post("/api/engine/pause")
    snap = wait_for(client, lambda s: s["state"] == "paused", what="the pause to land")

    cursor = snap["cursor"]
    stopped_on = snap["actions"][cursor]
    assert stopped_on["kind"] != "control.loop", "this test wants a top-level insertion point"
    r = client.post("/api/engine/inject", json={
        "after_aid": stopped_on["aid"],
        "action": {"kind": "arm.gripper", "device": "right", "state": "open",
                   "label": "injected: reopen the jaws"}})
    assert r.status_code == 200, r.text
    accepted = r.json()
    assert len(accepted["inserted"]) == 1
    injected_aid = accepted["inserted"][0]["aid"]
    assert accepted["inserted"][0]["index"] == cursor + 1
    assert accepted["inserted"][0]["origin"] == "inject"
    # The run was already paused, so the endpoint must not have paused or resumed it.
    assert accepted["paused_for_inject"] is False and accepted["resumed"] is False
    assert accepted["state"] == "paused"
    assert len(accepted["snapshot"]["actions"]) == 20
    # A completed action's result survives the renumber, keyed by aid (D4).
    assert accepted["snapshot"]["actions"][0]["result"]["status"] == "complete"

    first_aid = snap["actions"][0]["aid"]
    into_the_past = client.post("/api/engine/inject", json={
        "after_aid": first_aid,
        "action": {"kind": "arm.gripper", "device": "right", "state": "open"}})
    assert into_the_past.status_code == 409
    detail = into_the_past.json()["detail"]
    assert detail["error"] == "inject_refused"
    assert detail["after_aid"] == first_aid
    # Whichever rule bites — "that part of the plan has already run", or "the action executing
    # now is in front of it" while a handler is parked mid-decap — the reason has to say which
    # and what to do instead, because the UI renders it verbatim.
    assert "in the past" in detail["reason"] or "executing now" in detail["reason"]
    assert "Inject after aid" in detail["reason"] or "The run is at" in detail["reason"]

    # And the accepted one really runs — an accepted injection that never executes is a silent
    # skip wearing an accepted request's clothes.
    client.post("/api/engine/resume")
    done = wait_for(client, lambda s: s["state"] in ("complete", "failed"),
                    timeout=180.0, what="the resumed run to finish")
    assert states_by_aid(done)[injected_aid] == "complete"

    # With the worker gone, the past is the only rule that can bite, and it says so.
    after_the_run = client.post("/api/engine/inject", json={
        "after_aid": first_aid,
        "action": {"kind": "arm.gripper", "device": "right", "state": "open"}})
    assert after_the_run.status_code == 409
    assert "in the past" in after_the_run.json()["detail"]["reason"]


def test_the_endpoint_owns_the_pause_inject_resume_sequence(client, taught_handover):
    """R-ENG-12. `Runner.inject` deliberately does not pause around itself, because only the
    endpoint knows whether the operator sent one action or three — one pause for the batch."""
    client.post("/api/engine/plan", json={"name": "handover"})
    client.post("/api/engine/start", json={})
    snap = wait_for(client, lambda s: s["cursor"] >= 3 and s["state"] == "running",
                    what="the run to get past a few actions")

    r = client.post("/api/engine/inject", json={
        "after_aid": snap["actions"][-1]["aid"],
        "actions": [{"kind": "arm.gripper", "device": "left", "state": "open"},
                    {"kind": "arm.gripper", "device": "right", "state": "open"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paused_for_inject"] is True and body["resumed"] is True
    assert [row["aid"] for row in body["inserted"]] == sorted(
        row["aid"] for row in body["inserted"])
    assert len(body["inserted"]) == 2
    assert body["state"] in ("running", "pausing", "paused")
    wait_for(client, lambda s: s["state"] in ("complete", "failed", "aborted"),
             what="the run to finish")


def test_an_injection_needs_exactly_one_of_action_and_actions(client, taught_handover):
    client.post("/api/engine/plan", json={"name": "handover"})
    both = client.post("/api/engine/inject", json={
        "after_aid": None, "action": {"kind": "arm.gripper", "device": "left", "state": "open"},
        "actions": [{"kind": "arm.gripper", "device": "left", "state": "open"}]})
    assert both.status_code == 422
    neither = client.post("/api/engine/inject", json={"after_aid": None})
    assert neither.status_code == 422
    # A malformed action is a 422 from the union, before anything can move.
    bad = client.post("/api/engine/inject", json={
        "after_aid": None, "action": {"kind": "arm.gripper", "device": "left",
                                      "state": "sideways"}})
    assert bad.status_code == 422


def test_a_plan_swap_during_a_live_run_is_refused(client, taught_handover):
    """B8. Replacing the runner under a live worker leaves that thread alive holding its device
    claim; the new run's first gripper move is then refused as busy, retried, and refused again
    — while the UI shows a fresh plan and the bench holds a cap half unscrewed."""
    client.post("/api/engine/plan", json={"name": "handover"})
    client.post("/api/engine/start", json={})
    wait_for(client, lambda s: s["cursor"] >= 2, what="the run to start moving")

    r = client.post("/api/engine/plan", json={"name": "startup"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "run_active"
    assert "abort" in detail["reason"].lower()

    # A *paused* run is refused too, and that is the case that matters: its worker is alive and
    # still holds its claim.
    client.post("/api/engine/pause")
    wait_for(client, lambda s: s["state"] == "paused", what="the pause to land")
    assert client.post("/api/engine/plan", json={"name": "startup"}).status_code == 409

    # Aborting is the way out, and then the load is accepted.
    client.post("/api/engine/abort")
    wait_for(client, lambda s: s["state"] == "aborted", what="the abort to land")
    accepted = client.post("/api/engine/plan", json={"name": "startup"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["name"] == "startup"


# --- the whole run ------------------------------------------------------------

def test_the_full_simulated_handover_runs_through_the_api(client, taught_handover):
    """The reference run, end to end through the API: load, pre-flight, start, watch, finish.

    41 of 43 actions complete on the mock bench. The two that do not are the servo loop (which
    stalls: the mock world has no detectable tip/tube pair, and an unobserved offset counts as
    no progress rather than as converged) and the retract behind it, which is never reached.
    That is the honest outcome for this bench, and it exercises every layer: the plan, the
    wrapper, loop materialization, the failure classification, and the wire.
    """
    from backend.app.engine.runner import device_claims

    loaded = client.post("/api/engine/plan", json={"name": "handover"}).json()
    assert loaded["readiness"] == "ready"
    assert client.get("/api/engine/preflight").json()["ok"] is True

    started = client.post("/api/engine/start", json={})
    assert started.status_code == 200, started.text
    assert started.json()["state"] in ("running", "preflight")
    assert started.json()["started_at"]

    final = wait_for(client, lambda s: s["state"] in ("complete", "failed", "aborted"),
                     timeout=180.0, what="the handover run to finish")
    rows = final["actions"]
    completed = sum(1 for row in rows if row["state"] == "complete")
    assert len(rows) >= REFERENCE_ROWS, [r["kind"] for r in rows]
    assert completed >= REFERENCE_COMPLETE, {r["index"]: r["state"] for r in rows
                                             if r["state"] != "complete"}
    # Every authored action before the servo loop ran, in order.
    assert all(row["state"] == "complete" for row in rows[:17])
    # The loop materialized real indexed rows carrying their iteration, so iteration 3's frames
    # and solve are inspectable rather than collapsed into one opaque row (R-VIS-8).
    expanded = [row for row in rows if row["origin"] == "expand"]
    assert expanded and {row["iteration"] for row in expanded} >= {1, 2, 3}
    assert all(row["parent_aid"] is not None for row in expanded)
    # Nothing is left claimed, whatever the outcome.
    assert device_claims.held() == {}
    # And no non-finite float reached the wire — the models refuse to hold one, so the snapshot
    # cannot smuggle one out either (B5's third defence).
    text = client.get("/api/engine/snapshot").text
    assert "NaN" not in text and "Infinity" not in text
    _assert_finite(strict_loads(text))

    # Every action carries its own log records, findable by the identity the row shows.
    aid = rows[0]["aid"]
    records = client.get(f"/api/logs?run_id={final['run_id']}&aid={aid}").json()["records"]
    assert records and all(r["aid"] == aid for r in records)


def test_boot_initialization_runs_the_startup_plan_without_blocking(client, taught_handover):
    """D5/R-INIT: initialization is a plan on the same runner, and R-START-7: it can never hold
    up the API. `boot_init` therefore returns a thread and swallows everything."""
    from backend.app.services.run_manager import run_manager

    run_manager.reset()
    thread = run_manager.boot_init(force=True)
    assert thread is not None
    thread.join(30.0)
    assert not thread.is_alive()

    final = wait_for(client, lambda s: s["state"] in ("complete", "failed", "aborted"),
                     what="boot initialization to finish")
    assert final["name"] == "startup"
    assert final["state"] == "complete"
    outputs = final["actions"][0]["result"]["outputs"]
    assert outputs["kind"] == "initialize"
    assert {d["device"] for d in outputs["devices"]} == set(final["simulated"])
    assert all(d["connected"] for d in outputs["devices"])
    # No taught HOME in this library: warned and skipped, never sent to a guessed pose.
    assert sorted(outputs["home_missing"]) == ["left", "right"]

    # It is skipped under pytest unless forced, so no test ever finds a run it did not start.
    assert run_manager.boot_init() is None


# --- artifacts ----------------------------------------------------------------

def test_an_artifact_is_served_from_the_run_directory(client, tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "overlay.png").write_bytes(b"\x89PNG\r\n\x1a\nnot really")

    r = client.get("/api/runs/run-1/artifacts/overlay.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    assert r.headers["content-type"] == "image/png"
    assert "max-age" in r.headers.get("cache-control", "")


@pytest.mark.parametrize("run_id,name", [
    ("run-1", "../../../../etc/passwd"),
    ("run-1", "..%2f..%2fetc%2fpasswd"),
    ("..", "secret.txt"),
    ("run-1", "secret.txt"),          # simply not there
    ("run-1", "sub/secret.txt"),
    ("run-1", "/etc/passwd"),
])
def test_artifact_serving_never_escapes_the_run_directory(client, tmp_path, run_id, name):
    """The only route that turns client text into a filesystem path. Every rejection is a 404:
    "not allowed" and "not there" are the same answer to a client."""
    (tmp_path / "runs" / "run-1").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("the developer's home directory")
    r = client.get(f"/api/runs/{run_id}/artifacts/{name}")
    assert r.status_code == 404, r.text


def test_a_symlink_out_of_the_run_directory_is_refused(client, tmp_path):
    """Resolution happens after `realpath`, not before: a symlink inside the run directory
    passes every textual check there is."""
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    target = tmp_path / "outside.txt"
    target.write_text("not yours")
    os.symlink(target, run_dir / "link.txt")
    assert client.get("/api/runs/run-1/artifacts/link.txt").status_code == 404


def test_the_e_stop_stays_reachable_while_the_engine_holds_an_arm(client, taught_handover):
    """D21/R-ENG-11, at the route rather than at the claim registry. `device_claims` has no
    blocking primitive and nothing in the engine API may gate the stop path: emergency stop has
    to be reachable *while* an action is blocking, which is the only time it matters."""
    client.post("/api/engine/plan", json={"name": "handover"})
    client.post("/api/engine/start", json={})
    wait_for(client, lambda s: s["cursor"] >= 2, what="the run to start moving")

    r = client.post("/api/arms/right/stop", json={"emergency": True})
    assert r.status_code == 200, r.text
    client.post("/api/engine/abort")
