# frontend — Vue 3 + Vite

Control UI: live fleet status (websocket), camera feeds, and the workflow runner
that streams uncap→aspirate step / verify / retry events.

```
src/
  App.vue                     layout: fleet grid + workflow runner
  api/client.ts               REST + websocket helpers, typed models
  composables/useFleet.ts     live fleet state  (/ws/state)
  composables/useWorkflow.ts  run workflow + event stream (/ws/workflow)
  components/InstrumentPanel.vue  per-device card (status + camera feed)
  components/WorkflowRunner.vue   run button + streamed step timeline
```

## Run

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173  (proxies /api + /ws to :8000)
```

Start the backend first (`backend/README.md`).
