# Code artifacts: where every claim can be checked

Repository `Lbromig/hackathon-zeon`. Branch heads as of 2026-07-26.

The paper asserts that certain defects existed and certain mitigations landed. This
maps each to a commit so a reviewer, or a co-author, can check rather than take it on
trust. Nothing here is a claim about hardware.

## Branch heads

| Branch | Head | Role |
|---|---|---|
| `initial-setup-and-repo-structure` | `937835e` | Integration branch. Most complete tree |
| `agent-loop-p0` | `f13a00a` | Demo branch the project plan tracks |
| `fix-cameras-api-segfault` | `2220675` | Crash containment + depth-free homography (PR #7, open) |
| `feat-cap-depth-channel` | `a96b760` | Depth as a fusion channel (PR #5, open, conflicting) |

## Pull requests

| PR | State | Contains |
|---|---|---|
| #8 | **open** | The visual servo loop: the production caller that turns a detection into a move. Branches from `agent-loop-p0` |
| #7 | **open** | Findings 11, 12, 15 mitigations; the planar homography |
| #5 | **open, conflicting** | The depth fusion channel of `SUPPLEMENT` Section 3.4. Written against a fusion interface since replaced upstream by a world-model query interface. **Do not cite as shipped behaviour** |
| #4 | merged | Camera preflight, the failure taxonomy, MJPEG transport, snapshots |
| #6 | open | Control dashboard, another author |
| #3 | closed | Superseded by #4 |

## Claim-to-commit map

| Paper claim | Where to check |
|---|---|
| Finding 11: enumeration terminates the server | Before: `agent-loop-p0:backend/app/api/cameras.py`, the `try`/`except` around `rs.context().query_devices()`. After: `59023bc` |
| Finding 11: second crash site at `connect()` | `8ce8499`, guard in `drivers/camera/realsense.py` |
| Finding 11 containment | `core/perception/rs_devices.py` on `fix-cameras-api-segfault` |
| Finding 15: assertion pinning the bug | `2220675`, diff of `backend/tests/test_rs_devices.py` and `test_cameras_api.py` |
| Finding 13: depth scale not defaulted | `rs_devices.py`, `depth_scale` left `None`; test in `test_rs_devices.py` |
| Finding 14: property attribution across devices | Fix is on `feat-cap-depth-channel` in `camera_probe.py`, **not** on `fix-cameras-api-segfault`. This is `CORRECTIONS.md` item 5 |
| Supplement 3.2: failure taxonomy | `camera_probe.py`, `Diagnosis` enum and `REMEDY` table |
| Supplement 3.5: height-delta measurement | `core/verification/depth_height.py`; tests in `backend/tests/test_depth_height.py` |
| Supplement 3.6: planar homography | `core/calibration/deck_homography.py` (`b4b635c`); tests in `test_deck_homography.py` |
| Rigid hand-eye fit | `core/calibration/ot_hand_eye.py`, another author |
| Vision-to-motion loop | `core/motion/visual_servo.py` (PR #8); tests in `test_visual_servo.py` |

## Finding 16: verify this one yourself before publishing

It is the folder's most consequential claim and the cheapest to check:

```bash
for b in agent-loop-p0 initial-setup-and-repo-structure feat/ot-one-serial-driver; do
  echo "$b: $(git show origin/$b:drivers/opentrons/driver.py | wc -l) lines, \
pyserial=$(git show origin/$b:drivers/opentrons/driver.py | grep -c 'import serial')"
done
```

Observed: `agent-loop-p0` 74 lines with no pyserial and three TODO or stub markers;
`initial-setup-and-repo-structure` 641 lines with a real `serial.Serial`;
`feat/ot-one-serial-driver` 377 lines. Neither of the first two has the third merged.

Finding 16 is written up in `FINDINGS.md` with the branch table above.
`HANDOFF.md` previously described it as a candidate carried in
`CONTEXT-project-plan.md`; that status line was corrected on 2026-07-26.

## Test counts

184 passing on `fix-cameras-api-segfault` with no camera and no robot attached.
Per-file, to spare anyone re-deriving them:

| File | Tests |
|---|---|
| `test_deck_homography.py` | 11 |
| `test_depth_height.py` | 11 |
| `test_ot_hand_eye.py` | 8 |
| `test_rs_devices.py` | 7 |
| `test_fiducials.py` | 5 |

`CORRECTIONS.md` item 35 notes that 136 of the 184 are unaccounted for in the
supplement. The table above does not close that gap; it narrows it.

`test_cap_depth_channel.py` (6 tests) exists only on `feat-cap-depth-channel` and is
excluded from the 184.

## Reproducing the acquisition results

The hardware figures in Section 4.1 of the supplement need an Intel RealSense
D400-series device and, on macOS, a launching process holding the camera entitlement.
A process launched from a terminal has it; one launched from an editor or agent
subprocess may not, and will be refused without a prompt appearing. This is a
property of the launch context, not of configuration, and it consumed a substantial
part of the session before being identified.

Versions are **not currently recorded** anywhere in the folder beyond the librealsense
build (2.56.5). `CORRECTIONS.md` item 20 asks for macOS, Python and camera firmware
versions in Reproducibility. They should be captured from the bench before the
machine state changes.


## Late addition: the vision-to-motion gap is closed in software

PR #8, opened after `HANDOFF.md` was written, adds `core/motion/visual_servo.py`: the
production caller that `deck_homography` and `ot_hand_eye` lacked. Before it, neither
was imported by anything but its own tests, so a detection could not become a move.

240 tests pass on that branch with no camera and no robot. It also turned the
`agent-loop-p0` suite green: `test_device_discovery_never_500s` was failing there for
the reason finding 15 describes, which is finding 15 observed live rather than
historically.

**This does not mean vision-guided motion works.** It is software-complete and
hardware-blocked. The remaining blocker is physical and singular: `MARKER_MAP` assigns
every tag to static furniture, so **nothing rides the gantry** for the loop to
observe. Any claim in the paper about vision commanding motion must be scoped to
simulation until a marker is on the carriage and the loop has run.

One correction the paper should carry, because this project asserted the opposite
repeatedly: **a datum was never a prerequisite.** Every correction in the loop is
relative. The guidance that pushed toward homing the gantry, an operation that on this
unit grinds a stepper against a hard stop, conflated returning to a remembered place
with closing a visual error. Only the first needs an origin.
