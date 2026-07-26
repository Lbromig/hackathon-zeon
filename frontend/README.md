# frontend — Vue 3 + Vite + Tailwind

Two tabs:

* **Fleet** — live device status (websocket), camera feeds, and the workflow runner
  that streams uncap→aspirate step / verify / retry events.
* **Teach** — hand-drive an arm: jog every cartesian axis and joint, absolute
  move-to, gripper, taught-pose library. Internal bring-up tool.
* **Cameras** — live MJPEG feeds with an SVG overlay highlighting detected
  AprilTags and the twin entity each one belongs to.

```
src/
  App.vue                     tab shell (fleet | teach)
  style.css                   Tailwind v4 entry + .btn/.card/.chip components
  api/client.ts               fleet REST + websocket helpers
  api/teach.ts                teach/jog REST client  (/api/arms)
  api/cameras.ts              camera list, MJPEG urls, detection types
  composables/useFleet.ts     live fleet state  (/ws/state)
  composables/useWorkflow.ts  run workflow + event stream (/ws/workflow)
  composables/useTeach.ts     selected arm, 2 Hz state poll, commands, log
  components/InstrumentPanel.vue  per-device card (status + camera feed)
  components/WorkflowRunner.vue   run button + streamed step timeline
  components/teach/
    TeachPanel.vue    tab root: arm picker, safety row, increments, shortcuts
    JogPad.vue        cartesian X/Y/Z + roll/pitch/yaw, one click = one increment
    JointJog.vue      J1..Jn with live angles and limit rails
    MoveTo.vue        absolute pose / joint targets (cartesian needs a 2nd click)
    GripperControl.vue  open/close, width slider for width-capable grippers
    PoseLibrary.vue   save / go to / delete taught points
    CommandLog.vue    every command, result and latency
  components/cameras/
    CameraTab.vue     all fleet cameras, plus the selected-detection readout
    CameraView.vue    MJPEG <img> + SVG polygon overlay, click to select
```

## Cameras tab

Three RealSense viewpoints — gripper (eye-in-hand), overview, handover — configured
from `.env` (`CAM_GRIPPER` / `CAM_OVERVIEW` / `CAM_HANDOVER` + `CAM_WIDTH` /
`CAM_HEIGHT` / `CAM_FPS`). *Scan for devices* lists attached units so each serial can
be pinned to a fleet id; enumeration order is not stable across replugs.

The video is an `<img>` pointed at `/api/cameras/{id}/stream` (MJPEG — no player,
no WebRTC). Detections ride `/ws/state` as normalized `[0,1]` polygons and are drawn
as SVG on top, so the overlay stays crisp at any size and a slow detector never
stalls the video. Green = tag mapped to a twin entity, amber = tag detected but not
in `core/calibration/markers.py`.

On RGB-D units each tag label shows **measured depth**; the selected-detection panel
also gives the camera-frame position in metres. A camera that is offline shows its
configured serial/format and a Connect button — it deliberately does *not* point an
`<img>` at the stream, because that endpoint opens the device and every browser
retry would re-probe absent hardware.

No hardware? Run the backend with the synthetic tag fleet — it renders real
tag36h11 markers that the detector genuinely detects:

```bash
HZ_FLEET_FILE=fleet.mock.json uv run uvicorn backend.app.main:app --reload
```

## Teach tab

Jog increments (0.1–50 mm, 0.1–15°), speed presets and the selected arm persist in
`localStorage`. Keyboard: `←→` X, `↑↓` Y, `PgUp/PgDn` Z, `Q/E` yaw, `[`/`]` gripper,
`1`–`4` step presets, `Esc` E-STOP.

Safety lives in the backend, not here — the UI mirrors the limits but never enforces
them (see `backend/app/api/teach.py`). Jogging is discrete only; there is no
press-and-hold continuous jog, which would need servo-streaming mode and a deadman.

## Run

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173  (proxies /api + /ws to :8000)
npm run dev:local    # same, but loopback only
```

`dev` is `vite --host`, so it binds every interface and the machine's LAN address serves
the UI — and, through the proxy, the whole unauthenticated API with it. `dev:local` is the
plain-`vite` variant for when no second machine needs it.

The proxy target is `BACKEND_URL` (default `http://127.0.0.1:8000`) — set it to run against
a backend on another host. Start the backend first (`backend/README.md`).

`npm run build` type-checks with `vue-tsc` and emits `dist/`; `npm run preview` serves it.
