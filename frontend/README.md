# frontend — Vue 3 + Vite + Tailwind

Two tabs:

* **Fleet** — live device status (websocket), camera feeds, and the workflow runner
  that streams uncap→aspirate step / verify / retry events.
* **Teach** — hand-drive an arm: jog every cartesian axis and joint, absolute
  move-to, gripper, taught-pose library. Internal bring-up tool.

```
src/
  App.vue                     tab shell (fleet | teach)
  style.css                   Tailwind v4 entry + .btn/.card/.chip components
  api/client.ts               fleet REST + websocket helpers
  api/teach.ts                teach/jog REST client  (/api/arms)
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
```

Start the backend first (`backend/README.md`).
