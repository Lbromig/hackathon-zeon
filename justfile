# hackathon-zeon task runner — https://github.com/casey/just
# List recipes with `just` or `just --list`.

# Create the venv + install all deps (including the vendored xArm SDK).
sync:
    uv sync

# Run the FastAPI backend from the repo root (single import root).
dev:
    uv run uvicorn backend.app.main:app --reload

# Install and run the Vue dev server.
frontend:
    cd frontend && npm install && npm run dev

# Run the Python test suite (no hardware required).
test:
    uv run pytest backend/tests -q

# Initialize an xArm at the given IP, e.g. `just init-arm ip=192.168.3.13`.
init-arm ip:
    uv run python scripts/init_xarm.py --ip {{ip}}
