# hackathon-zeon task runner — https://github.com/casey/just
# List recipes with `just` or `just --list`.

# Create the venv + install all deps (including the vendored xArm SDK).
sync:
    uv sync

# Start the backend SIMULATED — the default, and the documented first-run command (D29).
# A fresh clone runs the whole workflow with no configuration and no hardware attached.
# HZ_SIM=all is stated explicitly rather than relied on, so the recipe still means
# "simulated" for someone whose .env sets HZ_SIM to something else.
backend:
    HZ_SIM=all uv run uvicorn backend.app.main:app --reload

# Alias for `backend`, kept because it is what the muscle memory types.
dev: backend

# Same backend, driving REAL hardware: arms, Opentrons and the four bench cameras.
# Explicit opt-in, deliberately more to type — a bench machine silently driving a simulated
# arm is worse than a laptop needing one extra word.
# Needs the arms powered and initialized (`just init-arm ip=...`) and the cameras attached.
real:
    HZ_SIM=none uv run uvicorn backend.app.main:app --reload

# Real cameras, simulated motion — the safe combination for vision work at the bench.
real-cameras:
    HZ_SIM=arms,lh uv run uvicorn backend.app.main:app --reload

# Simulate one class or one device and drive the rest: a fleet id, a class
# (arms | lh | cameras), or a comma list. e.g. `just sim-only left,ot`
sim-only what:
    HZ_SIM={{what}} uv run uvicorn backend.app.main:app --reload

# Install and run the Vue dev server.
frontend:
    cd frontend && npm install && npm run dev

# Run the Python test suite. Simulated, and it cannot reach hardware (R-SIM-7).
test:
    HZ_SIM=all uv run pytest backend/tests -q

# Kill orphaned camera child processes.
#
# Instead of a pid registry file plus boot-time adoption (review S9). The hazard is real and
# documented: opening a UVC device on macOS can block in an uninterruptible kernel wait that
# `kill -9` cannot reap, so a child can outlive the parent that spawned it and keep a device
# claimed — after which every later run finds the camera busy. A registry file would be a
# second source of truth that can disagree with `ps`; this asks the OS, which cannot.
#
# `-` prefixes and `|| true` so the recipe succeeds when there is nothing to reap, which is
# the normal case and must not look like a failure.
reap:
    -@pkill -f 'drivers.camera_proc' || true
    -@pkill -f 'avfsnap' || true
    @echo "remaining camera processes (empty is correct):"
    -@pgrep -fl 'drivers.camera_proc|avfsnap' || true

# Pretty-print the tail of the one log file, for a human at a terminal. The JSONL is the
# transport and `GET /api/logs` is what the frontend reads; this is the `tail -f` answer.
logs n="60":
    @tail -n {{n}} data/logs/zeon.jsonl 2>/dev/null | uv run python scripts/pplog.py

# Identify the bench cameras by a key that survives a replug, and snapshot each one.
cameras:
    uv run python scripts/identify_cameras.py --snapshot

# Initialize an xArm at the given IP, e.g. `just init-arm ip=192.168.3.13`.
init-arm ip:
    uv run python scripts/init_xarm.py --ip {{ip}}
