# Agent handoff — hackathon-zeon

A self-contained brief for an autonomous agent with **full access** (repo + git push +
the machine on the arm's network, optionally an `ANTHROPIC_API_KEY`). Read this top to
bottom before acting. It states the current state, the hard conventions, how to land the
work-in-progress branches, and the prioritized tasks to finish the demo.

Mission: **Track C — cooperative uncap → aspirate, with a digital twin + AI verification.**
Two UFACTORY **xArm Lite 6** arms cooperatively uncap a sealed tube; one arm holds the open
tube while an **Opentrons OT-One** aspirates; **cameras + AI agents verify each step** and
recover on failure. Repo layout: `core/` (framework-agnostic domain), `backend/` (FastAPI),
`drivers/` (instrument abstraction), `frontend/` (Vue 3 + Vite), `third_party/` (vendored xArm SDK).

---

## 1. Hard conventions (do not violate)

- **uv for all Python.** `uv sync` to install; run everything with `uv run …`. Never bare
  `pip`/`python` in docs or scripts. Python 3.11–3.13.
- **Import root = repo root.** Backend runs from repo root: `uv run uvicorn backend.app.main:app --reload`.
  Tests use `pytest` with `pythonpath = ["."]`. Scripts self-bootstrap the repo root.
- **Layer boundaries:**
  - `core/` must NOT import FastAPI or `backend.*` (it's reused by ZEON skills + scripts).
  - `drivers/` is the only place a vendor SDK (xArm, Opentrons, OpenCV) may be imported.
  - `backend/` depends on `core/` + `drivers/` interfaces only.
- **Safety spine is authoritative.** The deterministic guards — anti-spill upright guard
  (`core/motion/pick_place.py::assert_upright`), waypoint discipline (safe height → approach-above
  → vertical final approach), soft joint/speed limits, collision detection, e-stop — are always
  enforced. No agent/LLM path may bypass them.
- **Git hygiene:** commit with **targeted `git add <path>`**, never `git add -A`, because the tree
  usually has unrelated in-flight work. Secrets live in `.env` (gitignored); `.env.example`
  documents the variable names the code actually reads.
- **Verify before you commit:** `uv run pytest` green + an import smoke + (if hardware present)
  a dry-run.

---

## 2. Current git state (as of handoff)

Branches (nothing pushed — the previous environment had no GitHub credentials):

- `main` — base.
- `x-arm-integration` — xArm SDK vendored + `scripts/init_xarm.py` + uv (`pyproject.toml`) + docs.
  Contains WIP snapshot commit `d966144`.
- `structure-refactor` (`aed62fe`, based on `d966144`) — **repo review fixes**:
  untracked `.claude/settings.local.json` + `TrackC-Pitch.pptx` (gitignored `*.pptx`), tracked
  `uv.lock`; env-driven `core/config.py` + aligned `.env.example`; extracted top-level **`core/`**
  package (`worldmodel`, `motion`, `calibration`, `verification`, `config`); unified import root;
  `drivers/mock/` + **11 passing tests**; `justfile`; `docs/README.md` index.
- `agent-loop-p0` (`32d229e`, **stacked on** `structure-refactor`) — **AI-agent orchestration P0**:
  `backend/app/agent/` (`tools.py`, `policy.py`, `engine.py`) + `WS /ws/agent` + tests
  (**17 passing total**). Offline `RuleBasedPolicy` default; `ClaudePolicy` gated behind
  `ANTHROPIC_API_KEY`. The one-line `main.py` router wiring was left **uncommitted** on purpose.
- `initial-setup-and-repo-structure` — earlier setup branch (superseded).

**Working tree** also holds the human team's uncommitted in-flight work (teach UI + `useTeach`,
world-model CAD meshes `core/worldmodel/meshes.py`, schema/driver edits, the `main.py` `/ws/agent`
wiring). Preserve it — review and commit it as its own logical commits; do not fold it into the
refactor/agent commits.

### Land it (agent with push access)
1. Commit the in-flight work in logical chunks (teach UI, meshes, schema/driver edits, the
   `main.py` `/ws/agent` include line) with targeted `git add`.
2. Land order: merge/PR **`structure-refactor` first**, then **`agent-loop-p0`** (it fast-forwards
   on top). Rebase `x-arm-integration`'s unique commits if still needed.
3. `git push` each branch / open PRs. Re-run `uv run pytest` after merge.
4. Set the **real second-arm IP** (currently `192.168.3.14` placeholder in `core/config.py` /
   `.env.example`).

---

## 3. How to run / verify

```bash
uv sync                                              # env + deps (incl. vendored xArm SDK)
uv run pytest                                        # expect all green (17+)
uv run uvicorn backend.app.main:app --reload         # from repo ROOT; boots w/o hardware
cd frontend && npm install && npm run dev            # UI on :5173, proxies /api + /ws

# arm bring-up (on the arm's network; release E-stop first)
uv run python scripts/init_xarm.py --ip 192.168.3.13            # add --home to fold in
uv run python scripts/init_xarm.py --fleet --dry-run            # both arms, no hardware
```
Import smoke: `PYTHONPATH=. uv run python -c "import backend.app.main, core.worldmodel, core.motion, backend.app.agent.engine, drivers"`.
Agent loop offline: connect a WS client to `/ws/agent` (RuleBasedPolicy) and watch the event stream.

---

## 4. Priority tasks to finish the demo (do in this order)

These are the real gaps; the scaffolding is done. Each is scoped to be independently shippable.

**T1 — Make ONE verifier real (highest priority).**
Every agent in `core/verification/agents.py` returns `ok=True, confidence=0.0, detail="stub"`, so
the verify→retry loop is currently theater — the "physical verification" half of Track C. Implement
`cap_removed` first: fuse the turning arm's **gripper-torque / force drop** with a simple
**vision/marker** cue (threads visible or cap moved). Return a real `confidence`. Then wire the
same pattern for `grasp_secure` (gripper width band + object present) and `aspiration_ok`
(OT volume report / coarse liquid-level before/after). Keep them as pure twin/telemetry queries.

**T2 — Wire the hero workflow to actually move.**
`backend/app/workflows/uncap_aspirate.py::_execute` has every driver call commented out (TODO), so
nothing runs end-to-end. Implement at least the **FLOOR path** (snap-cap uncap + place tube in OT
nest + aspirate) using `core/motion` safe pick/place + teachpoints. This also makes the P0 agent
loop drive real hardware instead of no-ops.

**T3 — Concurrency + thread safety on hardware calls.**
Blocking SDK calls must never run on the event loop. `/ws/state` already uses a threadpool + cache
and `/ws/agent` uses `asyncio.to_thread`; audit `/ws/workflow` and `/ws/calibrate` the same way.
Add a **per-driver lock** in `DeviceManager` so the state poller, workflow, teach jog, and camera
frame endpoint can't issue concurrent SDK commands to the same arm.

**T4 — Populate the twin (calibration).**
`core/calibration/pipeline.py` steps are TODO stubs, so `services/twin.get_world()` is `None` until
calibration runs. Implement the reliable path first: **hand-taught reference poses** to solve
`arm_base → world` and `arm ↔ OT` (see teachpoints binding below); defer vision (Grounded-SAM 2 /
FoundationPose / Kaolin) — those deps aren't installed and are non-commercial-licensed
(see `docs/WORLD_MODEL_REQUIREMENTS.md` §NFR-LICENSE-1).

**T5 — Teachpoints ↔ world model binding.**
Persist each teachpoint (from `backend/app/api/teach.py` / `teach_poses.json`) with optional
`entity_id` + `anchor_name`. On calibrate, attach it as that entity's **anchor** in the twin
(convert arm-frame mm/deg → world-frame m via the `arm_base→world` transform). Give callers two
ways to reach a target: **replay teachpoint** (joint-space, repeatable) or **compute from twin**
(entity pose + anchor offset, vision-adaptive). Verification compares live twin pose vs the
expected anchor within tolerance.

**T6 — Bring-up the other devices.** Verify the OT-One driver's one real action (home) and camera
streaming; these exist but aren't confirmed on hardware.

---

## 5. Agent-orchestration roadmap (after T1–T2)

Follow `docs/AGENT_ORCHESTRATION.md`. P0 is done. Next rungs:

- **P1 — bounded tuning + approval gate.** Add a `tune(param, value)` tool clamped to config
  ranges; add a UI approval gate. Turn on `ClaudePolicy` by setting `ANTHROPIC_API_KEY` (already
  wired + gated) for normal-mode step selection; keep `RuleBasedPolicy` as the offline default.
- **P2 — recovery mode.** On a failed/low-confidence verifier, hand the agent gated autonomy to
  pick/parameterize **pre-built recovery actions** (regrasp, re-approach, re-localize) with
  propose → human-approve → guarded execute → verify. Bounded attempts, then stop + request help.
- **P3 (stretch) — generated snippets.** Let the agent author recovery code; run it in a restricted
  sandbox, **dry-run against the twin/sim first**, require approval, and still pass the safety spine.
- **Frontend:** add the `/ws/agent` runner + **checkpoint-chat** panel + **approval** control; later
  perception overlays (masks / pose axes / verdicts).

Modes recap (from the plan, per the product owner's decision): **normal mode** = step-by-step over
vetted skills + param tuning within bounds, chat at checkpoints; **recovery mode** = full autonomy
to diagnose, may propose operations/snippets outside verified bounds, always propose→approve→verify.

---

## 6. Hardware safety (real-arm runs)

- Keep a physical **E-stop** in reach. `Esc` in the teach UI triggers stop.
- `move_gohome` is **not collision-checked** — clear the workspace before homing.
- Set the real **payload mass + CoG** (`scripts/init_xarm.py` `DEFAULT_PAYLOAD_*`, ~0.86 kg for the
  parallel gripper) or the arm sags on brake and collision detection skews.
- `init_xarm.py` always leaves the arm **braked** (servos off) in a `finally`; the driver
  re-enables on `connect()`.
- Prefer **dry-run / sim** and deliberate failure injection for the demo; never let the agent send
  motion that hasn't passed the guards.

---

## 7. Housekeeping / review follow-ups

- Confirm post-merge: `uv.lock` tracked, `*.pptx` + `.claude/settings.local.json` untracked, the
  xArm manual PDF still tracked.
- Ensure every env var the code reads is documented in `.env.example` (names must match exactly).
- Consider CI: `uv sync && uv run pytest` on push.
- Docs index: `docs/README.md`. Key docs: `ARCHITECTURE.md`, `WORKFLOW.md`, `DIGITAL_TWIN.md`,
  `WORLD_MODEL_REQUIREMENTS.md`, `CAPABILITY_pick_place.md`, `ZEON_INTEGRATION.md`,
  `AGENT_ORCHESTRATION.md`, `PROJECT_PLAN.md`.
```
