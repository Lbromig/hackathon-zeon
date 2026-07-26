# What to do now — step-by-step bring-up

The whole software loop (camera → detect → fuse → project → verify → recover) is built and
green on the mock (`backend/tests/test_integration_loop.py`). The remaining work is getting
it onto the real bench. Do these **in order** — each step has a command and a "done when"
check. Tags: **[run]** just run it · **[hw]** physical/hardware · **[code]** still needs code.

Owners (from PROJECT_PLAN): **Di** = cameras/perception, **Dale** = arms, **Lukas** = OT +
orchestrator. The critical path is **1 → 2 → 5 → 6**; steps 3, 4 can run in parallel.

---

## 0. Environment  [run]

```bash
uv sync                        # installs deps incl. opencv-contrib-python
cd frontend && npm install && cd ..
uv run pytest backend/tests -q # sanity: perception loop should be green
```
On macOS, `pyrealsense2` needs a local build: `bash scripts/build_pyrealsense2_macos.sh`.

**Done when:** `pytest` passes and `uv run uvicorn backend.app.main:app --reload` boots.

---

## 1. No-hardware dry run first  [run] · Di

Prove the full UI + overlay path before touching hardware, using the mock tag cameras.

```bash
HZ_FLEET_FILE=fleet.mock.json uv run uvicorn backend.app.main:app --reload
cd frontend && npm run dev          # http://localhost:5173
```
- Open the UI, open the camera tab. You should see three mock feeds with **green AprilTag
  boxes** (ids 180/183/224) and **projection outlines** for the tube/cap/nozzle.
- Trigger a calibrate run (the UI button, or `wscat -c ws://localhost:8000/ws/calibrate`)
  so `GET /api/worldmodel` returns entities.

**Done when:** overlays render and `curl localhost:8000/api/worldmodel` is populated. This
means every software piece works; everything below is just swapping mocks for real devices.

---

## 2. RealSense bring-up  [hw] · Di

```bash
sudo .venv/bin/python scripts/validate_camera.py --list          # SDK serials + .env block
```
- Copy the printed serials into `.env` as `CAM_GRIPPER=`, `CAM_OVERVIEW=`, `CAM_HANDOVER=`
  (use the **SDK** serial, not the USB-descriptor one — see the note in `.env.example`).
- Confirm all three stream together (USB bandwidth is the usual failure):
```bash
sudo .venv/bin/python scripts/validate_camera.py --all --frames 30
sudo .venv/bin/python scripts/validate_camera.py --fiducials      # tags detected live
```

**Done when:** all three stream at once and `--fiducials` reports tag detections. If macOS
blocks the SDK, set `CAM_<NAME>_TYPE=camera` to fall back to the UVC path per camera.

---

## 3. Arms + OT bring-up  [hw] · Dale / Lukas

```bash
uv run python scripts/init_xarm.py --ip 192.168.3.13     # left; repeat for .11 (right)
uv run python scripts/find_joint_limit.py --ip 192.168.3.11   # measure RIGHT J5 clearance
```
- Put the measured right-arm J5 limit into `core/config.py` (`LEFT_J5_LIMITS` has the left
  one; add the right — do **not** copy the left numbers, the tooling differs).
- OT: the serial driver `connect()/_send()` is still **[code] TODO** (a
  `feat/ot-one-serial-driver` branch exists on origin) — merge/finish it, then set
  `OT_SERIAL_PORT` and confirm it homes.

**Done when:** both arms home under soft limits, and the OT homes.

---

## 4. Print & place the tags  [hw] + [code] · Di  (task #20)

- Print **AprilTag tag36h11 @ 20 mm** stickers (stock ids 180–224; you already have them).
- Stick one on each entity and build a small **fixed world board** the two fixed cameras
  both see. Current `MARKER_MAP` (`core/calibration/markers.py`) assumes:
  `180→left_base, 181→right_base, 182→ot_base, 183→rack_1, 186→tipbox_1, 224→tube_1_cap`.
- **Measure** each marker-centre → entity-origin offset and replace the placeholder
  `identity()` offsets in `MARKER_MAP` with real values (`T_marker_to_entity`).

**Done when:** every tag in `MARKER_MAP` is physically placed and its offset is measured.

---

## 5. Run on real cameras + verify detections  [run] · Di

```bash
uv run uvicorn backend.app.main:app --reload    # real fleet (no HZ_FLEET_FILE)
cd frontend && npm run dev
```
- Open each feed; confirm tag boxes land on the real tags and CV circles on tubes/caps.

**Done when:** live tag ids match the physical tags and `/api/cameras/<id>/detections`
shows `apriltag` detections with a non-null `camera_xyz`.

---

## 6. Calibration — the critical path  [code] + [run] · Di  (task #14)

`core/calibration/pipeline.py` still has TODO steps. Implement them (mostly OpenCV):
1. **Intrinsics** — read RealSense factory intrinsics via `CameraDriver.intrinsics()` (no
   ChArUco needed), cache to `calib/intrinsics/`.
2. **Hand-eye** — `gripper_cam → right_tcp` with `cv2.calibrateHandEye` over 15–20 arm poses
   viewing the board; cache `calib/hand_eye.json`.
3. **Fixed-cam extrinsics** — `overview_cam`, `handover_cam` from the world board via
   `solvePnP`; write each `T_world_cam` into the twin (`set_world_pose`).
4. **World frame + arm bases + `ot_base`/rack** from their tags; publish the twin.

Then run it:
```bash
wscat -c ws://localhost:8000/ws/calibrate      # or the UI "Calibrate" button
curl localhost:8000/api/worldmodel             # should now hold world-correct poses
```

**Done when:** a tag seen by **both** fixed cameras localizes to the same world point within
~5 mm (the shared-frame cross-check), and projection outlines sit on the real objects.

---

## 7. Confirm the live loop is world-correct  [run] · Di / Lukas

- Move a tagged object by hand; the twin fusion (already running, 10 Hz) should move the
  entity and the overlay should track it.
- The four verifiers now read real geometry: `curl localhost:8000/api/worldmodel` +
  watch `/ws/state` verdicts flip as you stage/unstage the tube under the nozzle.

**Done when:** moving the real tube visibly updates the twin and the `tube_aligned` verdict.

---

## 8. Teach the choreography poses  [hw] · Dale  (relates to #18)

The workflow choreography (`workflows/uncap_aspirate.py`) references **taught** pose names
(`tube_hold`, `cap_grasp`, `cap_dropoff`, `present_ot`, …). Teach each via the UI teach panel
(free-drive → save pose). `preflight` lists any still missing.

**Done when:** `GET /api/workflow/plan` preflight reports no missing poses.

---

## 9. Run the demo loop with failure injection  [run] · all

```bash
wscat -c ws://localhost:8000/ws/workflow        # scripted chain, or /ws/agent for the agent
```
- Run uncap → transport → present → aspirate; watch verdicts pass at each gate.
- **Failure injection:** nudge the tube after "present" → `tube_aligned` fails → recovery
  → passes (this is the money-shot, already proven in the mock test).

**Done when:** the full chain runs, and an injected failure recovers instead of aspirating
in the wrong place.

---

## 10. Freeze & record  [run] · all

Feature-freeze, rehearse, and record a backup video at the best stable tier (T0 markers is
the guaranteed floor; learned perception is out of scope). Keep the mock fleet working as the
demo fallback.

---

### Summary of what still needs CODE (vs just running)
- **#14 calibration** steps in `core/calibration/pipeline.py` (step 6) — the one true blocker.
- **OT serial driver** `connect()/_send()` (step 3) — needed for a real aspirate.
- **Measured `MARKER_MAP` offsets** (step 4) — values, not logic.
- Everything else (detect, fuse, project, verify, recover, camera stream, overlay) is **done
  and tested** — it just needs the calibrated poses from step 6 to be world-correct.
