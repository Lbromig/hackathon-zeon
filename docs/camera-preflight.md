# Depth camera preflight

`camera_probe.py` establishes whether a depth camera is attached, whether this
process can open it, and what to do when it cannot. `backend/app/api/camera.py`
exposes that to the UI, along with a live feed and snapshots.

This exists because physical verification has a failure mode worse than not
working: a verification step that silently observes nothing and reports success.
Anything claiming to have checked a cap, a seat or a transfer must first prove it
had a working sensor. This is that gate.

## Quick start

    python3 camera_probe.py            # full preflight, names the blocker and the fix
    python3 camera_probe.py detect     # what is attached, stdlib only, opens nothing
    python3 camera_probe.py --json     # machine readable, for a runner or CI
    python3 camera_probe.py capture    # write frames to disk

`detect` imports nothing outside the standard library, so it runs on a bare
system interpreter with no virtualenv. Exit code is 0 only when frames actually
flowed, so a runner can gate on it.

## API

| Route | Purpose |
| --- | --- |
| `GET /api/camera/preflight` | the gate. Opens nothing permanently, always answers. |
| `GET /api/camera/stream` | MJPEG. Renders in a plain `<img>`, no signalling. |
| `POST /api/camera/snapshot` | write one labelled frame to `snapshots/`. |
| `GET /api/camera/snapshots` | list them, newest first. |
| `POST /api/camera/webrtc/offer` | WebRTC via aiortc, for lower latency. |

MJPEG is the default rather than WebRTC. It needs no signalling, no ICE and no
client library, which matters while the camera itself is still what is being
debugged. WebRTC is there when latency starts to matter; `aiortc` is optional and
its absence returns a 503 naming the install command instead of a 500.

One `FrameSource` owns the device and everyone reads its latest frame, because a
camera admits exactly one reader. Without that, the second viewer to connect
simply fails to open the device.

## Why the diagnoses are the point

"Could not open camera" is not actionable at a bench. Every failure resolves to
one named diagnosis with one operator action:

| Diagnosis | Means |
| --- | --- |
| `no_device` | Nothing attached. Cable or port. |
| `permission_denied` | Attached, but the OS refuses this process. |
| `exclusive_access` | Attached and found, but another process owns it. |
| `driver_claimed` | The UVC driver holds the interfaces. |
| `no_raw_usb` | This process cannot enumerate USB at all. |
| `backend_missing` | The library is not installed here. |
| `opened_but_no_frames` | Opened, delivered nothing. Contention or a slow link. |

Three of these exist because of time already lost to them:

- `no_raw_usb` versus `no_device`. When a libusb backend cannot enumerate,
  librealsense prints `No device detected. Is it plugged in?`. That reads as a
  hardware fault and sends you to check cables that are fine. The probe checks
  the total libusb device count before believing it.
- `exclusive_access` versus a hardware fault. `failed to set power state` means
  the SDK *found* the camera and was refused it. No setting changes this.
- The USB link speed check. A D400 camera that negotiates USB 2 still enumerates
  and still opens, then fails later under load. Speed is reported up front.

### The SDK crashes, and that is contained

librealsense on macOS is not crash safe when it cannot claim the device: it
segfaults about as often as it raises, non-deterministically, for the same call.
Enumeration therefore runs in a throwaway subprocess. A preflight whose entire
job is to report faults must not be killed by the fault it is reporting. A child
that dies by signal is reported as `exclusive_access` with the signal number.

## Hardware state, honestly

Verified on the bench Mac on 2026-07-25, Apple Silicon:

- An **Intel RealSense D405** is attached and healthy. Serial `351623070085`,
  `0x8086:0x0b5b`, negotiated **5 Gbps USB 3**. IOKit lists it `registered,
  matched, active`.
- `pyrealsense2` imports and works: `pip install pyrealsense2-macosx` ships a
  real cp313 arm64 wheel with librealsense 2.56.5 bundled.

Not working, and not to be claimed as working:

- **No frame has been captured from this camera.** Not one.
- **AVFoundation is permission denied**, and not in the way first assumed. The
  responsible process is `com.anthropic.claude-code`, which carries no
  `com.apple.security.device.camera` entitlement at all, so it is refused
  without ever prompting and **will never appear in System Settings**. Toggling
  a setting cannot fix this. Run the probe from a terminal instead, where the
  grant is obtainable.
- **The SDK is blocked by exclusive ownership, not by TCC.** IOKit user clients
  open fine; `USBInterfaceOpen` then fails `kIOReturnExclusiveAccess` because
  macOS `UVCAssistant` holds `UsbExclusiveOwner` on every interface. Re-running
  under `sudo` is the documented workaround for this SDK on macOS and is the
  next thing to try. If that fails, the camera moves to a Linux host.

## Two traps that will cost you 10x

- **The D405 depth scale is 1e-4 m per count, not 1e-3.** The rest of the D400
  series uses 1e-3. Code carried over from a D435 that assumes millimetre counts
  reads every distance **ten times too large**. Always read `get_depth_scale()`;
  never hardcode it. The preflight prints the value it read.
- **This camera re-enumerates.** Its `locationID` moved mid-session and the
  AVFoundation `uniqueID` moved with it. Do not hardcode a device index or a
  uniqueID. Match on the model id or the USB serial.

## What this means for depth

The two paths are not equivalent, and the difference decides what can be built.

AVFoundation gives a **genuine, clean 8-bit image**, not a mangled one: macOS
advertises the D405's real UYVY/YUY2 profiles. That is enough for ArUco and
AprilTag work, so fiducial localisation unblocks on the permission fix alone.

It will **never** give depth. macOS filters out the `Z16`, `Y8` and `L8R8`
formats the camera advertises, OpenCV's AVFoundation backend has no property
plumbing to request them and hardcodes an 8-bit output, and the macOS depth APIs
are `API_UNAVAILABLE(macos)`. Metric depth comes from librealsense or not at all.

Depth is what makes a height delta measurable, and a height delta is the most
robust cap-on/cap-off signal available: it does not depend on lighting, on a
printed marker, or on the object being non-reflective.

## D405 versus D435 for this bench

The swap helps decisively, and the margin is dominated by minimum range.

| Resolution | D405 min-Z | D435 min-Z |
| --- | --- | --- |
| 1280x720 | 100 mm | 280 mm |
| 848x480 | 70 mm | 195 mm |
| 640x360 | 55 mm | 150 mm |

At a 250 mm standoff the D435 produces **no 720p depth at all** and must drop to
848x480, where it sits 1.28x above min-Z with roughly 10% of the image width
dead. The D405 at the same standoff is 2.5x above its 720p min-Z.

Close-range noise also favours it: RMS spatial noise <=1% versus <=2%, temporal
<=0.5% versus <=1%. At 250 mm that is a per-pixel sigma of 2.5 mm against 5.0 mm,
so a 15 mm step is 6 sigma per pixel on the D405. Averaged over a 10 mm cap disc
it is far beyond any plausible detection threshold. **The cap-on/cap-off height
delta is comfortably measurable once the SDK path opens.**

## Unblocking, in order

1. `sudo /opt/homebrew/bin/rs-enumerate-devices`. If it prints the D405 with its
   stream profiles, root defeats the UVCAssistant claim and everything else
   follows. This is the decisive test and it takes ten seconds.
2. For a 2D view, run the probe from Terminal.app and approve the camera prompt,
   then fully quit and reopen Terminal.
3. If `sudo` also fails, move the camera to a Linux host, where V4L2 and udev
   avoid the exclusivity problem entirely. Note Intel's aarch64 wheels are
   cp39/310/312 only, so pin that environment to Python 3.12.
