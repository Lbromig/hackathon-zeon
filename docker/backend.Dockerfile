# FastAPI backend + drivers + the vendored xArm SDK.
#
# Dev-oriented: the repo is bind-mounted at run time (see docker-compose.yml) so
# uvicorn --reload picks up edits without a rebuild. Only dependency changes
# (pyproject.toml / uv.lock) need `docker compose build`.
FROM python:3.13-slim

# libgl1 + libglib2.0-0: opencv-python links against them at import. Without these
# `import cv2` raises and every camera driver is silently skipped — which looks
# exactly like "no camera attached", so it is worth the ~40 MB to rule out.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing source doesn't invalidate the install layer.
# third_party/ is copied too: the xArm SDK is a path dependency (pyproject
# [tool.uv.sources]), so `uv sync` cannot resolve without it present.
COPY pyproject.toml uv.lock ./
COPY third_party/ ./third_party/
RUN uv sync --locked --no-dev

COPY . .

EXPOSE 8000
# --reload-dir, or the reloader polls the whole bind mount — including the host's
# .venv, which is thousands of files and triggers spurious restarts.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", \
     "--reload-dir", "backend", "--reload-dir", "core", "--reload-dir", "drivers"]
