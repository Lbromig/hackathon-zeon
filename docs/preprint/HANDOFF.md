# Handoff for the write-up

Prepared 2026-07-26 for Lukas. Read this first; it says what each document is, how far
it can be trusted, and what has to happen before submission.

---

## Read in this order

| # | File | What it is | State |
|---|---|---|---|
| 1 | `CORRECTIONS.md` | Adversarial fact-check: 30 must-fix, 12 should-fix | **Read before anything else.** 7 applied, the rest outstanding |
| 2 | `FINDINGS.md` | Evidence base, 15 findings | Substantially complete; some corrections applied |
| 3 | `DRAFT.md` | Main paper skeleton | Structure current as of 15 findings; prose not written |
| 4 | `SUPPLEMENT-methods-and-mitigations.md` | Architecture, taxonomy, containment patterns | Complete draft, carries most outstanding corrections |
| 5 | `METHODS.md` | Apparatus, evidence classes, threats to validity | Complete, unreviewed |
| 6 | `AUDIT-vision-to-pipette.md` | Multi-agent audit: can a detection command the pipette | New; provenance-labelled per claim |
| 7 | `CONTEXT-project-plan.md` | Your plan and open-questions log, verbatim, as a primary source | Complete; source for finding 16, now written up in FINDINGS.md |
| 8 | `README.md` | Orientation | Item 23 corrected 2026-07-26; now states evidence class by finding |
| 9 | `ARTIFACTS.md` | Claim-to-commit map, branch heads, PR states, test counts | New. Use to check any code claim without re-deriving it |
| 10 | `figures/` | Six frames captured on the bench, with `PROVENANCE.md` | New. The only unambiguously hardware-observed evidence in the folder |

---

## Three things to fix before anyone else reads this

**1. `README.md` overclaimed. FIXED 2026-07-26.** It stated everything here was
observed on hardware, which `CORRECTIONS.md` item 23 shows is false for most of the
evidence blocks. It now breaks the folder down by evidence class: observed on
hardware, observed in software on this repository, established in simulation only,
and not established at all. The correction is stated rather than silently applied,
since a folder arguing that systems assert unmade observations is the worst place to
do it quietly. **Re-check the class assignments** — they were made by the author of
this note and are not independently reviewed.

**2. The taxonomy is never validated** (`CORRECTIONS.md` item 7), and **no
false-refusal rate is reported for a fail-closed system** (item 32). Both are
structural, not wording. A fail-closed design whose refusal rate is unmeasured is a
design proposal, not a result.

**3. Item 41 is the one to think hardest about.** A paper that announces its own
honesty converts every unfixed gap into a broken promise. Either close the gaps or
lower the rhetorical register. The current draft does the former in places and the
latter nowhere.

Item 42 flags a review request that is *not* an author error and should not be
"fixed". Don't apply corrections mechanically.

---

## What is genuinely established, and what is not

**Measured on hardware, first-hand:**

- No endstop reports on any axis; `M119` unchanged over 18 s with the switch held
  closed. Homing burns a fixed search: Z twice in a row took 5.44 s then 6.56 s.
- A halted board acknowledges and discards. A 15 mm jog "completed" in 1.00 s against
  3.00 s expected, `M119` replied `!!`, **and the counter still advanced 15 mm**. After
  `M999`, the same step ran in 3.02 s.
- Position counters re-zero unpredictably: seen persisting across a port reopen, and
  seen zeroing both across a replug and between commands with no replug at all.
- Motion timings across roughly 40 commanded moves, consistently within a few tenths
  of a percent of prediction — which establishes only that moves *ran*.
- One live detection payload: tag 180 resolved to `left_base`, confidence 1.0, at
  14.3 fps, with `camera_xyz`, `depth_m` and `intrinsics` all null.

**Not established, and must not be implied:**

- **No liquid was ever moved.** The plunger axis was never identified — both candidates
  accept the command and complete on schedule, and the disambiguating observation
  needs a human eye. Aspirate and dispense stayed blocked all session.
- **No detection ever caused a motion.** The teach points in `data/teach_poses.json`
  were reached by a human watching a video feed and giving directions. That is
  teleoperation. Say so.
- **No camera-to-robot transform was ever fitted.**
- **The teach point was recorded but never replayed**, because replay needs a datum
  that was never established.
- Several verification thresholds are tunable and unsourced. They are marked as such
  in code; keep them marked in prose.

---

## The strongest material

In order of how much attention it will earn:

**The fusion arithmetic.** Torque 0.9, vision 0.9, and a *confident* depth reading of
0.0 average to exactly **0.600** against a 0.60 threshold. The only channel measuring
the tube directly is outvoted by two proxies that agree with each other. Reproduces
with no hardware. This is the lead figure.

**The reflexive case** (`FINDINGS.md` finding 3). The supervising agent tracked
absolute position by accumulating relative moves across two silent counter
re-zeroings, then refused a commanded move as physically impossible — five or six
times, with escalating confidence and explicit reasoning about grinding a stepper.
The operator overrode it and the gantry travelled another 50 mm. Every command had
been correct; the arithmetic had inherited the instrument's blindness.

`AUDIT-vision-to-pipette.md` documents a **second instance of the same pattern** from
later the same night: the same agent repeatedly advised homing the machine — which
grinds a stepper — as a prerequisite for vision-guided motion, when a visual
correction is purely relative and needs no datum at all. Two independent instances
make it a pattern rather than an anecdote.

**Q-ALLOC-1, the source for what is now finding 16** (`CONTEXT-project-plan.md`). Your own log
records that buildable-anywhere work advanced steadily while room-only deciding items
stood still — the OT transport unmerged for six consecutive cycles. You named the
pattern about yourselves while it was happening and it recurred anyway, which is what
makes it structural rather than a lapse. Your line deserves the abstract: with the
transport unmerged, `aspiration_ok` "would pass a green check over nothing."

---

## One thing to action outside the paper

`AUDIT-vision-to-pipette.md` establishes that **`core/calibration/deck_homography.py`
already exists** — pixel-to-deck-millimetres without depth, 201 lines plus 164 lines
of tests, pure numpy — on `lukas/fix-cameras-api-segfault`, commit `b4b635c`. It is
not an ancestor of the working branch, and 365 insertions with zero deletions means it
cherry-picks cleanly:

    git cherry-pick b4b635ca15f2155b134fe0e9e7de11c11c46492e

That is the capability the team spent the evening describing as missing, and part of
which was rewritten from scratch elsewhere. It also belongs in the paper, next to
Q-COMMIT-2: the cost of unmerged work is not only risk of loss, it is duplicated
effort by people who cannot see it.

---

## Suggested division

The empirical catalogue and the reflexive cases are written and need editing, not
research. The two structural gaps — validating the taxonomy, and measuring a
false-refusal rate — need work that does not exist yet, and one of them needs the
bench. If time forces a choice, the taxonomy validation is the one a reviewer will
demand, because without it the severity classes are assertion rather than result.

Machine state at handoff: the OT-One is off the USB bus, nothing in motion, tip
fitted. The last complete run was 40 of 50 Z cycles at 80.6 s against 80.0 s expected;
the final batch aborted on `Device not configured`, which is the board vanishing, not
a motion fault.
