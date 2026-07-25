# Workflow — Cooperative Uncap → Aspirate

Each step names the **devices** involved and the **capability** required. Cameras feed
the verification agents that gate every step; a failed check retries the step, and after
`MAX_ATTEMPTS` the chain stops and asks for help.

```mermaid
flowchart TB
  classDef cap fill:#e7f0ff,stroke:#2e6bff,color:#0b1220;
  classDef dev fill:#eef7ee,stroke:#22c55e,color:#0b1220;
  classDef ver fill:#fff4e5,stroke:#f59e0b,color:#0b1220;

  Start([Sealed tube in nest]) --> S1

  subgraph S1[Step 1 · Uncap]
    C1[/capability: dual_arm_manipulation/]:::cap
    D1a[Left xArm — hold tube]:::dev
    D1b[Right xArm — turn cap]:::dev
  end
  S1 --> V1{Cap off?<br/>torque + vision}:::ver
  V1 -- retry --> S1
  V1 -- pass --> S2

  subgraph S2[Step 2 · Transport]
    C2[/capability: arm_transport/]:::cap
    D2[Right xArm — carry open tube]:::dev
  end
  S2 --> V2{Grasp secure?<br/>width + vision}:::ver
  V2 -- retry --> S2
  V2 -- pass --> S3

  subgraph S3[Step 3 · Present]
    C3[/capability: arm_present/]:::cap
    D3[Right xArm — hold at pipette pose]:::dev
  end
  S3 --> V3{Tube aligned?<br/>pose check}:::ver
  V3 -- retry --> S3
  V3 -- pass --> S4

  subgraph S4[Step 4 · Aspirate]
    C4[/capability: liquid_handling/]:::cap
    D4a[Opentrons — aspirate]:::dev
    D4b[Right xArm — hold steady]:::dev
  end
  S4 --> V4{Aspirated?<br/>level / volume}:::ver
  V4 -- retry --> S4
  V4 -- pass --> Done([Done])

  V1 -- max retries --> Help([Stop · request help]):::ver
  V2 -- max retries --> Help
  V3 -- max retries --> Help
  V4 -- max retries --> Help

  Cam[On-arm + external cameras]:::dev
  Cam -. evidence .-> V1
  Cam -. evidence .-> V2
  Cam -. evidence .-> V3
  Cam -. evidence .-> V4
```

## Capability → driver mapping (the orchestration middle layer)

The orchestrator (`backend/app/workflows/`) resolves each step's capability to concrete
drivers via the `DeviceManager`. Capabilities are decoupled from hardware, so drivers are
swappable/mockable.

```mermaid
flowchart LR
  classDef cap fill:#e7f0ff,stroke:#2e6bff,color:#0b1220;
  classDef orch fill:#f0e7ff,stroke:#7c3aed,color:#0b1220;
  classDef drv fill:#eef7ee,stroke:#22c55e,color:#0b1220;

  subgraph CAPS[Capabilities required by steps]
    K1[dual_arm_manipulation]:::cap
    K2[arm_transport / arm_present]:::cap
    K3[liquid_handling]:::cap
    K4[verification]:::cap
  end

  ORCH{{Orchestrator + DeviceManager<br/>capability → device_id → driver}}:::orch

  subgraph DRV[Drivers]
    A1[XArmDriver ×2<br/>ArmDriver]:::drv
    A2[OpentronsDriver<br/>LiquidHandlerDriver]:::drv
    A3[OpenCVCameraDriver<br/>CameraDriver]:::drv
  end

  K1 --> ORCH
  K2 --> ORCH
  K3 --> ORCH
  K4 --> ORCH
  ORCH --> A1
  ORCH --> A2
  ORCH --> A3
```
