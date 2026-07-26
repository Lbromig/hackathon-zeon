# Findings

Ten mechanisms by which the stack reported success without having observed it.
Each entry states the claim, how it was measured, and where the mitigation landed.
Repository paths refer to the team checkout (`hackathon-zeon`) unless noted.

Severity key: **silent** = no software signal differs between success and failure;
**loud-but-wrong** = a signal exists and misleads; **fragile** = correct until an
ordinary event invalidates it.

---

## 1. No endstop and no current sensing: a crash is invisible

**Severity: silent.**

The gantry controller (Smoothieboard v1.0.3) reports no endstop state on any axis.
`M119` was polled for 18 seconds while the Z limit switch was held closed by hand;
not one bit changed. Baseline and final reply were identical:

    min_x:0 min_y:0 min_z:0 min_a:0 min_b:0

The firmware configuration has no axis limit entries either (`gamma_min`,
`gamma_max_travel`, `gamma_homing_direction` all absent).

**Consequence.** `G28.2` does not home to a limit. It drives a fixed search
distance, gives up, and zeroes the counter. Evidence: homing Z twice consecutively
took 5.44 s then 6.56 s. A real second home starts already on the switch and
completes in a fraction of a second; both runs burned the full search.

When an axis reaches a hard stop the driver keeps issuing steps, the motor skips
them, the move completes in exactly the predicted duration, and the open-loop
counter reports the commanded value. An out-and-back loop closes to 0.00 on paper.
**Motion duration proves a leg ran; it never proves the path was clear.**

*Mitigation:* none possible in software. Documented as the constraint that shapes
every other design decision; the operator is the only detector until vision is one.

---

## 2. A halted board acknowledges and silently discards commands

**Severity: loud-but-wrong.**

A relative 15 mm Z jog reported completion in **1.00 s against 3.00 s expected**,
and `M119` replied `!!` rather than the usual `min_x:0 ...` line. The board was
latched in HALT. Smoothieware ignores every G-code in that state while still
answering.

Two compounding details:

- The jog utility spoke raw serial and never sent `M999`, unlike the main driver,
  which sends it unconditionally on connect for exactly this reason.
- **The position counter still advanced by the full 15 mm.** After a move that
  never happened, the software's model of the machine was off by the whole step.

*Verified fix.* Sending `M999` before jogging: the identical step then ran in
**3.02 s** with a normal `M119`. Landed in `scripts/jog_z.py`.

*Generalisation:* a responsiveness check does not prove a device will accept
commands. A halted board answers `version` normally.

---

## 3. Position counters re-zero unpredictably, so dead reckoning is unsound

**Severity: fragile. This is the finding that caught the authors.**

Within one session the counters were observed behaving **both ways** across a
reconnect:

- A commanded `Z=-15` **persisted** across closing and reopening the serial port.
- `X` read **300 before a replug and 0 after**, with the carriage physically
  unmoved.
- On a later occasion `X` read **0 when -70 was expected**, with no replug at all.

The driver's own docstring asserts that "the counter survives reconnection." That
is true sometimes, which is worse than being false.

**The reflexive failure.** The agent tracked absolute X by accumulating relative
moves across two of these re-zeroings, then compared the running total against a
recorded envelope. It concluded the gantry was at its mechanical limit and refused
a requested move as physically impossible, five or six times, with rising
confidence and explicit reasoning about grinding a stepper. The operator
overrode. The gantry travelled a further 50 mm in ten clean 5 mm steps, every one
completing on schedule.

Two errors compounded: the accumulated offset was invalid across the resets, and
it was being compared against out-and-back travel measurements that were never
machine coordinates in the first place.

*Mitigation:* never state a position derived by accumulating moves across a
reconnect; re-home first, or describe motion purely in relative terms. All motion
tooling built in this session is relative-only for this reason
(`scripts/raise_and_traverse.py`).

---

## 4. Sensor fusion averaged away the only channel that measured the thing

**Severity: silent.**

The cap-removal verifier fused up to three channels: `torque` (wrist effort),
`vision` (fiducial displacement), `depth` (height delta at the tube mouth), with
`PASS_THRESHOLD = 0.60`.

Averaging produces the exact failure: torque 0.9 + vision 0.9 + a **confident**
depth reading of CAP_ON at 0.0 gives precisely **0.600**, which clears the
threshold. The verifier reported the cap removed. Depth is the only channel that
measures the tube rather than a proxy for it, and it was outvoted by two proxies
that agreed with each other.

*Fix.* `_conflict()` runs **before** `_fuse()` and returns `ok=False`,
`confidence=0.0` when the extreme channels differ by at least
`DISAGREEMENT_SPREAD = 0.50`, recording `would_have_fused_to` in the result.
Threshold is tunable and explicitly marked unsourced.

*Generalisation:* averaging assumes channels measure the same thing and roughly
concur. When they do not, the mean describes neither and hides exactly the case
an operator needs to see.

---

## 5. A contradiction is not a failure, and retrying one makes things worse

**Severity: silent.**

Both orchestrators treated any non-pass as retryable, and checked the `ok` flag
*before* the contradiction record, so a contradiction arriving with `ok=True`
reported as `passed`.

The distinction that was missing:

- **failed** — the motion ran to `max_attempts` and verification never passed.
  Retrying was the right thing to have tried, because the premise of a retry is
  that the action simply did not take effect.
- **escalated** — two independent channels contradict, so the physical state is
  unknown. There is nothing to retry, because a retry assumes you know the state
  you are starting from. If depth says the cap is on while torque says it came
  off, either the cap has snapped or the tube is being crushed, and repeating the
  unscrew makes whichever it is worse.

*Fix.* A terminal `escalated` phase, checked ahead of both the pass and the retry
budget, in `uncap_aspirate.run` and `Engine._run_skill`.

---

## 6. A verification channel that was dead in production while appearing wired

**Severity: silent.**

The torque channel looks for the unscrewing peak in `Evidence.during`. Neither
orchestrator ever populated it. `peak` therefore fell back to the unloaded
pre-step reading, landed below `MIN_UNSCREW_TORQUE_NM`, and the channel reported
"wrist never loaded" **on every run regardless of what the arm did**.

The system had three verification channels on paper and, in production, at most
two, one of which was inactive for want of configuration (finding 7). Nothing
surfaced the difference.

*Fix.* A cooperative `Sampler` hook threaded through `_execute` and `Skill.run`.

---

## 7. A vision channel that could not distinguish the thing moving from the camera moving

**Severity: silent.**

Cap-marker displacement was measured in image space with no reference. A cap
coming off, the camera being nudged, and the bench being knocked all translate the
marker identically.

*Fix.* Common-mode rejection against an optional static `datum_marker_id`: the
datum's displacement is subtracted from the cap's. Readings taken without a datum
are now explicitly flagged as unreferenced in the detail string rather than
silently trusted.

---

## 8. Device enumeration reported "none attached" when four were attached and refused

**Severity: loud-but-wrong.**

`rs.context().query_devices()` returns an **empty list** rather than raising when
librealsense cannot claim a device. The API surfaced `devices: [], error: ""`.

Ground truth at that moment: four RealSense units attached over USB 3, with
`rs-enumerate-devices` logging `RS2_USB_STATUS_ACCESS` and
`failed to claim usb interface: 0` for every one, because macOS `UVCAssistant`
holds exclusive ownership of the UVC interfaces.

The rendered result — "no cameras" — sends an operator to check cabling for a
problem that is entirely about exclusive ownership.

*Fix.* The empty case cross-checks the OS device list, which requires no claim,
and distinguishes attached-but-refused from nothing-found. The nothing-found
wording deliberately stops short of asserting nothing is attached, because a unit
already owned by another librealsense process also disappears from that list.

---

## 9. Blocking I/O on the event loop takes down the whole instrument server

**Severity: loud-but-wrong.**

A WebRTC video track defines `async def recv(self)`, which reaches a synchronous
`ok, frame = self._cap.read()`. That runs directly on the asyncio event loop. When
a camera is connected but not delivering frames, the read blocks and freezes the
entire backend.

Measured signature: `/api/health`, which does nothing but return `{"ok": true}`,
swung between **0.11 s** and **hard timeouts beyond 12 s**, while the host pinged
clean at 11 ms and the separate frontend dev server answered in 37 ms. Turning the
camera on is what made the system unreachable.

*Mitigation (proposed, not landed):* move the frame grab off the loop
(`asyncio.to_thread`). A bare `time.sleep(1.0)` on the same path has the same
defect.

---

## 10. A taught position is meaningless without a datum that outlives it

**Severity: fragile.**

Teaching a point stores machine coordinates. On this class of machine those
coordinates are expressed against a zero the board chose at its last reset
(finding 3), so a point taught in one counter epoch names a different physical
location in the next, and nothing reports the change.

*Design response.* Teach points record the datum state they were captured under
(`homed`, plus the raw counters) and replay refuses on either half of a mismatch:
a point taught without a homed datum, or a session that has not been homed since.
Saving is always permitted, because an un-homed point still carries useful
relative geometry; only replay is gated. One axis (Y) can never be homed on this
unit at all, so it is recorded but never replayed.

This is the same discipline as finding 5: an unknown state escalates rather than
averaging out to a pass.

---

## 11. An error guard that could not intercept the error it documented

**Severity: loud-but-wrong.**

Distinct from finding 8, and more severe. Finding 8 concerns enumeration returning
an empty list; this concerns the same call **terminating the process**.

The device endpoint wrapped the SDK call and documented the contract explicitly:
enumeration failures are "reported as `error`, not raised".

    try:
        for dev in rs.context().query_devices():
            ...
    except Exception as e:
        return CameraDevices(error=str(e))

On this platform that guard has no effect. When librealsense cannot claim the USB
interfaces, the bundled 2.56.5 build **terminates on SIGSEGV**, and no `except` clause
can intercept a signal. (A segmentation fault is delivered to the process, not
raised. In a finding whose whole point is the signal-versus-exception distinction, the
verb matters.) Confirmed from the interpreter's fatal-error trace:
`backend/app/api/cameras.py:83 in devices`, `Fatal Python error: Segmentation fault`.

**Consequence.** One unavailable camera terminated the entire control server, and
the triggering request was the user interface asking which cameras were attached.
Opening the Cameras tab was sufficient. The same fault then recurred at a second
site, `pipe.start()` in the driver's `connect()`, so fixing enumeration alone left
the server killable.

The fault is **condition-dependent, not intermittent**. Four consecutive attempts
terminated on SIGSEGV; the same call returned a clean `RuntimeError` earlier in the
session. Four-for-four under a fixed condition is reproducibility with respect to
unmodelled state, not randomness, and we did not identify the state that selects
between a signal and an exception. That distinction matters for mitigation: a
condition-dependent fault can in principle be predicted and avoided, which is the
basis of finding 12's pre-check, whereas an intermittent one could only be contained.
Each occurrence also raised an OS crash dialogue, which makes retry loops actively
hostile to the operator.

*Fix.* Both call sites moved to a short-lived child process which prints one JSON
line or dies; a child terminated by signal becomes an error carrying the signal
number. Verified against the running server: the endpoint returns HTTP 200 with
`cannot claim camera: the SDK crashed attempting the claim (signal 11)` and
`/api/health` still answers afterwards.

Two implementation details are load-bearing. The probe must **attempt** the
operation rather than inspect state, because starting a pipeline is what faults. And
claim viability must **not** be cached, unlike enumeration, because claimability
changes the moment another process releases the device and a cached negative would
keep a working camera offline for the session.

*Generalisation:* the class of failures a guard can intercept must be established,
not assumed. Language-level error handling does not extend to process-level faults,
and a guard written against the wrong failure class is indistinguishable from no
guard until the day it matters.

---

## 12. A guard that suppressed the fix it was printing

**Severity: loud-but-wrong.**

The guard added for finding 11 skipped the SDK claim whenever the OS still listed
the camera, on the reasoning that the exclusive lock is final. That is correct for an
unprivileged process.

Under elevation it is false: root satisfies the authorisation that
`USBInterfaceOpenSeize` requires and takes the interface rather than racing for it.
So running under `sudo` — the workaround the tool's own error message recommended —
reported `exclusive_access: skipped the SDK claim without attempting it`, on a run
where the alternative capture path simultaneously reported delivering a 1280x720
frame.

*Fix.* Privilege is checked before the guard applies.

*Generalisation:* a diagnostic that recommends an action must not contain a guard
that prevents that action. Removing the guard naively, however, reintroduced finding
11's crash: the honest resolution was to try the path that **cannot** crash first
and treat the vendor SDK as an upgrade attempted only when a child process has
confirmed it will succeed.

---

## 13. A depth scale that differs tenfold inside one product family

**Severity: silent.**

The D405 reports 10^-4 m per depth count. Every other D400-series unit in the lab
reports 10^-3. Measured on the attached D405: `9.9999997e-05`.

Code written against one and reused with the other produces distances wrong by
exactly ten times, in a direction that stays physically plausible: a tube 25 cm away
reads as 2.5 m, which is a number, not an obvious fault. Nothing in the frame, the
API, or the type system distinguishes the two cases.

This finding cost the session twice. Cap-height thresholds were derived against the
D405 and are wrong for the D435-class units that replaced it, and a comparison
favouring the short-range device was written before that substitution.

*Mitigation.* Read the scale from the device on every connect, never default it, and
prefer an absent value to a substituted one: the enumeration helper returns `None`
rather than a plausible constant, with a test enforcing it. The measurement function
takes raw integer counts plus a scale and rejects a non-positive scale, so conversion
occurs at one reviewable site rather than at each call.

*Generalisation:* a unit error inside a product family is more dangerous than one
across vendors, because the interchangeability of the parts implies an
interchangeability of the code.

---

## 14. One camera, two serial numbers

**Severity: silent.**

With one camera attached and no other D400-series device on the bus, a D405 reported
serial `352122272054` through the SDK and `351623070085` through the USB descriptor.
Both are that camera's serial. Neither layer indicates that another exists.

One confound has to be excluded explicitly, because this session also produced a
defect that fabricates exactly this signature: the property-attribution bug below
applied one device's registry property to every device of that vendor. It cannot
explain this observation. The two values come from different sources — the SDK value
from `pyrealsense2` directly, the descriptor value from the IOKit registry — and with
a single RealSense present the attribution bug has only one block to read and is
therefore inert. Only the descriptor value, `351623070085`, appears in a committed
artifact; the SDK value is recorded from the bench session and should be reproduced
before publication.

Any component matching a camera by serial must state which source it means, or it
will inspect a connected camera and conclude it is the wrong one. The hazard is
aggravated by exactly the advice this session issued before discovering it: to match
on serial rather than on an index, which is correct, and which is dangerous when
stated without qualification.

Two related identity failures were also measured. The device's USB location
identifier changed **within one session** and the capture layer's unique identifier
changed with it, so neither may be persisted. And enumeration order differs between
tools on the same host, so a device name obtained from one tool is not evidence for
an index used by another.

*Mitigation.* Match on the model identifier plus the source-qualified serial.
Persist neither index nor unique identifier.

---

## 15. A test that could not fail in the situation it was named for

**Severity: silent. This one is about the tests, not the system.**

Two instances, both in verification of the above.

The endpoint test asserted the right property in a comment — `# one or the other,
never a crash` — and could not enforce the second half, because a SIGSEGV terminates
the interpreter before any assertion executes. Running the suite exited **139 and
produced no failure list at all**. A crashed suite is not a failing suite, and it is
easier to miss: readers and CI are calibrated to red output, whereas an absence of
output reads as a tooling problem.

The second instance is the mirror image. A test named
`test_enumeration_never_raises_with_no_camera` asserted `devices or error`, requiring
one of the two to be non-empty. It passed only **because** enumeration was crashing
and populating the error field. When enumeration was fixed and began correctly
reporting "nothing attached", the assertion failed — in precisely the condition the
test was named for. The assertion had been pinning the bug.

Three outcomes are legitimate here: devices found, an error explaining why not, and
an empty list with no error when nothing is plugged in. The contract is that the call
answers rather than that it always has something to report.

*Mitigation.* Gate suites on exit code, not only on reported failures. And treat a
test that cannot fail in its own named scenario as a defect in its own right.

*Generalisation:* a recurring pattern across findings 6, 11 and 15 is that the
correct behaviour was already written down — in a docstring, a comment, or a test
name — and was not enforced. The intent was present and inert. This argues for making
such properties executable rather than documentary.

---

## 16. The inverse case: a blocker tracked as open for six cycles was already closed on another branch

**Severity: loud-but-wrong, applied to project state rather than machine state.**

Every other finding here concerns a system reporting success it had not earned. This
one is the mirror image, and it cost more than any of them.

The liquid-handler serial transport was recorded as the project's single deciding
blocker across six consecutive review cycles: *"#1 THREAT, the LONE room-only
deciding item"*, with the assessment that `connect()` stores `object()`, `_send()`
returns `None`, the aspiration is mimed, and *"`aspiration_ok` would pass a green
check over nothing."* That assessment was accurate for the branch it was made
against.

Measured across branches:

| Branch | Driver | pyserial | TODO / stub |
|---|---|---|---|
| `agent-loop-p0` (demo branch) | 74 lines | no | 3 |
| `feat/ot-one-serial-driver` | 377 lines | yes | 0 |
| `initial-setup-and-repo-structure` | **641 lines** | yes | **0** |

On `agent-loop-p0`, `connect()` contains `# TODO: open the real transport` and
`self._conn = object()`. On `initial-setup-and-repo-structure` the same driver has 24
methods, opens a real `serial.Serial`, and implements `_write`, `_send`,
`_move_plunger`, `aspirate`, `dispense`, `home`, `jog`, `machine_position` and
`move_to_machine`. It also contains the halt-clear command from finding 2.

Neither branch has `feat/ot-one-serial-driver` merged. The integration branch's
implementation is therefore independent of, and substantially larger than, the branch
the team planned to cherry-pick.

**Consequence.** Six review cycles allocated the top-priority slot to writing a
component that already existed, on a branch already in the repository, and the review
process re-confirmed the blocker each cycle by re-reading the same branch. The
verification discipline applied to the machine — never trust a signal you have not
checked against the physical article — was not applied to the repository.

*Generalisation:* a status assertion is scoped to what was inspected, and a branch is
part of that scope. "Re-verified in HEAD" is a claim about one ref, not about the
project. Where work proceeds on several branches, a blocker should be checked against
every branch before it is carried, and the check is one command.

This finding also weakens a claim the team was relying on. `aspiration_ok` passing a
green check over a mimed motion is real on the demo branch, and it is the paper's
thesis in miniature — but it is a property of a branch selection, not of the
implemented system, and should be reported that way.

---

## Cross-cutting observation

Findings 1, 4, 5, 6, 7, 10, 13, 14 and 15 are **silent**: no software signal
distinguishes success from failure. Findings 2, 8, 9, 11 and 12 are
**loud-but-wrong**: a signal exists and actively misleads. Only finding 3 was caught
by an operator contradicting the system, and only because a human was watching a
physical object.

The count matters more than any individual bug: nine of fifteen defects were
invisible to every form of testing that does not involve the physical world, and they
were found in one session on hardware that was, by every dashboard available,
working.

Findings 11 through 15 additionally form a class worth naming separately. Each
produces a **plausible wrong value** rather than a detectable absence: a guard that
appears to guard, a diagnostic that suppresses its own remedy, a distance wrong by a
round factor, an identity that resolves differently per layer, a test that passes for
the wrong reason. Their common property is that no amount of reading the output
reveals them, because the output looks exactly as it should.
