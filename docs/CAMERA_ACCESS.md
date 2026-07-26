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

This matters more than anything else below, because the two processes have completely
different network exposure. There is no Docker stack any more — the USB devices cannot be
passed into a VM on macOS, so the compose file was dropped.

**Backend** (`just dev`) — **loopback only**: `127.0.0.1:8000`, uvicorn's default host,
no `--host` flag in the justfile. Confirmed: a request to `192.168.1.174:8000` from the
LAN interface is refused. Reaching it directly from the network takes an explicit
`--host 0.0.0.0`.

**Frontend** (`just frontend`, `npm run dev`) — **every interface**: `0.0.0.0:5173`,
because `package.json`'s `dev` script is `vite --host`. Its dev proxy forwards `/api` and
`/ws` to the backend from the *server* side, so a loopback-only backend is still fully
reachable through it: `http://<bench-ip>:5173/api/cameras` answers to anyone who can route
to the bench. `npm run dev:local` (plain `vite`) is the loopback-only variant.

## Security: there is none

**No authentication on any endpoint.** No API key, no token, no `Depends()` guard, no
allowlist. The only key in the project is `ANTHROPIC_API_KEY`, which is outbound.

**CORS is not access control.** `core/config.py` sets
`cors_origins = ["http://localhost:5173"]`, and that only stops *browser JavaScript on
another origin* from reading a response. It does nothing about `curl`, `<img src=...>`,
ffmpeg, or a Python script. Verified: a request with `Origin: http://evil.local:1234`
returns 200 with the full body — it just comes back without an `Access-Control-Allow-Origin`
header, which only the browser cares about.

So, to answer the question directly: **while the frontend dev server is up, anyone on the
same network can list the cameras, pull snapshots, and watch the MJPEG streams without
credentials** — and, through the same unauthenticated API on `:5173`, hit the teach,
workflow, motion and agent endpoints, which command real arms. Nothing limits this now
that the stack runs on the host: the cameras and the arms are both genuinely attached, so
the feeds served over the LAN are the real bench.

Mitigations, cheapest first:

1. `npm run dev:local` instead of `npm run dev` unless a second machine actually needs the
   UI. That puts both processes back on loopback.
2. Stop the dev server when it is not in use — it is the only thing bound to the network.
3. If a second machine genuinely needs the feeds, put a shared-secret header check in
   front of the app (a one-function middleware) rather than relying on the network.

## Pointing a second machine at these cameras

Set **`HZ_CAMERA_HOST`** to the bench backend and start the second machine's backend
normally:

```bash
HZ_CAMERA_HOST=http://192.168.1.174:8000 uv run uvicorn backend.app.main:app --reload
```

It rewrites *every* camera slot to the `remote` driver (`core/config.py`
`_apply_camera_host`), which consumes the far end's `/stream` pixels and its intrinsics.
All-or-nothing by design, and it wins over `CAM_*` and `HZ_FLEET_FILE` alike — a machine
either has the cameras or it borrows them. The slot ids must match on both ends.

Frames enter at the driver layer exactly like local ones, so detection, twin fusion and
the Cameras tab all work unchanged, and the local backend re-serves its own MJPEG to its
own frontend. Detection deliberately runs again locally rather than copying the remote's
results. Two limits: MJPEG carries no depth (tag *distance* still resolves from solvePnP,
`depth_m` and `camera_xyz` stay null), and never point it at its own backend — it would
proxy its own cameras forever.

The other slot types, all local:

| `CAM_<SLOT>_TYPE` | Source | `CAM_<SLOT>` value |
| --- | --- | --- |
| `realsense` | librealsense, RGB-D (needs root on macOS) | device serial |
| `camera` | `cv2.VideoCapture`, UVC / path / RTSP | OpenCV index, file path, or RTSP URL |
| `still` | a saved PNG replayed as a frame | path to `*_color.png` |
| `remote` | another backend's MJPEG endpoint | its base URL (usually set via `HZ_CAMERA_HOST`) |
| `mock_tag_camera`, `mock_camera` | synthetic | — |

Note that `camera` also accepts a URL string, which looks like it would do the same job —
but as noted above OpenCV will not open this backend's MJPEG endpoint, which is why the
`remote` driver parses the multipart itself.

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
