# False green: when laboratory automation reports success it cannot have observed

Working material for a preprint, assembled from a single instrumented session on a
bench cell (2026-07-25/26) comprising an Opentrons OT-One gantry, two UFactory
xArm 6 arms, and three cameras, driven through a FastAPI/PyLabRobot-adjacent
stack.

## The claim

In open-loop laboratory automation, **a successful software signal is not evidence
of a successful physical action**, and the gap is systematic rather than
incidental. Across one session we recorded fifteen distinct mechanisms by which the
control stack reported success while the physical state was unknown, wrong, or
unchanged. Each was found by operating real hardware, not by code review.

Findings 11 to 15 were added from a parallel mitigations effort and form a class of
their own: each produces a **plausible wrong value** rather than a detectable
absence — an error guard that cannot intercept the error it documents, a diagnostic
that suppresses the fix it prints, a depth scale differing tenfold inside one product
family, one camera reporting two serial numbers, and a test passing for the wrong
reason.

The mechanisms are not exotic bugs. They are the default behaviour of ordinary
components: a stepper without an endstop, a firmware that acknowledges on queue
rather than on arrival, an SDK that returns an empty list rather than an error, a
sensor-fusion rule that averages, a retry policy that assumes it knows where it
started.

## Why it matters for autonomous labs

The field's stated direction is closed-loop, self-driving experimentation. Every
such loop rests on the assumption that the instrument layer can report what
happened. This material argues that assumption is routinely false, that the
failure is silent by construction, and that verification therefore has to be
designed as a first-class channel rather than inferred from command completion.

The sharpest evidence is reflexive: during the session, the **reasoning agent
itself** produced a false green. Tracking the gantry's absolute position by
accumulating relative moves, it repeatedly and confidently told the operator that
the axis had reached its mechanical limit and that a requested move was
physically impossible. The operator insisted; the move ran; the gantry travelled
another 50 mm. The commands had all been correct. The arithmetic had inherited
the instrument's blindness, because the position counters had silently re-zeroed
twice. See `FINDINGS.md`, finding 3.

## Contents

| File | What it holds |
|---|---|
| `FINDINGS.md` | The fifteen mechanisms, each with how it was measured, the evidence, and where the mitigation landed |
| `DRAFT.md` | Preprint skeleton: argument, structure, section-by-section content |
| `METHODS.md` | Apparatus, instrumentation, and what "measured" means for each claim |
| `SUPPLEMENT-methods-and-mitigations.md` | Mitigations developed at more length than a findings entry allows: the acquisition failure taxonomy, subprocess containment for a non-crash-safe SDK, the height-delta closure measurement, and a depth-free planar homography for vision-guided motion. Not a competing draft; where it disagrees with `FINDINGS.md`, `FINDINGS.md` wins |

## Provenance and honesty constraints

**This folder mixes evidence classes, and an earlier version of this line claimed it
did not.** It read "everything here was observed on hardware in one session", which
`CORRECTIONS.md` item 23 establishes is false for most of the evidence blocks. The
claim is corrected here rather than quietly deleted, because a folder arguing that
systems assert observations they did not make is the worst possible place to do the
same thing.

What is actually true, by class:

- **Observed on hardware.** The instrument behaviours in `FINDINGS.md` 1, 2, 3 and 9;
  the acquisition results in the supplement's Section 4.1; every image in `figures/`.
- **Observed in software, on this repository.** Findings 4 to 8 and 10 to 16. These
  are real defects in real code, established by reading and running it, not by
  operating the machine.
- **Established in simulation only.** Every method in the supplement's Sections 3.5
  and 3.6, and all 184 tests. No camera and no robot were attached.
- **Not established at all.** The closure measurement on a physical container, the
  hand-eye calibration, and any commanded manipulator motion. See the supplement's
  Section 6.

Where a number is measured it is stated as measured; where it is inferred it is
marked inferred.
Two claims that a reader might expect are deliberately absent because they were
never established: the plunger axis was never identified (both candidate axes
accept the command and complete on schedule, so no software signal distinguishes
them), and no camera-to-robot transform was ever fitted, so no detection was ever
converted into a motion. Those are limitations, reported in
`DRAFT.md`, not gaps to be filled with plausible values.

Source repositories are private/shared team repos; file paths are cited so
collaborators can verify, and code excerpts are reproduced only where needed to
support a claim.
