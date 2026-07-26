# Connecting to the bench cameras

How the backend exposes camera feeds, what a second machine on the same network can
reach, and what it cannot. Verified against the running stack on 2026-07-25.

## Transport: plain HTTP, no WebRTC

There is no WebRTC, no RTSP server, no signalling, no `aiortc` — grep the deps if you
doubt it. Everything is ordinary HTTP off the FastAPI app
([backend/app/api/cameras.py](../backend/app/api/cameras.py)):

| Endpoint | Method | Content type | What it gives you |
| --- | --- | --- | --- |
| `/api/cameras` | GET | JSON | one row per camera slot: state, w/h/fps, depth, error |
| `/api/cameras/devices` | GET | JSON | attached RealSense units (serial, firmware) |
| `/api/cameras/{id}/stream` | GET | `multipart/x-mixed-replace; boundary=frame` | live MJPEG |
| `/api/cameras/{id}/snapshot` | GET | `image/jpeg` | one frame, cheap |
| `/api/cameras/{id}/detections` | GET | JSON | latest AprilTag/CV detections for that camera |
| `/api/cameras/{id}/connect` | POST | JSON | try to open the device |
| `/api/cameras/{id}/stop` | POST | JSON | release the device |
| `/ws/state` | WS | JSON, ~2 Hz | fleet snapshot + `cameras` block with detections |

Camera ids: `gripper_cam`, `overview_cam`, `handover_cam`.

Video and overlay are deliberately separate: MJPEG carries pixels, detections travel as
JSON, and the UI draws an SVG overlay on top. A stalled detector never stalls the video.

## Connecting

**Browser** — the whole point of MJPEG is that nothing special is needed:

```html
<img src="http://HOST:PORT/api/cameras/overview_cam/stream" />
```

**curl:**

```bash
curl http://HOST:PORT/api/cameras | jq .
curl -o frame.jpg http://HOST:PORT/api/cameras/overview_cam/snapshot
curl -N http://HOST:PORT/api/cameras/overview_cam/stream > stream.mjpg   # ^C to stop
```

**Python** — MJPEG multipart is a few lines; parse on the boundary:

```python
import requests
r = requests.get("http://HOST:PORT/api/cameras/overview_cam/stream", stream=True)
buf = b""
for chunk in r.iter_content(4096):
    buf += chunk
    a, b = buf.find(b"\xff\xd8"), buf.find(b"\xff\xd9")   # JPEG SOI / EOI
    if a != -1 and b > a:
        jpeg, buf = buf[a:b + 2], buf[b + 2:]
        ...  # cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
```

Note: `cv2.VideoCapture("http://.../stream")` does **not** work against this endpoint —
tested, this OpenCV build refuses to open it. Parse the multipart yourself.

The first request to `/stream` is what starts the capture worker; it keeps the device open
for the backend's lifetime (`HZ_CAMERA_IDLE_LINGER_S=0` means "never release", because
repeatedly reopening a UVC device on macOS degrades it).

## Which host and port

This matters more than anything else below, because the two ways of running the stack
have completely different network exposure.

**Host stack** (`just dev` + `just frontend`) — **loopback only**:

- backend `127.0.0.1:8000` (uvicorn's default host; no `--host` flag in the justfile)
- frontend `[::1]:5173` (Vite's default; no `--host` flag in `package.json`)

Nothing on the network can reach either. Confirmed: a request to `192.168.1.174:8000`
from the LAN interface is refused.

**Docker stack** (`docker compose up`) — **published on every interface**:

- backend `0.0.0.0:8100 -> 8000`
- frontend `0.0.0.0:5273 -> 5173`, and its Vite dev proxy forwards `/api` and `/ws`
  straight to the backend, so `:5273/api/...` is a second door to the same API

So with compose up, `http://<bench-ip>:8100/api/cameras` answers to anyone who can route
to the bench. Confirmed working from the LAN address while writing this doc.

## Security: there is none

**No authentication on any endpoint.** No API key, no token, no `Depends()` guard, no
allowlist. The only key in the project is `ANTHROPIC_API_KEY`, which is outbound.

**CORS is not access control.** `core/config.py` sets
`cors_origins = ["http://localhost:5173"]`, and that only stops *browser JavaScript on
another origin* from reading a response. It does nothing about `curl`, `<img src=...>`,
ffmpeg, or a Python script. Verified: a request with `Origin: http://evil.local:1234`
returns 200 with the full body — it just comes back without an `Access-Control-Allow-Origin`
header, which only the browser cares about.

So, to answer the question directly: **while `docker compose up` is running, anyone on the
same network can list the cameras, pull snapshots, and watch the MJPEG streams without
credentials** — and, through the same unauthenticated API, hit the teach, workflow, motion
and agent endpoints, which command real arms.

The one thing that limits the damage today is USB, not any control we wrote: on macOS the
container gets no USB passthrough, so inside the container the RealSense and UVC slots
read as `error`/`disconnected` and stream nothing. That is an accident of the platform,
not a boundary. Two caveats:

- The repo is bind-mounted into the container, so a `still`-type slot (which replays a
  saved PNG from `temp/captures/`) *would* serve real bench imagery over the LAN.
- The arms are reached by TCP over the bridge network and work fine from the container.
  The exposed motion endpoints are the actual risk, not the video.

Mitigations, cheapest first:

1. `docker compose down` when you are not using it. The host stack is loopback-only.
2. Bind the published ports to loopback: `"127.0.0.1:8100:8000"` and
   `"127.0.0.1:5273:5173"` in `docker-compose.yml`. Costs nothing, keeps compose usable.
3. If a second machine genuinely needs the feeds, put a shared-secret header check in
   front of the app (a one-function middleware) rather than relying on the network.

## Pointing a second machine at these cameras

There is **no env var for "local hardware vs. remote API" today.** `CAM_<SLOT>_TYPE`
selects a *driver*, and every option is local:

| `CAM_<SLOT>_TYPE` | Source | `CAM_<SLOT>` value |
| --- | --- | --- |
| `realsense` | librealsense, RGB-D (needs root on macOS) | device serial |
| `camera` | `cv2.VideoCapture`, UVC / path / RTSP | OpenCV index, file path, or RTSP URL |
| `still` | a saved PNG replayed as a frame | path to `*_color.png` |
| `mock_tag_camera`, `mock_camera` | synthetic | — |

`camera` accepts a URL string, which looks like the remote path — but as noted above
OpenCV will not open this backend's MJPEG endpoint, so it does not work. A clone that
wants these feeds needs either a small `http`-type camera driver that pulls
`/stream` + `/detections` from another backend, or to consume the endpoints directly
without going through the driver layer.

## Current per-camera state (2026-07-25, host stack)

Not network-related, but worth knowing before you try to pull a feed:

- `overview_cam` — `camera` type, index 1, connected and streaming at 640x360. Snapshot
  returns a valid ~39 KB JPEG, though the last grab reported `frame grab failed`.
- `gripper_cam` — `still` type, but `CAM_GRIPPER=2` resolves to `source: 2`, an OpenCV
  index, not a path. It fails with `image not found: 2`. Point it at a real frame, e.g.
  `CAM_GRIPPER=temp/captures/gripper_cam/latest_color.png`.
- `handover_cam` — `realsense`, disconnected; snapshot times out. Its resolved serial is
  the literal string `# index 0 delivers no frames; no third feed yet`, i.e. an inline
  `.env` comment was swallowed into the value. Put comments on their own line.
