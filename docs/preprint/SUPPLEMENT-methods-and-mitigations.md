> **Status: supplement, not a competing draft.**
>
> `DRAFT.md` is the paper. It is a field study: fifteen measured mechanisms by which
> the stack reported success without having observed it, and it is the stronger frame
> for this material because the mechanisms were found by operating hardware rather
> than by reasoning about architecture.
>
> This document is the *mitigations* half, written in parallel before the two efforts
> were reconciled. It is retained because it develops four things at more length than
> a findings entry allows: the acquisition failure taxonomy (its Section 3.2), the
> subprocess containment pattern for a vendor SDK that faults rather than raising
> (3.3), the height-delta closure measurement and its refusal conditions (3.5), and
> the depth-free planar homography that permits vision-guided motion with no metric
> depth (3.6).
>
> Where the two disagree, `FINDINGS.md` wins: it is closer to the measurements.
> Findings 11 to 15 there were extracted from this document. Its Sections 1, 2, 7 and
> 8 substantially duplicate `DRAFT.md` and should not be carried into the paper.

---

# Verification-Gated Perception for Laboratory Robotics: Failing Closed When the Sensor Cannot See

**Di Hu**, **Dale Herzog**, **Lukas Bromig**

*Author list and affiliations to be confirmed before submission.*

---

## Abstract

Autonomous laboratory systems increasingly assert that a physical action succeeded:
a cap was removed, a tube was seated, a volume was transferred. Such assertions are
usually produced by a perception module that consumes a camera frame and emits a
verdict. We argue that the dominant failure mode of these systems is not
misclassification but **silent success**: a verification step that observes nothing
and reports that the action worked. A misclassification is visible in an error rate.
A verification step with no sensor behind it is invisible, because every software
signal along the path reports normally.

We describe a verification-gated architecture in which no downstream component may
claim to have observed anything until a preflight has established that a sensor was
attached, that the process could open it, and that frames were actually delivered.
We report four contributions. First, a failure taxonomy in which every sensor
acquisition failure resolves to exactly one named diagnosis carrying one operator
action, distinguishing conditions that are routinely conflated: a camera that is
absent, one the operating system refuses, and one held exclusively by another
process. Second, a containment pattern for vendor SDKs that are not crash-safe: a
bundled librealsense build (2.56.5) on macOS terminates on SIGSEGV rather than
raising an exception when it cannot claim a device's USB interfaces, so
`try`/`except` guards around it are decorative and a camera enumeration call can
terminate an entire control server. Third, a
fail-closed policy for verification agents, motivated by an instance in which four
agents returned `ok=True` with `confidence=0.0` from unimplemented stubs, rendering
a disconnected camera and a successful step indistinguishable to the orchestrator.
Fourth, a depth-free planar homography that permits vision-guided positioning when
metric depth is unavailable, with explicit refusal conditions.

We also catalogue a class of unit and identity errors that produce confidently wrong
positions rather than obvious failures, including a depth scale that differs by a
factor of ten within one product family, and a depth camera that reported two
mutually inconsistent serial numbers depending on which layer was queried.

We are explicit throughout about which results were measured on hardware and which
were established only in simulation. The central claim of this work concerns how a
system should behave when it cannot see, and it would be self-defeating to overstate
what we observed.

**Keywords:** laboratory automation, robotic verification, machine perception,
fail-safe design, RGB-D sensing, hand-eye calibration

---

## 1. Introduction

A robot that manipulates laboratory consumables must answer a question after every
physical step: did that work? The question is not rhetorical. Screw caps bind and
snap. Tubes sit proud in racks. Foil seals tear partially. Pipette tips fail to
seat. In each case the commanded motion completes, the controller reports success,
and the physical world does not match the plan.

Perception is the usual answer. A camera observes the workspace, a detector or
classifier consumes the frame, and a verdict is produced. Considerable attention has
been paid to making that verdict accurate. We argue that accuracy is the second
problem, and that the first is more elementary and more dangerous.

### 1.1 Silent success

Consider a verification agent implemented as follows, which we encountered in a
working codebase:

```python
def verify(self, evidence: Evidence) -> VerificationResult:
    # TODO: vision (threads/cap-gone classifier) + force telemetry fusion.
    return VerificationResult(ok=True, confidence=0.0, detail="stub")
```

The agent was registered in the orchestrator's dispatch table and called after every
uncapping step. The orchestrator gated on `result.ok`. Every step therefore passed,
and the run log recorded a chain of successful verifications, while nothing had been
observed at all.

This is worse than a crash and worse than a wrong answer. A crash halts the run. A
wrong answer appears in an error rate and can be measured. A verification step with
no sensor behind it produces a clean, plausible, permanently green record. Its
failure signature is identical to correct operation.

We take **silent success** to be the primary hazard in verified autonomy, and we
organise the rest of this paper around structural measures that make it
unrepresentable rather than merely unlikely.

### 1.2 Why the sensor layer is the right place to intervene

It is tempting to address silent success at the model level, by requiring
classifiers to report calibrated uncertainty. This is necessary but insufficient. A
classifier's uncertainty is conditioned on receiving an input. When the camera is
absent, refused by the operating system, or held by another process, there is no
input, and a well-calibrated model has nothing to be uncertain about.

The intervention therefore belongs below the model, at acquisition. Our position is
that a verification system must be able to distinguish three states, and that most
systems collapse them into two:

1. The sensor observed the workspace and the action succeeded.
2. The sensor observed the workspace and the action failed.
3. **The sensor did not observe the workspace.**

State 3 is not a variety of state 2, and it is not an error to be retried. Retrying
assumes the action did not take effect. When the physical state is unknown, a retry
proceeds from an unknown starting condition, which for a screw cap can mean applying
further torque to a cap that has already snapped.

### 1.3 Contributions

- A **preflight gate** (Section 3.1) that must pass before any component may assert
  a physical observation, with an exit code suitable for machine gating.
- A **failure taxonomy** (Section 3.2) mapping each acquisition failure to one
  operator action, separating conditions routinely conflated in vendor error
  messages.
- A **containment pattern** (Section 3.3) for vendor SDKs that terminate the process
  instead of raising, with measurements of the fault.
- A **fail-closed verification policy** (Section 3.4) in which absent evidence is
  never a pass, and an unimplemented checker cannot report success.
- A **height-delta verification method** (Section 3.5) for container closure state,
  chosen because it is invariant to illumination and requires no fiducial on the
  object.
- A **depth-free planar homography** (Section 3.6) for vision-guided motion, with
  refusal conditions that address a degeneracy invisible to residual error.
- A **catalogue of confidently-wrong-value hazards** (Section 5), each of which
  produces plausible incorrect geometry rather than a detectable failure.

---

## 2. Background and related work

We situate this work informally rather than attempting a comprehensive survey, and
we cite only methods and specifications we can name precisely.

**Fiducial-based pose estimation.** Square-marker systems, including ArUco and
AprilTag families, are standard for workspace localisation and are implemented in
OpenCV's `objdetect` module. The `tag36h11` family is used in the system described
here. Marker approaches require the marker to remain attached, unoccluded, and
sufficiently resolved, which is a meaningful constraint in wet laboratory settings.

**Rigid transform estimation.** The Kabsch algorithm recovers the optimal rotation
between two paired point sets under least squares, and is the standard basis for
hand-eye calibration from paired observations.

**Planar homography estimation.** The direct linear transform recovers a
plane-to-plane projective mapping from four or more point correspondences, without
requiring camera intrinsics. Coordinate normalisation prior to the linear solve is
standard practice for conditioning.

**Structured-light and stereo RGB-D sensing.** The Intel RealSense D400 series
provides stereo depth with an optional infrared projector. Device-specific minimum
range, depth scale and noise characteristics are given in the product family
datasheet (document 337029-017), and we reproduce the values relevant to close-range
work in Section 4.

**Fail-safe design.** The principle that a system should assume the unsafe state when
its sensing is absent is long established in safety engineering. Our contribution is
not the principle but its application to software verification agents, where the
default in practice is frequently the opposite.

---

## 3. Methods

### 3.1 The preflight gate

We implement acquisition verification as a standalone program with no dependency on
the application framework. This is deliberate. The two questions "is the application
broken?" and "is the camera unavailable?" must remain separable, and a preflight that
imports the application cannot answer the second when the first is true.

The gate performs three checks in order:

1. **Enumeration without acquisition.** Attached cameras are enumerated from
   operating-system registries, opening no stream. On macOS this uses
   `SPCameraDataType` for the capture-layer view and the IOKit registry for USB
   properties. This step uses only the language standard library, so it runs on a
   bare interpreter with no virtual environment.
2. **Acquisition attempt per backend.** Each available capture backend is tried and
   its outcome resolved to a diagnosis (Section 3.2).
3. **Verdict.** The process exits zero only if frames were actually delivered. A
   run harness can therefore gate on the exit code.

The design rule is that **a failure must name its own cause**. "Could not open
camera" is not actionable at a bench. Each diagnosis carries exactly one operator
action, and the remedy table is stored adjacent to the diagnosis enumeration so a
diagnosis cannot be added without an action to accompany it.

### 3.2 A failure taxonomy for sensor acquisition

| Diagnosis | Physical meaning | Operator action |
|---|---|---|
| `no_device` | Nothing attached | Cable, port, or hub |
| `permission_denied` | Attached; the OS refuses this process | Grant, or launch from a process that can be granted |
| `exclusive_access` | Attached and found; another process owns it | Release the holder, or elevate |
| `driver_claimed` | The class driver holds the interfaces | Use the OS capture path, or another host |
| `no_raw_usb` | This process cannot enumerate USB at all | Launch differently |
| `backend_missing` | The library is absent in this interpreter | Install |
| `opened_but_no_frames` | Opened, delivered nothing | Contention, or an under-negotiated link |

Three of these exist because of specific conflations that cost significant
diagnostic effort.

**`no_raw_usb` versus `no_device`.** When a userspace USB library cannot enumerate at
all, librealsense reports `No device detected. Is it plugged in?`. This reads as a
hardware fault and directs the operator to inspect cabling that is correct. The
preflight therefore queries the total device count from the USB library before
believing that message: a count of zero means the process lacks USB visibility
entirely, which is not a statement about the camera.

**`exclusive_access` versus a hardware fault.** The SDK message
`failed to set power state` indicates that the device was found and that claiming it
was refused. No configuration change affects this, and it is not a permission in the
operating system's access-control sense.

**Link speed as a first-class check.** A D400-series camera that negotiates USB 2
enumerates and opens normally, then fails under load. Because the failure appears
mid-run rather than at initialisation, the negotiated link rate is reported at
preflight. We treat "works now, fails when it matters" as a distinct hazard class.

### 3.3 Containment of a non-crash-safe vendor SDK

The following pattern appeared in a camera enumeration endpoint:

```python
try:
    for dev in rs.context().query_devices():
        ...
except Exception as e:
    return CameraDevices(error=str(e))
```

The accompanying documentation stated that enumeration failures are "reported as
`error`, not raised". On the platform in question this guard has no effect. When
librealsense cannot claim a device's USB interfaces, the bundled 2.56.5 build
**terminates on SIGSEGV** rather than raising a language-level exception, and no
`except` clause can intercept a signal. A segmentation fault is delivered to the
process, not raised; in a contribution about precisely the signal-versus-exception
distinction, the verb is not cosmetic.

The consequence is disproportionate. A single unavailable camera terminated the
control server, and the triggering request was the user interface asking which
cameras were attached.

We measured the fault directly. The full test suite exited 139 (SIGSEGV) and
reported no results. Notably, this is a distinct signature from a failing suite: a
crashed suite produces no failure list, which is easy to read past. The fault
occurred at the enumeration call site, confirmed by the interpreter's fatal-error
trace.

**Containment.** Enumeration and claim attempts are performed in a short-lived child
process which prints one JSON line or dies. A child terminated by signal is
reported as an error carrying the signal number. The cost is one process spawn,
negligible beside the USB round trips the call already performs.

Two properties of the implementation are load-bearing:

- **The claim probe must attempt the operation, not inspect state.** Starting a
  pipeline is the operation that faults, so viability is established by starting one
  in the child and stopping it.
- **Claim viability must not be cached, while enumeration may be.** Claimability
  changes the moment another process releases the device; a cached negative would
  keep a working camera offline for the remainder of a session.

A related result: a guard intended to prevent this crash was found to suppress the
remedy it recommended. The guard skipped the claim whenever the operating system
still listed the camera, which is correct for an unprivileged process. Under
elevation the exclusive lock is not final, so the guard caused the documented
workaround to report `exclusive_access` without attempting the operation that would
have succeeded. **A guard that suppresses its own recommended fix is worse than no
guard.** Privilege is now checked before the guard applies.

### 3.4 Fail-closed verification

We adopt two rules for verification agents.

**Absent evidence is not a pass.** An agent that cannot see returns `ok=False` with
the reason. This inverts the stub behaviour of Section 1.1, and it has a visible
consequence: a workflow gating on `result.ok` halts at its first step rather than
running to completion. We consider the halt correct and the prior green run
meaningless.

**An unimplemented checker cannot report success.** Where a demonstration
requires the chain to run before every checker exists, the escape hatch must satisfy
three properties: the pass is labelled as simulated in its human-readable detail and
its structured data; confidence remains zero so any confidence-weighted aggregation
gives it no weight; and the switch cannot affect an implemented agent, since a
mechanism able to force a pass on a real checker is a mechanism for fabricating
evidence.

**Unknown as a first-class verdict.** Our measurement layer returns one of
`CAP_ON`, `CAP_OFF` or `UNKNOWN`, and `ok` is true only for a confident `CAP_OFF`.
`UNKNOWN` is returned when too few pixels carry valid readings, or when the measured
change matches neither hypothesis. It is not an error and must not be coerced into
either affirmative verdict.

**Abstention versus a zero vote in sensor fusion.** Where multiple channels are
averaged, an inconclusive channel must abstain rather than contribute zero. A zero
from an unreadable region halves a two-channel average and allows a channel that saw
nothing to overrule channels that observed the workspace. A channel that produces a
confident negative observation, by contrast, should contribute zero, because that is
evidence. We regard the distinction between *abstention* and *negative evidence* as
essential in any fused verification scheme, and note it is easy to implement
incorrectly: our own first implementation discarded the abstaining channel's
diagnostic data, which is precisely the data required to distinguish "the region
drifted off the target" from "the target did not move".

### 3.5 Container closure by height delta

For screw-capped tubes we verify closure state geometrically. Removing a cap
uncovers a surface further from an overhead camera by approximately the cap height,
15 to 20 mm for the consumables considered.

We select this signal over appearance-based alternatives for three reasons. It is
invariant to illumination. It requires no fiducial attached to the cap, which is the
first thing lost in a wet or gloved workflow. And it measures the quantity of
interest directly rather than a correlate.

Three implementation rules follow from the error analysis in Section 5:

1. **The depth scale is read from the device, never assumed.** The measurement
   function accepts raw integer counts and a scale, and rejects a non-positive
   scale, so unit conversion occurs at one reviewable site.
2. **Statistics are robust, not mean-based.** Depth dropouts concentrate on the
   surfaces of interest, which are frequently transparent or specular, and a single
   zero-filled hole displaces a mean substantially. We use the median and
   interquartile range.
3. **A measurement over too few valid pixels is not a measurement.** Below a
   minimum valid fraction and a minimum absolute pixel count, the method returns
   `UNKNOWN`.

Confidence is reported as the margin of the measured delta over the measurement's
own spread, combining both observations' interquartile ranges, rather than as a
fixed number chosen for decisiveness.

**Comparison against a reference, not an absolute threshold.** No absolute "a cap is
15 mm" rule survives a change of consumable, standoff, or camera pose. A reference
measurement is captured with the container closed and the comparison is made against
it. Re-taking the reference is inexpensive and is what gives the number meaning.

### 3.6 Depth-free vision-guided positioning

Hand-eye calibration by rigid transform requires the observed feature's position in
metres in the camera frame, which requires depth and intrinsics. When the depth path
is unavailable, as it was in our deployment for the reasons in Section 4.2, this
route is closed and vision cannot command motion at all.

Depth is not required. The liquid-handler deck is planar, and a camera viewing a
plane induces a homography: eight parameters mapping image coordinates directly to
deck coordinates, recoverable from four or more correspondences, requiring no depth,
no intrinsics, and no knowledge of camera pose. For a fixed camera over a planar
workspace this is the correct model rather than a degraded substitute.

Correspondences are obtained by the same procedure used for the rigid fit: drive the
machine to known coordinates and observe the end-effector marker at each. Only the
units of the observation differ.

Because this component determines where a machine is commanded to move, its refusal
conditions are more important than its accuracy, and we state them explicitly.

**Four correspondences fit exactly, including four incorrect ones.** A homography has
eight degrees of freedom, so four point pairs determine it with no residual left to
expose a mistake. A clean residual on four points is not evidence. We require five
correspondences before a fit is considered trustworthy: the first count at which the
fit can be contradicted by its own data.

**Collinear correspondences cannot be detected by residual.** Points along a line
admit an exact fit along that line while leaving the mapping underdetermined across
the plane. This is checked directly, via the second singular value of the centred
point set, rather than inferred from fit quality.

**Points projecting to the plane's horizon have no finite deck coordinate.** The
mapping is checked for a vanishing homogeneous coordinate and raises, rather than
returning a large value that would be interpreted as a reachable target.

**Corrections are computed on the plane, not scaled from pixels.** A homography is
not a uniform scaling: an identical pixel offset corresponds to different physical
displacements depending on image location. Both points are mapped to deck
coordinates and differenced there. A single millimetres-per-pixel factor is
incorrect everywhere except the point at which it was measured.

The method returns in-plane coordinates only. Height must be supplied by the
machine's own axis, and a fit is valid only for the plane on which it was
established: a container mouth elevated above the deck projects differently from the
deck beneath it, and this model cannot distinguish them.

### 3.7 Closing the loop where no datum exists

The liquid handler in our deployment has no functional endstop on any axis. This was
established rather than assumed: the limit-switch state was polled for 18 seconds
while a switch was depressed by hand and no bit changed, and the firmware
configuration contains no axis limit entries. Homing therefore does not terminate on
a switch; it drives a fixed search distance and zeroes the counter.

Two consequences bear on verification. First, a stalled axis still has its steps
issued, the move completes on schedule, and the position counter continues to
advance, so the internal coordinate diverges from physical reality while every
software signal reports success. Re-observing the end-effector is the only mechanism
that detects this, which makes positioning and verification a single loop rather than
two subsystems. Second, an axis that cannot be homed at all can still be closed
visually, because the camera measures its position directly. Vision supplies the
missing datum.

---

## 4. Results

We separate results measured on hardware from those established in simulation. All
simulation results run with no camera or robot attached.

### 4.1 Measured on hardware

**Acquisition.** Colour at 1280x720 and depth were acquired from an Intel RealSense
D405. The depth scale reported by the device was 9.9999997 x 10^-5 m per count.

A frame set contained 734,469 pixels with **nonzero** depth, and a second returned
882,330. We report nonzero-pixel counts rather than valid-measurement counts, because
the extrema show the predicate was nonzero-ness and nothing stronger. The reported
maximum of 6.553 m is exactly 65535 counts at this device's depth scale: a **16-bit
saturation sentinel, not a range reading**, and far outside the D405's specified
0.07 to 0.50 m window. The reported minimum of 0.093 m is below the 0.100 m nominal
minimum for this resolution quoted in Section 4.4. A second acquisition's 4.016 m
maximum is likewise out of specification and is invalid-region noise.

This matters beyond bookkeeping. Section 3.5's refusal condition is a valid-pixel
*fraction*, and a fraction computed over a nonzero predicate can be satisfied
entirely by noise and saturation sentinels. The predicate must range-gate against the
device's specified window before the fraction means what the gate assumes it means.
It does not currently do so, and reporting a saturation sentinel as a depth span was
this paper reproducing its own Section 5 failure mode.

**Streaming.** An MJPEG transport delivered 21 JPEG frames in four seconds over
HTTP with correct multipart framing, with scene content changing between frames,
confirming a live feed rather than a repeated cached frame. A subsequent two-camera
configuration delivered 30 frames from each of two devices.

**The SDK fault.** Pipeline initialisation terminated on SIGSEGV (exit 139) on four
consecutive attempts, and returned a clean `RuntimeError` for the same call earlier in
the session. This establishes the fault as **condition-dependent rather than
intermittent**: four-for-four under a fixed condition is reproducibility with respect
to unmodelled state. We did not identify the state that selects between a signal and
an exception, and say so rather than calling it random. Each
occurrence raised an operating-system crash dialogue, making retry loops actively
harmful to the operator.

**Suite-level effect of the fault.** Before containment, the complete test suite
exited 139 and produced no result list. After containment, 173 tests passed. The
endpoint that previously terminated the server returned HTTP 200 with the message
`cannot claim camera: the SDK crashed attempting the claim (signal 11)`, and the
server continued to answer health checks.

**Exclusive access.** With the operating system's capture assistant holding the
device, 30 unprivileged eviction attempts across three rounds failed, each reporting
kernel driver active on all four interfaces. The eviction utility itself aborted on
a userspace USB library mutex assertion.

**Device identity.** A single physical D405 reported serial `352122272054` through
the SDK and `351623070085` through the USB descriptor. Separately, the device's USB
location identifier changed within one session, and the capture layer's unique
identifier changed with it.

### 4.2 Platform findings

The operating-system capture path was refused to the host process throughout. The
responsible process carried no camera entitlement, so access was denied without
generating a user prompt and the application never appeared in the operating
system's camera permission list. No configuration change can grant access in this
condition; the process must be launched under a different responsible application.

This has a direct methodological consequence. The capture grant follows the
responsible application of the process tree, not the user and not privilege
elevation. A server launched by a process without the entitlement cannot acquire
frames regardless of configuration, and we spent substantial effort attributing to
configuration what was a property of the launch context.

The vendor SDK path and the operating-system capture path fail independently and for
unrelated reasons. We recommend that any system on this platform probe both and
report them separately, which is why the taxonomy in Section 3.2 distinguishes
`permission_denied` from `exclusive_access`.

### 4.3 Established in simulation

**Height-delta verification.** 11 tests over synthetic depth frames at a known
standoff with a known step, using the published close-range noise figure. The
measurement recovers a 250 mm standoff to within 0.3 mm from a median over 3,600
pixels. A 15 mm step is detected with the expected margin. A 95 percent dropout rate
yields `UNKNOWN` rather than a verdict, while a 30 percent dropout rate still
measures to within 0.5 mm. One test pins the consequence of the incorrect depth
scale at exactly ten times.

**Fusion behaviour.** 6 tests, on a branch not merged at time of writing. An
inconclusive depth channel abstains rather than vetoing channels that observed the
workspace; a confident negative contributes zero. These tests were written against a
multi-channel fusion interface that was subsequently replaced upstream by a
world-model query interface, so they are not included in the totals below and the
design argument in Section 3.4 should be read as a position rather than as shipped
behaviour.

**Planar homography.** 11 tests. A known homography is recovered to 1 part in 10^6
and generalises to held-out points. A fit survives 0.5 mm labelling noise. A
perfect four-point fit is refused. A swapped correspondence is detected by residual.
Collinear input is refused before fitting. Horizon points raise.

**Rigid hand-eye fit.** 8 tests against a synthetic camera at a known pose recover
the transform to 1 part in 10^9, survive 0.5 mm detection noise, refuse collinear
geometry, and surface a tenfold scale error as untrustworthy. A further 5 tests
cover marker detection.

**Containment.** 7 tests covering a child terminated by signal, a hang, SDK
diagnostics emitted before the result line, and an assertion that the probe source
is never imported into the parent process.

Total: **184 tests passing with no camera and no robot attached.** The figure of 173
quoted in Section 4.1 is the same suite measured before the planar homography of
Section 3.6 was added, and is retained there because it is the number that
establishes the containment result.

### 4.4 Sensor selection for close-range verification

Minimum range differs sufficiently across the D400 family to determine feasibility
rather than merely quality. From the family datasheet:

| Resolution | D405 min-Z | D435 min-Z |
|---|---|---|
| 1280 x 720 | 100 mm | 280 mm |
| 848 x 480 | 70 mm | 195 mm |
| 640 x 360 | 55 mm | 150 mm |

At a 250 mm standoff, the D435 produces no depth at 1280x720 and must operate at
reduced resolution near its minimum range. The D405 remains a factor of 2.5 above
its minimum at full resolution.

**Close-range feasibility is determined by minimum range, not by noise, and an
earlier version of this section got the noise comparison backwards.** The datasheet
bounds root-mean-square spatial noise at 1 percent of range for the short-range
device, specified up to 0.5 m, and 2 percent for the general-purpose devices,
specified up to 2 m. Those two bounds are anchored to *different distances*, and
stereo depth error grows as the square of range, so neither may be rescaled linearly
to a 250 mm standoff and the two percentages are not directly comparable. Propagating
each bound to 250 mm through each device's own baseline and focal length gives
approximately 1.25 mm for the short-range device and 0.63 mm for the general-purpose
one: **the ranking inverts**, because the latter's baseline-focal product is roughly
2.6 times larger.

We record this because the error was instructive. A percentage was treated as
scale-invariant, the resulting figure overstated the competing device's noise by
roughly a factor of eight, and the overstatement was then used to justify the device
already selected. That is the confidently-wrong-value pattern of Section 5 occurring
inside this paper's own analysis, and it was caught by adversarial review of the
arithmetic rather than by any measurement.

The defensible statement is narrower. Propagating the short-range bound to 250 mm
gives a per-pixel standard deviation of at most approximately 1.25 mm, so a 15 mm step
is at least 8 standard deviations per pixel even after the factor-of-root-two penalty
incurred by measuring a difference against a reference. We quote no per-pixel figure
for the general-purpose devices at 1280x720, because at this standoff that
configuration returns no depth at all.

**We did not validate this comparison experimentally.** The short-range device was
removed from the deployment before the closure measurement was performed on a real
container, and was replaced by two general-purpose devices, which inverts the
margin above. This is stated as a limitation in Section 6, not as a result.

---

## 5. A catalogue of confidently-wrong-value hazards

We group separately a class of defects whose common property is that they produce
plausible incorrect values rather than detectable failures. Each was encountered in
practice.

**A depth scale that varies within a product family by a factor of ten.** The D405
reports 10^-4 m per count; other D400-series devices report 10^-3. Code written
against one and reused with the other produces distances wrong by ten times, in a
direction that remains physically plausible. Mitigation: read the scale from the
device, never default it, and prefer an absent value to a substituted one. Our
enumeration helper returns `None` rather than a default for this reason, and a test
enforces it.

**A device reporting two inconsistent serial numbers.** The SDK's serial and the USB
descriptor's serial differ for the same physical camera. Any component matching a
camera by serial must specify which source it means, or it will inspect a connected
camera and conclude it is the wrong one. This hazard is aggravated by advice, which
we ourselves issued before discovering it, to "match on serial" without qualification.

**Capture indices that renumber.** Device indices and capture-layer unique
identifiers both change when a device re-enumerates, which occurs on every
reconnection. Neither may be persisted. Enumeration orders additionally differ
between tools on the same host, so a device name obtained from one tool is not
evidence for an index used by another.

**Attribution of properties across devices.** Our own enumeration code located the
first matching property in a flat registry dump and applied it to every device of
that vendor. With one camera attached this is invisibly incorrect; with two it
reported both devices with the same serial. Properties are read from each device's own
registry block, and left absent when no block matches rather than borrowed from a
neighbour. This fix is committed on the preflight tool's branch and was not present on
the branch audited for this paper; the two branches should be reconciled before the
claim is repeated.

**Over-strict test assertions that pin a bug.** An assertion of the form
`assert devices or error` requires one of two fields to be non-empty. It passed only
because enumeration was crashing and populating the error field. When enumeration
was corrected and began reporting "no devices attached" honestly, the assertion
failed. Three outcomes are legitimate here, and the contract is that the call
answers rather than that it always has something to report.

**A stub that returns success.** Discussed in Section 1.1. We note it here because it
belongs to the same class: it produces a plausible value rather than a detectable
absence.

---

## 6. Limitations

We state these plainly, since a paper about not overclaiming is an unusually poor
place to overclaim.

**The closure measurement has never been run on a physical container.** The method,
its refusal conditions and its error analysis are validated against synthetic depth
at a known standoff. The thresholds in use derive from a datasheet and from
simulation, not from measurement on the deployed hardware. Until that measurement is
performed, the method is unimplemented in the sense that matters, not merely
untested.

**The sensor comparison in Section 4.4 is not experimentally validated,** and the
device it favours was removed from the deployment before validation was possible.
The thresholds require re-derivation for the general-purpose devices now installed,
where minimum range is 2.8 times larger and per-pixel noise approximately doubles.

**The metric depth path was unavailable in the final deployment configuration.** All
cameras were operated as generic capture devices, yielding no depth and no factory
intrinsics. Verification in that configuration is two-dimensional and
fiducial-based. We consider it important not to describe such a system as performing
depth verification.

**The hand-eye calibration procedure was never executed on hardware.** The fit, the
servo correction and their refusal conditions are validated in simulation. No paired
observations were collected from the physical machine.

**No manipulator motion was commanded.** The robotic arms were unreachable on the
deployment network throughout, and the liquid handler was driven only in relative
jogging. No full-envelope motion and no liquid handling was demonstrated.

**Single-site, single-platform.** The platform findings in Section 4.2 concern one
operating system and one SDK build. The architectural claims are intended to
generalise; the specific fault modes are not claimed to.

**Some measurements are single-trial.** The frame counts and pixel-validity figures
in Section 4.1 are representative observations, not distributions over repeated
trials.

---

## 7. Discussion

The results we consider most transferable are not the measurements but three
structural observations.

**A guard that cannot observe the failure it guards against is decorative.** The
`try`/`except` in Section 3.3 was written in good faith and documented accurately
with respect to its author's model of the failure. It was ineffective because the
failure was a signal rather than an exception. The general lesson is that the class
of failures a guard can intercept must be established, not assumed, and that
language-level error handling does not extend to process-level faults.

**A crashed test suite is not a failing test suite, and is easier to miss.** The
suite in Section 4.1 exited 139 with no failure list. Continuous integration and
human readers alike are calibrated to red output. An absence of output reads as a
tooling problem. We suggest that suites be gated on exit code and not solely on
reported failures.

**Refusal conditions deserve the attention normally given to accuracy.** In each
component described here, the interesting engineering is in the conditions under
which the component declines to answer: too few valid pixels, too few
correspondences, collinear geometry, a projection to the horizon, a movement matching
neither hypothesis. On a machine without endstops, a confidently wrong coordinate is
a collision that every software signal reports as success. The accuracy of the
common case is comparatively easy.

A recurring pattern is worth naming. In several instances the correct behaviour was
already documented in a comment or asserted in a test name, and the implementation
did not achieve it: the endpoint documented as reporting errors rather than raising,
the test asserting "never a crash" that could not survive one, the agent named for a
check it did not perform. The intent was present and unenforced. We take this as an
argument for making such properties executable, and for treating a test that cannot
fail in the situation it is named for as a defect in its own right.

---

## 8. Conclusion

We have argued that the primary hazard in verified laboratory autonomy is a
verification step that observes nothing and reports success, and that this hazard is
best addressed below the perception model, at acquisition. We described a preflight
gate, a failure taxonomy in which every condition carries one operator action, a
containment pattern for vendor code that terminates the process rather than raising,
a fail-closed policy for verification agents, and a depth-free planar method that
permits vision-guided positioning when metric depth is unavailable.

The system we describe verifies less than we initially set out to build. The closure
measurement awaits a physical trial, the metric depth path is unavailable in the
deployed configuration, and no calibration has been performed on hardware. We report
it in that state deliberately. The alternative was a system that appeared to verify
more, which is the failure this work is about.

---

## Reproducibility

All simulation results are reproducible with no hardware attached. The preflight
requires no virtual environment for its enumeration path. Hardware-dependent results
in Section 4.1 require an Intel RealSense D400-series device and are subject to the
platform constraints in Section 4.2, notably that the acquiring process must be
launched under a responsible application holding the operating system's camera
entitlement.

## Data and code availability

To be completed prior to submission.

## Author contributions

To be confirmed. Broadly: manipulation and end-effector design; perception and
verification; integration, liquid-handler control and orchestration.

## References

Formal citations to be completed prior to submission. The following sources are
referenced by name in the text and should be cited in full: the Intel RealSense
D400 series product family datasheet (document 337029-017); the librealsense SDK;
the OpenCV `objdetect` module for square-marker detection; the Kabsch algorithm for
optimal rigid rotation from paired point sets; and the direct linear transform with
coordinate normalisation for planar homography estimation.
