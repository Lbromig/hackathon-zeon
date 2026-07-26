"""The adversarial fault harness, checked for honesty rather than for passing.

The harness produces the project's headline number ("N of M planted faults
caught"), which makes it the easiest thing in the repo to quietly rig. A table
that scores itself can be wrong in three ways, and these tests exist for those
three specifically:

1. The declared expectation could disagree with what the agent actually returns,
   and the harness could still print a tidy table. So every row is re-checked
   against the agent, and one row is re-verified from scratch here so the Outcome
   record cannot be a work of fiction.
2. The straw man could be a constant ``False``, which would make the contrast
   free. So it is pinned as a real function of the controller's fault flag: it
   fails the one row where the arm noticed the problem itself, and passes the
   rest.
3. The output could be presentable as a hardware result. So the SIMULATED
   labelling is asserted, not left to reviewer discipline.

No cv2, no camera, no arm. ``test_the_harness_never_calls_the_real_detector``
pins that the vision channel comes from the stubbed seam, which is why the
scored number does not move depending on whether OpenCV is installed.
"""
from __future__ import annotations

import re

import pytest

from core.verification import adversarial as adv
from core.verification import agents as agents_mod
from core.verification.agents import AGENTS, PASS_THRESHOLD


@pytest.fixture(scope="module")
def outcomes():
    return adv.run()


@pytest.fixture(scope="module")
def by_name(outcomes):
    return {o.scenario: o for o in outcomes}


@pytest.fixture(scope="module")
def summary(outcomes):
    return adv.summarise(outcomes)


# --- the harness agrees with the agent -------------------------------------

def test_every_scenario_meets_its_declared_expectation(outcomes, summary):
    """The gate. A declared verdict the agent does not reach is the harness lying."""
    assert summary.mismatches == []
    for o in outcomes:
        assert o.met_expectation, (
            f"{o.scenario}: declared {o.expect}, agent returned {o.fused_verdict} "
            f"({o.fused_detail})"
        )
    assert summary.ok is True


def test_outcome_fields_are_not_fabricated(by_name):
    """Re-verify one row end to end, without going through the harness.

    Everything else here reads the harness's own record of what happened. This
    test builds the evidence, calls the registered agent directly and compares,
    so a bug (or a convenient shortcut) in ``run_scenario`` cannot hide.
    """
    scenario = next(s for s in adv.scenarios() if s.name == "cap_still_on")
    evidence = scenario.build()
    with adv.stubbed_vision(evidence, scenario.vision_travel_px, scenario.datum_travel_px):
        result = AGENTS["cap_removed"].verify(evidence)

    recorded = by_name["cap_still_on"]
    assert result.ok is False
    assert adv.classify(result).value == recorded.fused_verdict
    assert result.data["depth_verdict"] == recorded.depth_verdict
    assert result.detail == recorded.fused_detail
    assert pytest.approx(result.confidence, abs=1e-3) == recorded.fused_confidence


def test_the_table_covers_the_situations_it_claims_to(by_name):
    """Coverage is asserted, not assumed.

    Deleting a row would otherwise raise the caught percentage, which is the one
    change to this file that makes the headline look better while making it mean
    less.
    """
    assert by_name["clean_uncap"].expect == "pass"
    assert by_name["cap_still_on"].expect == "fail"
    assert by_name["depth_region_unreadable"].expect == "fail"
    assert by_name["no_evidence_at_all"].expect == "fail"
    assert by_name["cap_on_but_proxies_say_off"].expect == "escalate"
    assert {o.expect for o in by_name.values()} == {"pass", "fail", "escalate"}


def test_scenario_names_are_unique_and_described():
    table = adv.scenarios()
    names = [s.name for s in table]
    assert len(set(names)) == len(names)
    for s in table:
        assert s.situation.strip(), f"{s.name} has no physical story attached"
        # House rule, and the situation strings are what a reviewer reads.
        assert s.situation.isascii(), s.name
        assert s.name.isascii()


# --- the straw man is real, and it does lose ------------------------------

def test_the_naive_baseline_waves_through_what_the_fusion_catches(outcomes):
    """The contrast the table exists to show.

    Every planted fault the arm cannot see for itself is a fault the naive
    verifier reports as success. If this ever fails, either the baseline stopped
    reproducing the old behaviour or the faults stopped being physical.
    """
    faults = [o for o in outcomes if o.planted_fault]
    assert faults, "a harness with no planted faults proves nothing"

    missed_by_naive = [o for o in faults if not o.naive_caught]
    assert len(missed_by_naive) >= 4
    for o in missed_by_naive:
        assert o.caught, f"{o.scenario}: the fused verifier missed it too"
        assert o.naive_verdict == "pass"


def test_the_naive_baseline_is_not_a_constant_function(by_name):
    """It has to be an honest implementation, or the comparison is free.

    It reads the controller's own fault flag and nothing else, so it does catch
    the row where the arm faulted, and that row is in the table on purpose.
    """
    assert by_name["controller_faulted_mid_unscrew"].naive_verdict == "fail"
    assert by_name["cap_still_on"].naive_verdict == "pass"


def test_naive_and_fused_differ_only_by_consulting_a_sensor(by_name):
    """The two rows with identical physical state and different fault flags.

    ``cap_still_on`` and ``controller_faulted_mid_unscrew`` plant the same
    physical situation. The fused verifier must reach the same verdict for both,
    because the tube is what it looks at. The naive one flips, because the flag is
    all it has. Both halves are asserted: the fused half is the one that says the
    sensor is doing the work.
    """
    quiet, flagged = (
        next(s for s in adv.scenarios() if s.name == name)
        for name in ("cap_still_on", "controller_faulted_mid_unscrew")
    )
    quiet_ev, flagged_ev = quiet.build(), flagged.build()

    assert adv.naive_verify(quiet_ev).ok is True
    assert adv.naive_verify(flagged_ev).ok is False
    assert (
        by_name["cap_still_on"].fused_verdict
        == by_name["controller_faulted_mid_unscrew"].fused_verdict
        == "fail"
    )
    assert (
        by_name["cap_still_on"].channels
        == by_name["controller_faulted_mid_unscrew"].channels
    )


def test_the_naive_baseline_is_blind_to_every_sensor_reading():
    """Sensor-blindness, demonstrated rather than declared.

    ``naive_verify`` sets ``data["consulted_sensors"] = False``, and asserting
    that flag would only restate a literal the function assigns two lines
    earlier. The property has to be shown instead: hold the controller's fault
    flag fixed, swing every sensor reading from "clean uncap" to "cap never
    moved", and the verdict must not budge. A verifier that flinched at any of
    these would not be reproducing the behaviour this table is contrasting
    against.
    """
    unchanging = [
        adv.cap_evidence(torque_nm=torque)
        for torque in (
            adv.TORQUE_COLLAPSED_NM,
            adv.TORQUE_STILL_LOADED_NM,
            adv.TORQUE_NEVER_LOADED_NM,
        )
    ]
    unchanging += [
        adv.cap_evidence(depth_distance_m=adv.STANDOFF_M),
        adv.cap_evidence(depth_distance_m=adv.STANDOFF_M + adv.CAP_HEIGHT_STEP_M),
        adv.cap_evidence(depth_distance_m=adv.STANDOFF_M, depth_dropout=1.0),
        adv.cap_evidence(torque_nm=adv.TORQUE_COLLAPSED_NM, with_overview=True),
    ]
    verdicts = {adv.naive_verify(ev).ok for ev in unchanging}
    assert verdicts == {True}, "the straw man reacted to a sensor, so it is not the straw man"

    # The same evidence, scored by the real agent, does not come out uniform.
    # Without this half the test above would also pass against a constant True.
    fused = set()
    for ev in unchanging:
        with adv.stubbed_vision(ev, adv.MARKER_TRAVEL_PX):
            fused.add(AGENTS["cap_removed"].verify(ev).ok)
    assert fused == {True, False}


def test_a_fault_code_survives_evidence_with_no_torque_block():
    """The builder must not quietly drop the flag when no effort is supplied.

    A controller fault and a torque trace arrive from different places on the
    real driver, and an evidence bundle carrying the fault but no effort is a
    normal thing to want. Dropping the flag would make the straw man look better
    than it is on any scenario built that way.
    """
    evidence = adv.cap_evidence(error_code=adv.CONTROLLER_ERROR_CODE)
    assert evidence.telemetry[adv.TURNING_ARM]["error_code"] == adv.CONTROLLER_ERROR_CODE
    assert adv.naive_verify(evidence).ok is False


def test_the_naive_baseline_passes_no_evidence_at_all(by_name):
    """The original bug, restated as a row: nothing observed still reads green."""
    row = by_name["no_evidence_at_all"]
    assert row.naive_verdict == "pass"
    assert row.fused_verdict == "fail"
    assert row.channels == {}


def test_the_naive_baseline_never_escalates(outcomes):
    """Escalation needs two channels to compare. The straw man has none."""
    assert all(o.naive_verdict != "escalate" for o in outcomes)


# --- the properties each row is meant to demonstrate -----------------------

def test_unreadable_depth_abstains_rather_than_vetoing(by_name):
    """Both halves of the property, which need two rows to state.

    An unreadable region must not carry a pass on its own (first row), and must
    not overrule channels that did see something (second row). Contributing a
    zero would satisfy the first and break the second.
    """
    blind = by_name["depth_region_unreadable"]
    assert blind.depth_verdict == "unknown"
    assert blind.depth_abstained is True
    assert "depth" not in blind.channels
    assert blind.fused_verdict == "fail"

    witnessed = by_name["unreadable_depth_two_witnesses"]
    assert witnessed.depth_verdict == "unknown"
    assert witnessed.depth_abstained is True
    assert set(witnessed.channels) == {"torque", "vision"}
    assert witnessed.fused_verdict == "pass"


def test_escalation_carries_the_disagreement_and_the_number_it_avoided(by_name):
    """The contradiction row must escalate, and must show what averaging cost.

    ``would_have_fused_to`` clearing PASS_THRESHOLD is what makes this row a
    regression guard rather than a hypothetical: the mean of the contradicting
    channels is a pass.
    """
    row = by_name["cap_on_but_proxies_say_off"]
    assert row.fused_verdict == "escalate"
    assert row.fused_confidence == 0.0
    assert row.would_have_fused_to is not None
    assert row.would_have_fused_to >= PASS_THRESHOLD
    assert row.channels["depth"] == 0.0
    assert row.channels["torque"] > 0.8


def test_a_bench_bump_is_not_read_as_the_cap_moving(by_name):
    """Common-mode rejection, as a scored row.

    The cap marker sweeps across the frame and the datum sweeps with it. After
    subtraction the cap has not moved, so the vision channel must vote no. An
    unreferenced channel would have voted yes here and turned this row into an
    escalation instead of a clean fail.
    """
    row = by_name["bench_bumped_cap_never_moved"]
    assert row.vision_datum_referenced is True
    assert row.cap_marker_travel == pytest.approx(0.0, abs=1e-6)
    assert row.channels["vision"] == 0.0
    assert row.fused_verdict == "fail"


def test_the_clean_row_passes_on_three_agreeing_channels(by_name):
    """The control that catches a verifier which has learned to just say no."""
    row = by_name["clean_uncap"]
    assert row.fused_verdict == "pass"
    assert set(row.channels) == {"torque", "vision", "depth"}
    assert row.depth_verdict == "cap_off"
    assert row.fused_confidence >= PASS_THRESHOLD
    assert row.caught is None, "a must-pass control has no fault to catch"


# --- the numbers -----------------------------------------------------------

def test_summary_counts_add_up(outcomes, summary):
    assert summary.total == len(outcomes)
    assert summary.planted_faults + summary.controls == summary.total
    assert summary.fused_caught == summary.planted_faults
    assert summary.controls_passed == summary.controls
    assert summary.naive_caught < summary.fused_caught


def test_the_split_behind_the_headline_is_reported(outcomes, summary, capsys):
    """A caught fault with no channels was caught by failing closed, not by seeing.

    Both are worth having and they are not the same claim, so the total is only
    quotable next to the split. The split is derived from the channel dict rather
    than declared per row, which is why it cannot drift.
    """
    assert summary.caught_by_a_channel + summary.caught_by_failing_closed == (
        summary.fused_caught
    )
    assert summary.caught_by_a_channel >= 1, "no fault was caught by a measurement"
    assert summary.caught_by_failing_closed >= 1

    blind = {o.scenario for o in outcomes if o.caught and not o.channels}
    assert blind == {"depth_region_unreadable", "no_evidence_at_all"}

    adv.main([])
    out = capsys.readouterr().out
    assert "failing closed" in out
    assert "Not the same achievement" in out


def test_headline_is_the_declared_wording(summary):
    """The submission quotes this string, so its shape is pinned."""
    assert re.fullmatch(r"\d+ of \d+ planted faults caught", summary.headline())


def test_an_empty_or_faultless_table_is_not_a_green_gate():
    """Deleting the fault rows must not be the way to make the gate pass.

    "0 of 0 planted faults caught" is the shape of a harness that scored nothing,
    and it used to exit zero, which made removing the rows the cheapest way to a
    clean run. A control-only table is the same hole from the other side: it says
    nothing about whether a fault would be caught.
    """
    empty = adv.summarise(adv.run([]))
    assert empty.ok is False
    assert empty.structural
    assert "NOT VALID" in empty.headline()

    control_only = tuple(s for s in adv.scenarios() if s.expect is adv.Expect.PASS)
    assert control_only, "the real table has lost its must-pass controls"
    faultless = adv.summarise(adv.run(control_only))
    assert faultless.fused_caught == 0
    assert faultless.ok is False, "a table with no planted fault scored nothing"
    assert faultless.mismatches == [], "every row still met its own expectation"


def test_a_control_only_table_fails_the_cli(monkeypatch, capsys):
    """The same hole, through the exit code a hook reads."""
    control_only = tuple(s for s in adv.scenarios() if s.expect is adv.Expect.PASS)
    monkeypatch.setattr(adv, "scenarios", lambda: control_only)

    assert adv.main([]) == 1
    out = capsys.readouterr().out
    assert "NOT A VALID RUN" in out
    assert "no planted faults" in out


def test_the_headline_carries_its_own_verdict(monkeypatch, capsys):
    """A quoted number must say when the run behind it did not hold.

    The headline is the one line that travels without its table, so an invalid
    run has to be visible in the string itself rather than only in the exit code.
    """
    mislabelled = adv.Scenario(
        name="mislabelled_clean_uncap",
        expect=adv.Expect.FAIL,
        situation="a clean uncap declared as a fault, so the run is invalid",
        build=lambda: adv.cap_evidence(
            depth_distance_m=adv.STANDOFF_M + adv.CAP_HEIGHT_STEP_M,
            torque_nm=adv.TORQUE_COLLAPSED_NM,
            with_overview=True,
        ),
        vision_travel_px=adv.MARKER_TRAVEL_PX,
    )
    broken = adv.summarise(adv.run((mislabelled,)))
    assert broken.mismatches == ["mislabelled_clean_uncap"]
    assert "NOT VALID" in broken.headline()
    assert "mislabelled_clean_uncap" in broken.headline()

    monkeypatch.setattr(adv, "scenarios", lambda: (mislabelled,))
    assert adv.main(["--json"]) == 1
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["gate_passed"] is False
    assert "NOT VALID" in payload["headline"]


def test_the_table_is_reproducible():
    """Same number every run, or it is not evidence.

    The synthetic frames are seeded. An unseeded rng would make the headline
    drift between runs, and a drifting headline invites re-running until it looks
    good.
    """
    from dataclasses import asdict

    first = [asdict(o) for o in adv.run()]
    second = [asdict(o) for o in adv.run()]
    assert first == second


def test_the_depth_scale_is_applied_and_is_the_d405_value():
    """A 10x scale slip is the failure mode depth_height warns about.

    The reference must measure back to the standoff it was built at. With the
    D400 series' 1e-3 it would read 2.5 m and every row would still "work",
    just about a scene that does not exist.
    """
    assert adv.D405_DEPTH_SCALE_M_PER_COUNT == 1e-4
    assert adv.capped_reference().median_m == pytest.approx(adv.STANDOFF_M, abs=0.002)


# --- no cv2, no camera ----------------------------------------------------

def test_the_harness_never_calls_the_real_detector(monkeypatch):
    """Pins that the vision channel is the stub, in every row.

    If any scenario fell through to the real ``_marker_centre`` the scored table
    would depend on whether OpenCV is installed on the machine running it. The
    sentinel raises if it is ever reached, and is still in place afterwards,
    which also shows the seam is restored rather than left patched.
    """
    def sentinel(frame, marker_id):
        raise AssertionError("the harness fell through to the real detector")

    monkeypatch.setattr(agents_mod, "_marker_centre", sentinel)
    adv.run()
    assert agents_mod._marker_centre is sentinel


def test_the_seam_is_restored_even_when_verify_raises(monkeypatch):
    original = agents_mod._marker_centre
    evidence = adv.cap_evidence(torque_nm=adv.TORQUE_COLLAPSED_NM, with_overview=True)
    with pytest.raises(RuntimeError):
        with adv.stubbed_vision(evidence, adv.MARKER_TRAVEL_PX):
            raise RuntimeError("boom")
    assert agents_mod._marker_centre is original


# --- the CLI ---------------------------------------------------------------

def test_cli_exits_zero_and_labels_everything_simulated(capsys):
    """The output has to be unusable as a hardware claim.

    Someone will paste this table into a slide. It must carry its own provenance
    when it gets there.
    """
    assert adv.main([]) == 0
    out = capsys.readouterr().out
    assert "SIMULATED" in out
    assert "not a hardware" in out
    assert "STRAW MAN" in out
    assert re.search(r"\d+ of \d+ planted faults caught", out)
    for name in (s.name for s in adv.scenarios()):
        assert name in out


def test_cli_json_carries_the_provenance_too(capsys):
    import json

    assert adv.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence_provenance"] == "simulated"
    assert payload["not_a_hardware_measurement"] is True
    assert "straw man" in payload["naive_baseline"]
    assert len(payload["scenarios"]) == len(adv.scenarios())


def test_cli_exits_non_zero_when_a_row_misses_its_verdict(monkeypatch, capsys):
    """The gate, from the outside.

    A mis-declared row must break the command rather than print a wrong table
    quietly, since a commit hook is the only reader that cannot notice for itself.
    """
    mislabelled = adv.Scenario(
        name="deliberately_mislabelled",
        expect=adv.Expect.FAIL,
        situation="a clean uncap declared as a fault, to prove the gate bites",
        build=lambda: adv.cap_evidence(
            depth_distance_m=adv.STANDOFF_M + adv.CAP_HEIGHT_STEP_M,
            torque_nm=adv.TORQUE_COLLAPSED_NM,
            with_overview=True,
        ),
        vision_travel_px=adv.MARKER_TRAVEL_PX,
    )
    monkeypatch.setattr(adv, "scenarios", lambda: (mislabelled,))

    assert adv.main([]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "deliberately_mislabelled" in out
