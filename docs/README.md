# docs

Design notes and requirements for the Track C cell. Start with the architecture,
then the workflow.

| Document | What's in it |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The layered design (frontend → backend → core/drivers → devices) and dependency rules. |
| [WORKFLOW.md](WORKFLOW.md) | The hero workflow: cooperative uncap → transport → aspirate, step by step. |
| [DIGITAL_TWIN.md](DIGITAL_TWIN.md) | The live digital twin (scene graph of entities + poses) and how calibration builds it. |
| [WORLD_MODEL_REQUIREMENTS.md](WORLD_MODEL_REQUIREMENTS.md) | Requirements for the perception + world-model layer that keeps the twin true. |
| [CAPABILITY_pick_place.md](CAPABILITY_pick_place.md) | Requirements for the safe pick/place primitive + the upright (anti-spill) guard. |
| [AGENT_ORCHESTRATION.md](AGENT_ORCHESTRATION.md) | The orchestration plan: no hardcoded "next", agent-driven step selection + gated recovery. |
| [ZEON_INTEGRATION.md](ZEON_INTEGRATION.md) | How the core logic maps onto ZEON's design/simulate/run platform. |
