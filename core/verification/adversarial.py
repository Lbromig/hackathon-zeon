"""Adversarial fault harness: plant a fault, then ask whether the verifier caught it.

Everything in this module is SIMULATED. The depth frames are synthetic numpy
arrays, the telemetry is a hand-written dict, and the marker detector is stubbed.
No camera, arm, tube or cap was involved in producing any number here, and the
output must not be presented as a hardware measurement. What the harness scores
is the decision logic in ``CapRemovedAgent``, nothing else. A verifier that
passes every row here can still be wrong on the bench, because the evidence it
was fed was written by us.

Why this exists. "The verifier is fail-closed" is an easy claim to make and a
hard one to check, because the failure it guards against is invisible in a happy
path: a verifier that returns True unconditionally looks identical to a working
one for as long as nothing goes wrong. So the only honest way to report the
layer is to plant faults on purpose and count. Each scenario below declares the
verdict a correct verifier must reach, and the CLI exits non-zero when any
scenario does not reach it, which makes the claim a gate rather than a sentence
in a README.

The contrast is the point. ``naive_verify`` stands in for what this layer did
before ``c29a76c``, one notch more charitably (see its own docstring): it reports
success from the controller's own belief and never looks at a sensor. It is a
straw man and is labelled one everywhere it appears. It is here because it is not
a hypothetical one: that is the code that shipped, and the planted faults are
exactly the ones it waves through.

Two words are used precisely:

- **caught** is the weak claim: the fused verifier did not report a pass.
- **met expectation** is the strong claim: it reached the exact declared verdict,
  including escalating rather than merely failing where escalation is required.

The gate is on the strong claim. The headline counts the weak one, because "did
not wave the fault through" is what an operator cares about first.

What the headline is not. Some rows are caught because a channel produced a
reading and that reading voted no, and some are caught because no channel
produced a reading at all and the layer fails closed. Those are not the same
achievement, and a single percentage hides the difference, so the summary splits
them and derives the split from whether any channel produced a reading rather
than from a label that could drift. Read the split before quoting the total.

The split says nothing about *which* channel caught a row, and that distinction
is worth more than the split is. Only depth measures the tube; torque infers from
the arm. On most of the caught rows below it is the arm's own torque trace that
votes no, which is a weaker result than an independent sensor contradicting an
arm that believes it succeeded. Exactly one row is the strong case. The counter
cannot tell them apart, so it does not claim to: read the per-row channels
(``-v``) before describing how a fault was caught.

Deliberately not covered, because every one of them would need a number this
repo has not measured:

- A cap broken loose but still resting on the mouth. That is a partial height
  change, and depth_height owns exactly one step (cap seated versus cap gone).
  Picking a partial lift would be inventing a distance, and the scenario would
  then be scoring the number we picked.
- Cross-threading, or a cap that binds and then frees. Needs a breakaway torque
  curve. Nothing in this repo has measured screw-cap torque, only that the
  controller reports joint torque at all.
- Liquid or condensation on the cap, which is the realistic reason a depth region
  drops out. The dropout fraction here is a blunt stand-in with no measured
  relationship to a wet cap.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from . import agents as agents_mod
from .agents import AGENTS, CapRemovedAgent, Evidence, VerificationResult
from .depth_height import HeightStat, measure_region

# --- simulated bench constants ---------------------------------------------
# Every value in this block describes the synthetic scene, not the real cell.
# None of them is a new physical constant: each is either read from a module
# that owns it, or copied from the existing depth-channel tests so the two agree.

# D405 reports 1e-4 m per count. The rest of the D400 series reports 1e-3, and
# using that here would make every distance ten times wrong in a direction that
# still looks plausible. See depth_height.measure_region.
D405_DEPTH_SCALE_M_PER_COUNT = 1e-4

# Region of the synthetic frame standing in for the tube mouth, and the frame
# size around it. Same values as backend/tests/test_cap_depth_channel.py, so the
# harness and that test are describing the same simulated scene.
DEPTH_FRAME_SHAPE = (80, 80)
CAP_ROI = (10, 10, 60, 60)

# Camera standoff. depth_height works its MIN_VALID_PIXELS budget at 250 mm, so
# this is that module's stated operating distance, not a measured one.
STANDOFF_M = 0.25

# How much further away the mouth sits once the cap is off. This is
# depth_height.compare's documented expected_delta_mm default (15 mm), which is
# where the number is owned. Not re-derived here.
CAP_HEIGHT_STEP_M = 0.015

# Per-pixel depth noise of the synthetic frames. TUNABLE: copied from the
# existing depth-channel tests. No bench measurement of D405 noise at this
# standoff backs it, so it sets how hard the simulated frames are, and nothing
# about the real camera.
DEPTH_NOISE_SIGMA_M = 0.0025

# Fraction of the region zeroed to simulate dropout on a shiny cap. TUNABLE and
# deliberately far past depth_height.MIN_VALID_FRACTION, so the scenario is
# unambiguously unreadable rather than sitting on the boundary. Boundary
# behaviour is test_depth_height.py's job.
DROPOUT_FRACTION = 0.95

# Simulated wrist torque, in Nm, as (peak while turning, reading afterwards).
# TUNABLE. Chosen relative to the agent's own MIN_UNSCREW_TORQUE_NM and DROP
# band, which own those thresholds. No measurement of an actual screw-cap
# breakaway torque exists in this repo, so these say "clearly loaded" and
# "clearly not", and no more than that.
TORQUE_COLLAPSED_NM = (2.0, 0.1)
TORQUE_STILL_LOADED_NM = (2.0, 1.9)
TORQUE_NEVER_LOADED_NM = (0.2, 0.01)

# Simulated cap-marker displacement, in pixels of the synthetic overview frame.
# TUNABLE, all four. The agent normalises by the frame diagonal and owns the
# MOVE_LO / MOVE_HI band; these are picked to land clearly outside it. No cap
# travel has been measured on the rig, so they encode nothing about the real one.
OVERVIEW_FRAME_SHAPE = (1000, 1000, 3)
MARKER_TRAVEL_PX = 300.0
CAP_MARKER_PX = (100.0, 100.0)
DATUM_MARKER_PX = (20.0, 20.0)

# A marker that does not move between the two frames, to reference cap travel
# against, so a simulated camera nudge does not read as the cap moving. 183 is
# MARKER_MAP's rack_1 entry, which markers.py labels an EXAMPLE association
# rather than a confirmed physical placement. Nothing in this repo records
# whether the rack is fixed to the bench, so "static" is a property of the
# simulated scene here and not a claim about the cell.
DATUM_MARKER_ID = 183

# Controller fault code for the one scenario where the arm itself notices. 31 is
# the code observed on the rig for the J5 wrist-versus-flange-camera collision
# (docs/OPEN_QUESTIONS.md, Q-JLIMIT-1). Used as an opaque non-zero flag.
CONTROLLER_ERROR_CODE = 31

TURNING_ARM = "right"
OVERVIEW_CAM = "overview_cam"

# Fixed seeds. The headline number has to be the same on every machine and every
# run, or it is not evidence.
SEED_REFERENCE = 1
SEED_OBSERVED = 2


# --- simulated evidence -----------------------------------------------------

def depth_frame(distance_m: float, *, dropout: float = 0.0, seed: int = 0) -> Any:
    """A SIMULATED uint16 depth frame of a flat surface at ``distance_m``.

    Same construction as backend/tests/test_cap_depth_channel.py: normal noise
    about the distance, divided by the device scale into counts, with dropout
    written as literal zeros because that is what librealsense reports for "no
    reading here". numpy is imported lazily to match the rest of core/.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    metres = rng.normal(distance_m, DEPTH_NOISE_SIGMA_M, size=DEPTH_FRAME_SHAPE)
    counts = np.clip(metres / D405_DEPTH_SCALE_M_PER_COUNT, 0, 65535).astype("uint16")
    if dropout:
        counts[rng.random(DEPTH_FRAME_SHAPE) < dropout] = 0
    return counts


def capped_reference(seed: int = SEED_REFERENCE) -> HeightStat:
    """The setup measurement: the mouth of a still-capped tube, SIMULATED."""
    return measure_region(
        depth_frame(STANDOFF_M, seed=seed), D405_DEPTH_SCALE_M_PER_COUNT, CAP_ROI
    )


def _overview_frame() -> Any:
    """A blank overview frame.

    Its contents do not matter: the detector is stubbed for every scenario, so
    the only thing the agent reads off this array is its diagonal, which is what
    normalises marker travel.
    """
    import numpy as np

    return np.zeros(OVERVIEW_FRAME_SHAPE, dtype="uint8")


def _arm_status(torque_nm: float, error_code: int) -> dict[str, Any]:
    """One device's status() snapshot, shaped like the real driver's.

    The 6-slot torque array matters: the driver trims the controller's fixed
    7-slot report to the axis count so that ``[-1]`` is the wrist rather than the
    unused trailing slot, and the agent depends on that.
    """
    return {
        TURNING_ARM: {
            "error_code": error_code,
            "effort": {"joints_torque": [0.0, 0.0, 0.0, 0.0, 0.0, torque_nm]},
        }
    }


def cap_evidence(
    *,
    depth_distance_m: float | None = None,
    depth_dropout: float = 0.0,
    torque_nm: tuple[float, float] | None = None,
    with_overview: bool = False,
    error_code: int = 0,
    reference: HeightStat | None = None,
    seed: int = SEED_OBSERVED,
) -> Evidence:
    """Assemble one SIMULATED Evidence bundle for CapRemovedAgent.

    Each argument switches a channel's raw input on or off rather than switching
    a channel's *verdict* on or off. That distinction is the whole value of the
    harness: the scenarios below state a physical situation, and the agent is
    left to draw its own conclusion from it. Handing it conclusions would score
    nothing.
    """
    frames: dict[str, Any] = {}
    before_frames: dict[str, Any] = {}
    expected: dict[str, Any] = {
        "turning_arm": TURNING_ARM,
        "overview_camera": OVERVIEW_CAM,
    }

    if depth_distance_m is not None:
        frames["depth"] = depth_frame(depth_distance_m, dropout=depth_dropout, seed=seed)
        expected |= {
            "depth_scale": D405_DEPTH_SCALE_M_PER_COUNT,
            "cap_roi": CAP_ROI,
            "cap_reference": reference if reference is not None else capped_reference(),
        }

    if with_overview:
        frames[OVERVIEW_CAM] = _overview_frame()
        before_frames[OVERVIEW_CAM] = _overview_frame()
        expected["datum_marker_id"] = DATUM_MARKER_ID

    telemetry: dict[str, Any] = {}
    during: list[dict[str, Any]] = []
    if torque_nm is not None:
        peak, final = torque_nm
        telemetry = _arm_status(final, error_code)
        during = [_arm_status(peak, error_code)]
    elif error_code:
        telemetry = {TURNING_ARM: {"error_code": error_code}}

    return Evidence(
        frames=frames,
        before_frames=before_frames,
        telemetry=telemetry,
        during=during,
        expected=expected,
    )


@contextmanager
def stubbed_vision(
    evidence: Evidence, travel_px: float | None, datum_travel_px: float = 0.0
) -> Iterator[None]:
    """Stand in for the cap-marker detector while one verify call runs.

    ``_marker_centre`` is the seam agents.py already uses to keep core/
    importable without OpenCV, so patching it needs no cv2 and no camera. It is
    patched for *every* scenario, including the ones with no vision channel:
    otherwise the scored table would change depending on whether OpenCV happens
    to be installed on the machine running it, and a headline number that moves
    with the environment is not a headline number.

    ``travel_px=None`` means the cap marker was not tracked at all.
    ``datum_travel_px`` moves the reference marker as well, which is how a camera
    knock or a leaned-on bench looks: everything in the frame shifts together.
    Left at zero the datum does not move, so the agent's common-mode rejection
    subtracts zero rather than being skipped. Whether the real fixture holds still
    is not recorded anywhere in this repo, so that is a choice about the simulated
    scene rather than a fact about the bench.
    """
    cam = evidence.expected.get("overview_camera", OVERVIEW_CAM)
    after = evidence.frames.get(cam)
    datum_id = evidence.expected.get("datum_marker_id")

    def fake(frame: Any, marker_id: int) -> tuple[float, float] | None:
        if frame is None or travel_px is None:
            return None
        if marker_id == CapRemovedAgent.CAP_MARKER_ID:
            if frame is after:
                return (CAP_MARKER_PX[0] + travel_px, CAP_MARKER_PX[1])
            return CAP_MARKER_PX
        if datum_id is not None and marker_id == int(datum_id):
            if frame is after:
                return (DATUM_MARKER_PX[0] + datum_travel_px, DATUM_MARKER_PX[1])
            return DATUM_MARKER_PX
        return None

    original = agents_mod._marker_centre
    agents_mod._marker_centre = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        agents_mod._marker_centre = original  # type: ignore[assignment]


# --- the naive baseline -----------------------------------------------------

NAIVE_NAME = "naive_commanded_ok"


def naive_verify(evidence: Evidence) -> VerificationResult:
    """STRAW MAN. The previous behaviour: trust the actuator's own belief.

    This is not a caricature invented to lose. Before ``c29a76c`` every agent in
    this layer was literally ``return VerificationResult(ok=True,
    confidence=0.0, detail="stub")``, so a disconnected camera and a completed
    step were indistinguishable to the orchestrator and all four steps reported
    green while the cell did nothing.

    Reproduced here one notch *more* charitably than the original, so the
    comparison is not rigged: it does read the one thing the controller actually
    reports about itself, its fault code, and fails when that is set. It reads no
    sensor. That is the entire difference between it and the real agent, and it
    is why it waves through every fault below except the one the arm noticed on
    its own: a cap that stayed on is not a controller fault. The arm finished the
    trajectory it was given and has no way to know the threads never let go.

    ``confidence=0.0`` on a pass is kept from the original, because that shape is
    the tell: anything downstream weighting results by confidence gave the old
    stub none, and passed it anyway.
    """
    arm = evidence.expected.get("turning_arm", TURNING_ARM)
    dev = evidence.telemetry.get(arm)
    code = dev.get("error_code") if isinstance(dev, dict) else None
    data = {"naive": True, "straw_man": True, "consulted_sensors": False}

    if code:
        return VerificationResult(
            False, 0.0, f"controller reported error_code {code}", data
        )
    return VerificationResult(
        True, 0.0,
        "move was commanded and the controller flagged no fault, so assume it worked",
        data,
    )


# --- scenario table ---------------------------------------------------------

class Expect(str, Enum):
    """The verdict a correct verifier must reach.

    ESCALATE is separate from FAIL on purpose. A fail is a verdict; an escalation
    is a refusal to give one, because two independent channels are telling
    opposite stories and their average would describe neither. Collapsing the two
    would let a verifier score full marks while quietly averaging a contradiction
    into a plausible-looking number, which is the exact bug the disagreement
    check was added to close.
    """

    PASS = "pass"
    FAIL = "fail"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Scenario:
    """One planted physical situation, and the verdict it demands.

    ``situation`` is the physical story in one line. It is not decoration: if the
    story and the evidence ever drift apart, the row is a lie regardless of what
    the verifier returns, and the story is the only part a reviewer can check.
    """

    name: str
    expect: Expect
    situation: str
    build: Callable[[], Evidence]
    vision_travel_px: float | None = None
    datum_travel_px: float = 0.0

    @property
    def planted_fault(self) -> bool:
        """True when a correct verifier must not pass this row.

        Note what this is not: a scenario can carry a degraded sensor and still
        demand a pass, because a depth region going unreadable during a genuinely
        successful uncap is not a fault in the uncap. Those rows are controls,
        and they are the ones that catch a verifier that has learned to say no.
        """
        return self.expect is not Expect.PASS


def scenarios() -> tuple[Scenario, ...]:
    """The table. Built fresh so no synthetic frame is shared between rows."""
    reference = capped_reference()

    return (
        Scenario(
            name="clean_uncap",
            expect=Expect.PASS,
            situation=(
                "nothing wrong: threads let go, wrist load collapsed, cap marker "
                "travelled, mouth receded by a cap's height"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M + CAP_HEIGHT_STEP_M,
                torque_nm=TORQUE_COLLAPSED_NM,
                with_overview=True,
                reference=reference,
            ),
            vision_travel_px=MARKER_TRAVEL_PX,
        ),
        Scenario(
            name="cap_still_on",
            expect=Expect.FAIL,
            situation=(
                "cap never came off: the arm ran the whole unscrew move and "
                "reported no fault, but the wrist stayed loaded and the mouth "
                "did not move"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M,
                torque_nm=TORQUE_STILL_LOADED_NM,
                reference=reference,
            ),
        ),
        Scenario(
            name="cap_on_but_proxies_say_off",
            expect=Expect.ESCALATE,
            situation=(
                "cap still on, and both proxies disagree: wrist load collapsed "
                "and the marker travelled (gripper slipped on the cap, say) "
                "while the only channel measuring the tube says unchanged"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M,
                torque_nm=TORQUE_COLLAPSED_NM,
                with_overview=True,
                reference=reference,
            ),
            vision_travel_px=MARKER_TRAVEL_PX,
        ),
        Scenario(
            name="bench_bumped_cap_never_moved",
            expect=Expect.FAIL,
            situation=(
                "cap still on and somebody knocked the camera: the cap marker "
                "swept across the frame, but so did the static datum marker, by "
                "the same amount. Shared motion is not the cap moving"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M,
                torque_nm=TORQUE_STILL_LOADED_NM,
                with_overview=True,
                reference=reference,
            ),
            vision_travel_px=MARKER_TRAVEL_PX,
            # Identical by construction, so the subtraction cancels exactly and
            # no bump magnitude has to be invented for this row to mean anything.
            datum_travel_px=MARKER_TRAVEL_PX,
        ),
        Scenario(
            name="depth_region_unreadable",
            expect=Expect.FAIL,
            situation=(
                "nobody could see: depth region dropped out on the shiny cap, "
                "the marker was out of view, and the wrist was never "
                "meaningfully loaded, so no channel observed anything"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M + CAP_HEIGHT_STEP_M,
                depth_dropout=DROPOUT_FRACTION,
                torque_nm=TORQUE_NEVER_LOADED_NM,
                reference=reference,
            ),
        ),
        Scenario(
            name="unreadable_depth_two_witnesses",
            expect=Expect.PASS,
            situation=(
                "control: the cap did come off and two channels saw it, while "
                "the depth region dropped out. The unreadable region must "
                "abstain, not veto the channels that did observe something"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M + CAP_HEIGHT_STEP_M,
                depth_dropout=DROPOUT_FRACTION,
                torque_nm=TORQUE_COLLAPSED_NM,
                with_overview=True,
                reference=reference,
            ),
            vision_travel_px=MARKER_TRAVEL_PX,
        ),
        Scenario(
            name="no_evidence_at_all",
            expect=Expect.FAIL,
            situation=(
                "everything unplugged: no frames, no telemetry, no reference. "
                "The step may well have worked, and that is the point: there is "
                "nothing here to say so"
            ),
            build=Evidence,
        ),
        Scenario(
            name="controller_faulted_mid_unscrew",
            expect=Expect.FAIL,
            situation=(
                "same physical state as cap_still_on, plus the controller "
                "flagged a fault. The one row the naive baseline also catches, "
                "because the arm noticed by itself"
            ),
            build=lambda: cap_evidence(
                depth_distance_m=STANDOFF_M,
                torque_nm=TORQUE_STILL_LOADED_NM,
                error_code=CONTROLLER_ERROR_CODE,
                reference=reference,
            ),
        ),
    )


# --- running the table ------------------------------------------------------

def classify(result: VerificationResult) -> Expect:
    """Which of the three outcomes a VerificationResult represents."""
    if result.ok:
        return Expect.PASS
    if "disagreement" in result.data:
        return Expect.ESCALATE
    return Expect.FAIL


@dataclass
class Outcome:
    """What one scenario produced. All fields describe SIMULATED evidence."""

    scenario: str
    expect: str
    situation: str
    fused_verdict: str
    fused_confidence: float
    fused_detail: str
    naive_verdict: str
    naive_detail: str
    channels: dict[str, float] = field(default_factory=dict)
    depth_verdict: str | None = None
    depth_abstained: bool = False
    cap_marker_travel: float | None = None
    vision_datum_referenced: bool | None = None
    would_have_fused_to: float | None = None
    met_expectation: bool = False
    caught: bool | None = None        # None on must-pass controls: nothing to catch
    naive_caught: bool | None = None

    @property
    def planted_fault(self) -> bool:
        return self.expect != Expect.PASS.value


def run_scenario(scenario: Scenario) -> Outcome:
    """Score one scenario against the registered agent and the straw man.

    The agent comes from ``AGENTS`` rather than a fresh instance, so the harness
    scores the object the orchestrator actually calls. A private copy could pass
    here while the wired-up one had regressed.
    """
    agent = AGENTS["cap_removed"]
    evidence = scenario.build()

    with stubbed_vision(
        evidence, scenario.vision_travel_px, scenario.datum_travel_px
    ):
        fused = agent.verify(evidence)
    naive = naive_verify(evidence)

    fused_verdict = classify(fused)
    naive_verdict = classify(naive)
    channels = dict(fused.data.get("channels") or {})
    depth_verdict = fused.data.get("depth_verdict")
    disagreement = fused.data.get("disagreement") or {}

    return Outcome(
        scenario=scenario.name,
        expect=scenario.expect.value,
        situation=scenario.situation,
        fused_verdict=fused_verdict.value,
        fused_confidence=round(fused.confidence, 3),
        fused_detail=fused.detail,
        naive_verdict=naive_verdict.value,
        naive_detail=naive.detail,
        channels={k: round(v, 3) for k, v in channels.items()},
        depth_verdict=depth_verdict,
        # Recorded rather than inferred: a depth read that happened and then
        # abstained is a different thing from one that never happened, and the
        # difference is exactly what "abstain, do not veto" means.
        depth_abstained=depth_verdict is not None and "depth" not in channels,
        cap_marker_travel=fused.data.get("cap_marker_travel"),
        vision_datum_referenced=fused.data.get("vision_datum_referenced"),
        would_have_fused_to=disagreement.get("would_have_fused_to"),
        met_expectation=fused_verdict is scenario.expect,
        caught=None if not scenario.planted_fault else not fused.ok,
        naive_caught=None if not scenario.planted_fault else not naive.ok,
    )


@dataclass
class Summary:
    """Counts over one run. Nothing here is a hardware result."""

    total: int
    planted_faults: int
    fused_caught: int
    naive_caught: int
    controls: int
    controls_passed: int
    # The split behind fused_caught. Derived from whether any channel produced a
    # reading, so it cannot drift out of step with the table: a row caught with
    # no channels at all was caught by failing closed, which is a weaker claim
    # than a channel having produced a reading that voted no. Neither counter
    # says which channel it was, and only depth measures the tube.
    caught_by_a_channel: int = 0
    caught_by_failing_closed: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def structural(self) -> list[str]:
        """Reasons the run is not evidence whatever the rows individually did.

        A table with no planted faults scores "0 of 0 planted faults caught" and
        would otherwise exit zero, so deleting every fault row is the one edit
        that turns the gate green by removing the thing it checks. A table with
        no must-pass control cannot tell a working verifier from one that has
        learned to refuse everything. Both are counted as failures here rather
        than reported as a clean run over an empty table.
        """
        problems: list[str] = []
        if not self.planted_faults:
            problems.append("no planted faults in the table, so nothing was scored")
        if not self.controls:
            problems.append(
                "no must-pass control in the table, so a verifier that fails "
                "everything would score full marks"
            )
        return problems

    @property
    def ok(self) -> bool:
        """True only when the table was worth running and every row met its verdict."""
        return not self.mismatches and not self.structural

    def headline(self) -> str:
        """The one line a submission quotes, carrying its own verdict.

        The count alone reads identically whether every row reached its declared
        expectation or none of them did, and this is the string most likely to be
        copied somewhere it cannot be checked. So an invalid run says so in the
        same breath as the number.
        """
        line = f"{self.fused_caught} of {self.planted_faults} planted faults caught"
        if self.ok:
            return line
        reasons = list(self.structural)
        if self.mismatches:
            reasons.append(
                f"{len(self.mismatches)} row(s) missed their declared verdict: "
                + ", ".join(self.mismatches)
            )
        return line + " [NOT VALID: " + "; ".join(reasons) + "]"


def summarise(outcomes: Sequence[Outcome]) -> Summary:
    faults = [o for o in outcomes if o.planted_fault]
    controls = [o for o in outcomes if not o.planted_fault]
    caught = [o for o in faults if o.caught]
    return Summary(
        total=len(outcomes),
        planted_faults=len(faults),
        fused_caught=len(caught),
        naive_caught=sum(1 for o in faults if o.naive_caught),
        controls=len(controls),
        controls_passed=sum(1 for o in controls if o.fused_verdict == Expect.PASS.value),
        caught_by_a_channel=sum(1 for o in caught if o.channels),
        caught_by_failing_closed=sum(1 for o in caught if not o.channels),
        mismatches=[o.scenario for o in outcomes if not o.met_expectation],
    )


def run(table: Sequence[Scenario] | None = None) -> list[Outcome]:
    """Run every scenario and return structured results. All evidence SIMULATED."""
    return [run_scenario(s) for s in (table if table is not None else scenarios())]


# --- CLI --------------------------------------------------------------------

BANNER = (
    "ADVERSARIAL FAULT HARNESS: cap_removed verifier\n"
    "ALL EVIDENCE BELOW IS SIMULATED: synthetic numpy depth frames, hand-written\n"
    "telemetry, and a stubbed marker detector. No camera, arm, tube or cap was\n"
    "involved. These numbers score decision logic only and are not a hardware\n"
    "measurement of the cell.\n"
    "The naive column is a STRAW MAN: it is this layer's own pre-c29a76c\n"
    "behaviour, which reported success from the controller's belief without\n"
    "consulting any sensor."
)


def _table_lines(outcomes: Sequence[Outcome]) -> list[str]:
    header = ("scenario", "expected", "naive", "fused", "caught")
    rows = [
        (
            o.scenario,
            o.expect,
            o.naive_verdict,
            o.fused_verdict,
            "n/a" if o.caught is None else ("yes" if o.caught else "NO"),
        )
        for o in outcomes
    ]
    widths = [max(len(r[i]) for r in (header, *rows)) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [
        line.rstrip()
        for line in (
            fmt.format(*header),
            fmt.format(*("-" * w for w in widths)),
            *(fmt.format(*r) for r in rows),
        )
    ]


def _report(outcomes: Sequence[Outcome], summary: Summary, verbose: bool) -> str:
    out = [BANNER, ""]
    out += _table_lines(outcomes)
    out += [
        "",
        f"fused verifier:  {summary.headline()}",
        f"  of those, {summary.caught_by_a_channel} had at least one channel produce "
        "a reading, and the verdict came from those readings,",
        f"  and {summary.caught_by_failing_closed} had no channel reading at all, so "
        "the layer was failing closed rather than sensing. Not the same achievement.",
        "  The split does not say which channel caught a row. Only depth measures "
        "the tube; torque infers from the arm. Run with -v before describing how any "
        "of these was caught.",
        f"naive baseline:  {summary.naive_caught} of {summary.planted_faults} "
        "planted faults caught",
        f"must-pass controls: {summary.controls_passed} of {summary.controls} passed",
    ]

    if verbose:
        out.append("")
        for o in outcomes:
            out.append(f"{o.scenario}: {o.situation}")
            channels = ", ".join(f"{k}={v:.2f}" for k, v in o.channels.items()) or "none"
            out.append(f"  channels: {channels}  confidence {o.fused_confidence:.2f}")
            if o.depth_verdict is not None:
                out.append(
                    f"  depth: {o.depth_verdict}"
                    + ("  (abstained, did not veto)" if o.depth_abstained else "")
                )
            if o.cap_marker_travel is not None:
                out.append(
                    f"  cap marker travel {o.cap_marker_travel:.4f} of the frame "
                    f"diagonal, datum referenced: {o.vision_datum_referenced}"
                )
            if o.would_have_fused_to is not None:
                out.append(
                    f"  averaging the contradiction would have returned "
                    f"{o.would_have_fused_to:.3f}"
                )
            out.append(f"  fused: {o.fused_detail}")
            out.append(f"  naive: {o.naive_detail}")

    if summary.structural:
        out.append("")
        out.append(
            "NOT A VALID RUN: the table itself does not support a claim, whatever "
            "the rows above say:"
        )
        out += [f"  {problem}" for problem in summary.structural]

    if summary.mismatches:
        out.append("")
        out.append(
            "MISMATCH: these scenarios did not reach their declared verdict, so the "
            "harness is not evidence of anything until they are resolved:"
        )
        by_name = {o.scenario: o for o in outcomes}
        for name in summary.mismatches:
            o = by_name[name]
            out.append(
                f"  {name}: expected {o.expect}, got {o.fused_verdict} "
                f"({o.fused_detail})"
            )
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the table. Non-zero exit when any scenario missed its verdict.

    Non-zero is the point of the CLI: it makes "the verifier catches planted
    faults" a thing a commit hook can check rather than a claim in a document.
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    outcomes = run()
    summary = summarise(outcomes)

    if "--json" in args:
        print(json.dumps(
            {
                "evidence_provenance": "simulated",
                "not_a_hardware_measurement": True,
                "naive_baseline": "straw man: pre-c29a76c behaviour, no sensor consulted",
                # asdict() drops properties, and a consumer reading only the
                # counts would not see that the run was invalid.
                "gate_passed": summary.ok,
                "structural_problems": summary.structural,
                "summary": asdict(summary),
                "headline": summary.headline(),
                "scenarios": [asdict(o) for o in outcomes],
            },
            indent=2,
        ))
    else:
        print(_report(outcomes, summary, verbose="--verbose" in args or "-v" in args))

    return 0 if summary.ok else 1


if __name__ == "__main__":  # python3 -m core.verification.adversarial [-v|--json]
    raise SystemExit(main())
