# Teaching the 15 workflow waypoints

Operator procedure. Everything here is done from the **Teach** tab; nothing on this page
needs the execution engine, which is being rebuilt in parallel.

You are teaching 15 `(arm, waypoint)` pairs: **10 on `right`**, **5 on `left`**. The
checklist in the teach tab is the authority — it reads the same spec
([`core/waypoints.py`](../../core/waypoints.py)) the engine will pre-flight against, so a
waypoint that is ticked there is one the workflow can use.

---

## 1. Start the backend against the real arms

```sh
just sync                    # once
just sim-only cameras        # real arms + real Opentrons, cameras simulated
just frontend                # second terminal
```

`just sim-only cameras` is the recommended teaching mode: the arms are real, and the four
bench cameras are simulated so a camera that will not open cannot get in the way of a teach
session. Use `just real` if you also want the real cameras.

`just backend` is **fully simulated** and will happily let you "teach" 15 waypoints against
mock arms that are not on the bench. Check the simulated/real indicator in the header before
you start; simulation is the default (D29/Q8), and this is the mistake that default buys.

The arms must be powered and initialized first:

```sh
just init-arm ip=192.168.3.13    # right
just init-arm ip=192.168.3.11    # left
```

Then, in the teach tab: pick the arm and press **Connect** if it is not already connected.

## 2. Safety sequence, every time

In this order, per arm:

1. **Clear errors.** An e-stop or a collision latches an error that blocks *every*
   subsequent motion command until it is cleared and the arm re-enabled. The backend
   refuses to move a faulted arm, so if the panel shows `error <n>`, press **Clear errors**
   before anything else. (A gripper fault latches separately and is cleared by the same
   button.)
2. **Enable.** Motors on. **Disable** engages the brakes.
3. **Support the arm, then Hand-guide.** Hand-guiding is xArm mode 2: the controller holds
   the arm against gravity from the *configured payload* and lets you back-drive the joints.
   If the payload is understated the arm **sinks** the moment you let go, and if it is
   overstated it climbs. Take the weight before you press the button.
4. **Hand-guide off before any commanded move.** Programmed motion does not behave normally
   in a teaching mode. The panel keeps the hand-guiding banner up the whole time it is on,
   and the toggle stays clickable even when the rest of the motion controls are disabled —
   turning it off is how you get back to position control.
5. **E-STOP** is the red button, or `Esc`. It deliberately bypasses the one-command-per-arm
   lock: an e-stop that queued behind the move it is interrupting would be useless. After an
   e-stop, go back to step 1.

Jog keys, once hand-guiding is off: arrows for X/Y, PgUp/PgDn for Z, `Q`/`E` for yaw,
`[`/`]` to close/open the gripper, `1`–`4` for the linear step size.

## 3. Teach the waypoints, in this order

Work down the checklist: it is already sorted by workflow step, which is the order that
makes physical sense — each waypoint is reachable by a short move from the one before it,
and the arm is never asked to cross the other arm's working volume with a tube in the jaws.

For each row: get the arm there (hand-guide, or jog), then press **Teach here**. Press
**Go** afterwards to confirm the arm returns to the same place, at the tier the workflow
will use.

### `right` — 10 waypoints

| # | Waypoint | Tier | What the arm is doing |
|---|---|---|---|
| 0 | `HOME` | slow | The right arm's initialization home and rest pose. Teach this first — it is the way back. |
| 1 | `APPROACH_RACK` | fast | Clear standoff above the tube rack, before anything is over a tube. |
| 2 | `APPROACH_TUBE_GRAB` | fast | Lined up directly above the target tube, jaws open. |
| 3 | `TUBE` | slow | Down on the tube body at grip height. The jaws close here. |
| 13 | `APPROACH_TUBE_TRANSFER` | medium | Open tube lifted clear of the rack, ready to traverse. |
| 14 | `TRANSITION_ROBOT_TABLE` | fast | Traverse waypoint over the robot table. |
| 15 | `TRANSITION_MID_TABLE` | fast | Traverse waypoint mid-table, between the two benches. |
| 16 | `TRANSITION_LIQUID_HANDLER_TABLE` | fast | Traverse waypoint over the liquid-handler table. |
| 17 | `LIQUID_HANDLER_APPROACH_DECK` | fast | Standoff outside the deck envelope, tube clear of the gantry. |
| 18 | `LIQUID_HANDLER_DECK` | slow | Tube presented on the deck under the pipette. The servo loop starts here. |

### `left` — 5 waypoints

| # | Waypoint | Tier | What the arm is doing |
|---|---|---|---|
| 5 | `APPROACH_CAP_GRAB` | fast | Left tool brought in beside the cap while the right arm holds the tube. |
| 6 | `CAP_GRAB` | slow | Closed on the cap at grip height. Decap (360° in 90° bites) runs from here. |
| 9 | `APPROACH_CAP_STORE` | fast | Cap carried clear of the tube, standing off above its store position. |
| 10 | `CAP_STORE` | slow | Cap lowered into the store. The jaws open here and leave it behind. |
| 12 | `HOME` | slow | Left arm parked clear of the right arm's traverse path. |

The gaps in the numbering are the workflow steps that name no waypoint: gripper closes at 4
and 7, the decap rotation at 8, the cap release at 11, the vision servo loop at 19, and the
liquid handler retracting Z at 20. Step 0 is not a workflow step at all — it is
initialization, which is why the right arm's `HOME` sits there.

Steps 14–18 are the numbers the brief gives those five waypoints; the rest are the order of
the uncap sequence that precedes them.

### Practical order at the bench

The two arms are independent for teaching, so pick whichever is easier to reach. A
reasonable session:

1. `right` `HOME` — first, so you always have a safe pose to return to.
2. `right` steps 1–3 with a tube in the rack: `APPROACH_RACK`, `APPROACH_TUBE_GRAB`, `TUBE`.
3. `left` steps 5–6 with the right arm holding a tube at `TUBE`: `APPROACH_CAP_GRAB`,
   `CAP_GRAB`. These two have to be taught with the tube actually present; the cap position
   depends on it.
4. `left` steps 9–10: `APPROACH_CAP_STORE`, `CAP_STORE`, then `left` `HOME`, which must be
   clear of the right arm's traverse path.
5. `right` steps 13–18, walking the tube across the tables. Teach these with the tube in the
   jaws — the traverse waypoints exist to keep the tube clear of things, and an empty
   gripper teaches the wrong clearance.

### Why `TRANSITION_*` and `LIQUID_HANDLER_*` are on the **right** arm

The original brief names those five with a `LEFT_ARM_` prefix, but the arm that carries the
tube across the tables to the deck is the right one. Open question **Q3** resolved this: the
*names* were the mistake, and the device prefix is dropped entirely. They belong to `right`.
Do not re-teach them on `left` — the backend refuses that save.

## 4. What "taught" means

Pressing **Teach here** snapshots where the arm is *right now* and stores:

- **the joint angles** (6 values, degrees),
- **the cartesian pose** (x/y/z mm, roll/pitch/yaw degrees),
- the gripper width, if the gripper reports one,
- a timestamp.

**Replay prefers the joints.** Those exact angles were physically reached, so there is no
inverse-kinematics branch to guess at; two joint solutions can put the tool in the same
place with the elbow on opposite sides, and only one of them is the one you taught.
Cartesian replay is the fallback for a pose whose joints are missing (a hand-edited file, or
a pose saved on an arm with a different axis count), and the checklist marks such a row
`no joints — cartesian replay only`.

Both replay paths are distance- and rotation-capped: a target more than
`max_move_to_jump` mm away, or more than 90° of wrist rotation, is refused rather than
executed — jog closer first. Joint targets are also pre-flighted against the joint soft
limits before the arm moves.

The library is written to `data/teach_poses.json` **before the save request returns**, via a
temp file plus an atomic rename, so a crash can never leave it half-written. It survives a
backend restart; deletion likewise.

### `HOME` is per arm

`HOME` is an ordinary waypoint name that exists **once per arm and means a different pose on
each**. There is no shared home and nothing is derived from the other arm's. If an arm has
no `HOME`, initialization **warns and does not move** — it will not guess a home pose,
because a guessed home is a full-speed move to a place nobody chose.

### Waypoints belong to exactly one arm

Every waypoint above is owned by one device (`HOME` by both, as two separate poses). The
backend refuses to save, and refuses to move to, a waypoint the acting arm does not own —
naming both the owner and the arm that asked. The checklist only ever offers an arm its own
waypoints, so the wrong pairing is not expressible from the UI.

If you see one of these, act on it before running anything:

| Warning | Meaning |
|---|---|
| **`X` is taught under `right` but is owned by `left`** | A hand-edited file, or an older library. That pose will never be used, and the arm that needs it is still untaught. Re-teach on the correct arm and delete the stray. |
| **`home` looks like the workflow waypoint of the same name** | Names are case-sensitive. `home` is not `HOME`, and does not count as taught. |
| **`rack_A1_above` is not a workflow waypoint** | A scratch point. Harmless, kept, and not on the checklist. |

Scratch points are still welcome: the pose library below the checklist saves any name you
like, for the intermediate positions that make a teach session practical. They are badged so
they can never be mistaken for progress against the workflow.

## 5. When you are done

The checklist should read **10 of 10** on `right` and **5 of 5** on `left`, with no red
warnings. Walk both arms through their waypoints with **Go** once more before handing over —
a waypoint taught while the arm was resting against something, or with the tube absent, will
look perfect in the list and be wrong on the bench.
