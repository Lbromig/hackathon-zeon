# Main paper skeleton

**Di Hu**, **Dale Herzog**, **Lukas Bromig**
*Author list and affiliations to be confirmed before submission.*

Companion documents:

| File | Role |
|---|---|
| `FINDINGS.md` | Evidence base. Fifteen findings, each with measurement, evidence, mitigation |
| `METHODS.md` | Apparatus, evidence classes, threats to validity |
| `SUPPLEMENT-methods-and-mitigations.md` | Detailed architecture, taxonomy, containment patterns, implementation |
| `CONTEXT-project-plan.md` | Primary source: the team's contemporaneous plan and open-questions log, plus candidate finding 16 |

This skeleton is the **main paper**. It should stand alone at ~8-10 pages and defer
implementation depth to the supplement.

---

## Title

Recommended: **Silent success: fifteen ways a laboratory robot reported an action it
could not have observed**

Alternatives, in descending preference:
- *When the dashboard is green and the sensor is blind: a field study of verification gaps in a bench-scale autonomous cell*
- *Command completion is not evidence*

The supplement carries the architectural title (*Verification-Gated Perception...*).
Keeping the main paper's title empirical and the supplement's prescriptive is the
right split: the finding is what earns attention, the architecture is what follows
from it.

---

## Abstract (draft)

Autonomous laboratory platforms rest on an implicit assumption: that the instrument
layer can report what physically happened. We tested that assumption directly, by
instrumenting a bench-scale cell — a gantry liquid handler, two six-axis arms, three
cameras — and recording every case in which the stack reported success without having
observed it. In one session we identified fifteen distinct mechanisms. **Nine produce
no software signal whatsoever that distinguishes success from failure**; five emit a
signal that actively misleads, naming a cause that is not the cause.

They are not exotic defects. They are the default behaviour of ordinary parts: a
stepper without an endstop, firmware that acknowledges on queue rather than on
arrival, an SDK that returns an empty collection instead of an error, a fusion rule
that averages a dissenting sensor into agreement, a retry policy that presupposes
knowledge of its own starting state. A subclass of five produces something worse than
absence — a **plausible wrong value**: a guard that cannot intercept the fault it
documents, a diagnostic that suppresses the remedy it prints, a depth scale differing
tenfold within one product family, one camera answering with two serial numbers, and
a test that passes only while the bug it names is present.

We report a reflexive case in which the supervising software agent, reasoning
correctly over silently corrupted position state, repeatedly and with escalating
confidence asserted a physical impossibility that the operator disproved by
insisting. We argue that verification must be a first-class sensing channel with
explicit provenance, that "unknown" must be representable and distinct from "false",
and that a recurring failure — correct behaviour recorded in a docstring, comment, or
test name but never made executable — is the tractable target for intervention.

---

## 1. Introduction

- The self-driving-lab thesis and its dependency on trustworthy actuation feedback.
- The gap in the literature: papers report *what was achieved*, rarely *how the
  system would have known had it not been*.
- Position: misclassification is the second problem. The first is asserting an
  observation that never occurred, which has no error rate because it has no
  observations.

### 1.1 Contributions

1. **An empirical catalogue** of fifteen mechanisms observed on operating hardware
   in a single session (`FINDINGS.md`).
2. **A severity taxonomy** — silent / loud-but-wrong / fragile — that predicts
   discoverability rather than merely describing impact.
3. **The plausible-wrong-value class** (findings 11-15), distinguished from
   detectable absence and argued to be the harder target.
4. **The inert-intent pattern**: across findings 6, 11 and 15 the correct behaviour
   was already written down and not enforced. This is the most actionable result in
   the paper.
5. **A reflexive case study** of an autonomous agent inheriting instrument blindness
   and laundering it into confident prose.

---

## 2. Apparatus and method

Condense from `METHODS.md`. Two points must survive compression:

- The hardware is **deliberately unremarkable**. The argument depends on these being
  defaults, not a pathological rig.
- The **evidence classes** must be stated explicitly: direct instrument reply, timed
  motion, operator observation, code inspection with a reproducing observation. In
  particular, timed motion is used *only* to detect moves that did not run, never to
  confirm one arrived — because with no endstop and no current sensing, a stalled
  axis is timing-identical to a clean move.

---

## 3. Results

Organised by severity, because severity predicts discoverability.

### 3.1 Silent (nine findings: 1, 4, 5, 6, 7, 10, 13, 14, 15)

Lead with the fusion arithmetic (finding 4). Torque 0.9, vision 0.9, and a
*confident* depth reading of 0.0 average to **exactly 0.600** against a 0.60 pass
threshold. The single channel measuring the tube directly is outvoted by two proxies
that agree with each other. It is memorable, it reproduces with no hardware, and it
generalises: averaging presumes channels measure the same thing and roughly concur.

Then finding 13 (tenfold depth scale within one product family) and finding 14 (one
camera, two serials) as the geometric analogues — wrong numbers that remain
physically plausible.

Close on finding 15, which turns the lens on the tests themselves.

### 3.2 Loud but wrong (five findings: 2, 8, 9, 11, 12)

Unifying theme: **the error names the wrong cause.** "No cameras attached" for four
attached-and-refused cameras. A clean move report for a move that never happened. A
health endpoint timing out because of an unrelated camera. A guard documenting a
contract it cannot honour. A diagnostic recommending `sudo` while containing the
guard that makes `sudo` fail.

Finding 11 is the strongest single result here and may deserve promotion to its own
subsection: opening a UI tab terminated the control server, because `except` cannot
intercept SIGSEGV.

### 3.3 Fragile (finding 3)

State that is valid within an epoch, silently invalid across one, with no epoch
marker available at any layer.

---

## 4. The reflexive case: an agent inheriting instrument blindness

Finding 3, told in full and plainly, including the incorrect assertions verbatim.

The argument to make carefully: the agent's refusals were **safety-motivated and
would normally be correct behaviour**. It declined a commanded move, cited measured
envelope figures, and reasoned explicitly about grinding a stepper against a hard
stop. Nothing in its reasoning was careless. The defect was misplaced confidence in a
*derived* quantity — an absolute position accumulated across two silent counter
re-zeroings — compounded by comparing it against out-and-back travel measurements
that were never machine coordinates.

The conclusion is not that supervisory agents are unsafe. It is that adding a
reasoning layer **does not create observability where none exists**; it converts its
absence into fluent, confident, and wrong natural language. Systems that reason over
instrument state must mark derived quantities as derived, and propagate that marking
into what they assert.

This section is the paper's most transferable result for the LLM-agent audience and
should not be buried in the catalogue.

---

## 5. The plausible-wrong-value class

Findings 11-15. Argue that these are categorically harder than absence:

- An absent signal can be detected by asserting presence.
- A **plausible** signal defeats presence checks, type systems, and range checks,
  because the value is well-formed and physically reasonable. A tube at 25 cm reading
  as 2.5 m is a number, not a fault.

Corollary for practice: prefer an absent value to a substituted one. The enumeration
helper returning `None` rather than a plausible default, with a test enforcing it, is
the pattern worth generalising.

---

## 6. The inert-intent pattern

Short section, high value. Across findings 6, 11 and 15, the correct behaviour was
**already written down** — in a docstring ("reported as `error`, not raised"), in a
comment ("one or the other, never a crash"), in a test name
(`test_enumeration_never_raises_with_no_camera`) — and was not enforced. In each case
the author knew the property. The property was documentary, not executable.

This reframes the intervention from "know more" to "make what you already know
executable", which is tractable in a way that exhortations to greater care are not.

---

## 6b. The deferral pattern: the same failure at team scale

Candidate finding 16, sourced from `CONTEXT-project-plan.md`.

The team's own open-questions log documents, across roughly twelve review cycles,
that work buildable and testable anywhere advanced steadily while the items requiring
physical presence at the bench stood still. The liquid handler's serial transport —
logged as the "#1 THREAT" and the "LONE room-only deciding item" — remained unmerged
for **six consecutive cycles**, while a dual-arm ratchet-unscrew with twelve tests,
world-frame calibration, a kinematics loop, and a remote camera driver all landed.

This is the paper's thesis at organisational scale. Work whose correctness can be
established without the physical world advances quickly and registers as progress on
every available indicator: commits, passing tests, annotated milestones. Work that
requires the physical world to answer accumulates as deferred risk and registers as
nothing at all.

Two things make this admissible rather than merely anecdotal. The team **identified
and named the pattern about itself, in writing, while it was happening** — and it
recurred anyway, which is the evidence that it is structural rather than a lapse of
attention. And the log states the consequence in terms that anticipate this study
exactly: with the transport unmerged, `aspiration_ok` "would pass a green check over
nothing."

Argue this without blame. Documenting one's own deferral pattern in public during a
24-hour build is better practice than most projects manage. The claim is that
verification-deferrable work outcompetes verification-requiring work under time
pressure, by default, and that this needs a structural remedy rather than more
diligence.

---

## 7. Limitations

State plainly and resist filling:

- **Single site, single session, n=1 cell.** Mechanisms are general; frequency is not
  established and no prevalence claim is made.
- **The plunger axis was never identified.** Both candidate axes accept the command
  and complete on schedule; no software signal distinguishes them and the
  disambiguating observation requires a human eye. Aspirate and dispense remained
  blocked; **no liquid was moved at any point.**
- **No camera-to-robot transform was ever fitted.** No detection was ever converted
  into a motion. Tooling to identify the moving element in a camera view was built
  and is described, but never executed: the vision backend would not hold a stream
  long enough to capture two comparable frames (finding 9).
- **No depth or camera intrinsics** were available on the streams that did work,
  ruling out metric hand-eye calibration and constraining any mapping to a planar
  approximation.
- **The teach point was recorded but never replayed**, because replay requires a
  homed datum that was never established.
- Several verification thresholds are tunable and explicitly unsourced; they are
  marked as such in code rather than presented as measured.
- **The observer was part of the system.** The agent that recorded the findings
  produced one of them. That case is reported from the transcript rather than
  reconstructed.

---

## 8. Discussion and conclusion

Verification is not a feature of an autonomous lab; it is the precondition. A
platform that cannot fail loudly cannot be trusted to succeed quietly.

The practical ask is modest and specific: report provenance with every state claim;
make "unknown" representable and distinct from "false"; make the absence of a sensor
visible in the data model rather than assumed away; and make documented intent
executable.

---

## Figures

1. **The fusion arithmetic.** Three channels, the mean landing exactly on the
   threshold, direct-measurement channel highlighted. One panel, high impact. Lead
   figure.
2. **Severity vs discoverability.** Fifteen findings placed on axes of "distinguishing
   signal exists" and "found by testing / found by operating". Should show the
   silent cluster falling entirely outside what non-physical testing reaches.
3. **The counter-epoch timeline.** Commanded vs believed vs physical position across
   the session, the two re-zero events marked, the divergence producing the false
   refusal shaded, and the operator override annotated.
4. **Apparatus schematic.** Deliberately plain, supporting the unremarkable-hardware
   point.

## Data and code availability

Motion tooling, verification agents, and the datum-guarded teach-point implementation
are in the team repositories, with paths cited throughout `FINDINGS.md`. Timing
measurements reproduce with the scripts named there. The fusion result reproduces
from the channel values alone with no hardware.

## Open drafting questions

- Does the reflexive case (Section 4) lead the paper or sit mid-body? It is the most
  novel material for an agent-focused venue and the least conventional for a
  laboratory-automation one. Venue choice should decide this, not prose preference.
- Findings 13 and 14 are vendor-specific. Keep them concrete and named, or abstract
  them to the general pattern? Recommendation: keep concrete — the specificity is the
  evidence, and abstraction would make them unfalsifiable.
- Section 6 could be the paper's actual thesis rather than a late section. Worth
  testing that structure once the results are drafted.
