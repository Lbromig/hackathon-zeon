# Vision reference fixtures

Three real frames, copied out of the gitignored `temp/captures/` so they cannot be lost. They are the
ground truth the v2 tip/tube detectors are written against — see
[docs/v2/IMPLEMENTATION_PLAN.md](../../../../docs/v2/IMPLEMENTATION_PLAN.md) D30 and
[docs/v2/GAP_ANALYSIS.md §3.1](../../../../docs/v2/GAP_ANALYSIS.md).

Measured 2026-07-26 with `core.perception.fiducials.FiducialDetector()` and its bench-tuned
parameters — no upscaling, no CLAHE, unless noted.

| file | origin | detected (tag36h11) |
|---|---|---|
| `handover_cam_handover_1280x720.png` | `temp/captures/handover_cam/20260726T071143_135Z_color.png` | **225 @ (619, 211)** on the tube/gripper assembly; 189 @ (535, 353), 219 @ (316, 424), 180 @ (396, 526), 181 @ (471, 526) on the deck |
| `right_gripper_cam_handover_1280x720.png` | `temp/captures/gripper_left_cam/20260726T071850_224Z_color.png` | 218 @ (1001, 649) — a **table** marker. The tube's own tag (~(672, 540)) is **not** detected: it is wrapped on the cylinder and curvature defeats the square-quad fit |
| `right_gripper_cam_handover_640x480_undetectable.png` | `temp/captures/gripper_left_cam/20260726T065209_454Z_color.png` | **none** at native resolution; **218** on a 2× upscale. Kept as the negative fixture proving the ≥1280×720 requirement (P-1) |

## Things these fixtures encode

1. **Both required viewpoints see the handover in colour**, with a detectable fiducial on the tube
   assembly. The servo loop is feasible.
2. **Slot names do not identify cameras.** Both gripper frames come from the slot named
   `gripper_left_cam` but are physically the **right** arm's camera. Never key calibration on a slot
   name — see R-VIS-10.
3. **Resolution is load-bearing.** The same scene is undetectable at 640×480 and detectable at
   1280×720. A silent resolution change is a silent servo gain change.
4. **Fiducials must be flat.** The flat table tag detects; the tag wrapped around the tube does not,
   at ~60 px and clearly legible to the eye.
5. **The classical fallback has a favourable image** in the gripper view: the pipette tip is a bright
   vertical shaft against a dark background and the tube opening is a bright ellipse beneath it.

## Reproducing the measurements

```python
import cv2
from core.perception.fiducials import FiducialDetector
det = FiducialDetector()
img = cv2.imread("backend/tests/fixtures/vision/handover_cam_handover_1280x720.png")
for d in det.detect(img):
    print(d.marker_id, d.center)
```
