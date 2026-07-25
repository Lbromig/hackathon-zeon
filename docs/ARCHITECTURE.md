# Software architecture

Three layers with a strict dependency direction: **frontend → backend → drivers → devices**.
The backend depends only on driver *interfaces*; vendor SDKs stay behind the driver layer.

```mermaid
flowchart TB
  classDef fe fill:#e7f0ff,stroke:#2e6bff,color:#0b1220;
  classDef be fill:#f0e7ff,stroke:#7c3aed,color:#0b1220;
  classDef dr fill:#eef7ee,stroke:#22c55e,color:#0b1220;
  classDef hw fill:#fff4e5,stroke:#f59e0b,color:#0b1220;

  subgraph FE[Frontend · Vue 3 + Vite]
    UI[App.vue]:::fe
    CMP[composables<br/>useFleet / useWorkflow]:::fe
    CL[api/client.ts]:::fe
  end

  subgraph BE[Backend · Python / FastAPI]
    REST[REST · /api/instruments]:::be
    WS[WebSocket · /ws/state, /ws/workflow]:::be
    DM[DeviceManager]:::be
    WF[Workflow orchestrator<br/>capability → driver]:::be
    VER[Verification agents<br/>cap / grasp / pose / aspiration]:::be
  end

  subgraph DR[Drivers · abstraction]
    CAP[capabilities<br/>Arm / LiquidHandler / Camera]:::dr
    REG[registry]:::dr
    XA[xarm]:::dr
    OT[opentrons]:::dr
    CAMd[camera]:::dr
  end

  subgraph TP[third_party]
    SDK[xArm-Python-SDK]:::dr
  end

  subgraph HW[Physical devices]
    LARM[Left xArm]:::hw
    RARM[Right xArm]:::hw
    OTd[Opentrons OT-One]:::hw
    CAMS[Cameras]:::hw
  end

  UI --> CMP --> CL
  CL -->|HTTP + WS| REST
  CL -->|HTTP + WS| WS
  REST --> DM
  WS --> DM
  WS --> WF
  WF --> DM
  WF --> VER
  DM --> REG --> CAP
  CAP --> XA
  CAP --> OT
  CAP --> CAMd
  XA --> SDK
  XA --> LARM
  XA --> RARM
  OT --> OTd
  CAMd --> CAMS
  VER -. frames .-> CAMd
```

## Layer responsibilities

- **frontend/** — presentation only; talks to the backend over REST + websockets.
- **backend/** — orchestration (the workflow state machine), verification agents, and the
  API. The *middle layer* that maps required capabilities to concrete drivers.
- **drivers/** — capability interfaces + one driver per instrument, created by a registry.
  Swap real hardware for mocks by registering a different factory under the same type.
- **third_party/** — vendored vendor SDKs, referenced only by their driver.
