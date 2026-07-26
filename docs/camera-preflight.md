# Depth camera preflight

`camera_probe.py` establishes whether a depth camera is attached, whether this
process can open it, and what to do when it cannot.

This exists because physical verification has a failure mode that is worse than
not working: a verification step that silently observes nothing and reports
success. Anything that claims to have checked a cap, a seat or a transfer has to
prove first that it had a working sensor. This is that gate.

## Quick start

    python3 camera_probe.py            # full preflight, names the blocker and the fix
    python3 camera_probe.py detect     # what is attached, stdlib only, opens nothing
    python3 camera_probe.py --json     # same, machine readable, for CI or a runner
    python3 camera_probe.py capture    # write frames to disk

`detect` imports nothing outside the standard library, so it runs on a bare
system interpreter with no virtualenv. The capture backends are imported lazily
and their absence is reported rather than raised.

Exit code is 0 only when frames actually flowed. A runner can gate on it.

## Why the diagnoses are the point

"Could not open camera" is not actionable at a bench. Every failure path resolves
to one named diagnosis with one operator action:

| Diagnosis | Means |
| --- | --- |
| `no_device` | Nothing attached. Cable or port. |
| `permission_denied` | Attached, but the OS refuses this process. |
| `driver_claimed` | Attached, but another driver owns the interfaces. |
| `no_raw_usb` | This process cannot enumerate USB at all. |
| `backend_missing` | The library is not installed here. |
| `opened_but_no_frames` | Opened, delivered nothing. Contention or a slow link. |

The distinction between `no_device` and `no_raw_usb` is the one that costs the
most time. When a libusb backend cannot enumerate, librealsense reports
`No device detected. Is it plugged in?`, which reads as a hardware fault and
sends you to check cables that are fine. The probe checks the total libusb
device count before believing that message.

The USB link speed check matters for the same reason. A D400 series camera that
negotiates USB 2 still enumerates and still opens. It fails later, under load, at
the least convenient moment. The preflight reports the negotiated speed up front.

## Hardware state, honestly

Verified on the bench Mac on 2026-07-25, Apple Silicon, macOS 15:

- An **Intel RealSense D405** is attached and healthy. Serial `351623070085`,
  `0x8086:0x0b5b`, negotiated **5 Gbps USB 3**. IOKit lists it as `registered,
  matched, active`.
- Note this is a **D405, not a D435**. The D405 is the short range member of the
  family. Any calibration or standoff figure taken against a D435 does not carry
  over and has to be redone.

Not working, and not to be claimed as working:

- **No frame has been captured from this camera.** Not one. Everything below is
  about why, and nothing downstream should assume otherwise.
- **AVFoundation is permission denied.** `cv2.VideoCapture` on indices 0 through
  3 all fail with `not authorized to capture video (status 0)`. macOS attributes
  the camera grant to the responsible application of the process tree, which on
  this machine is `/Applications/Claude.app`, and it is not granted. The probe
  walks the process tree and prints the responsible app rather than guessing.
  Granting it requires an application restart, not just a reload.
- **The librealsense path is blocked separately.** librealsense 2.58.3 installed
  cleanly from Homebrew, but `rs-enumerate-devices` reports no device. The cause
  is not the camera: libusb enumerates **zero** USB devices of any kind from this
  process. Apple's `UVCAssistant` also holds the camera's interfaces, which
  independently prevents the libusb backend from claiming them.
- **`pyrealsense2` is not installed and is not trivially installable here.** The
  Homebrew formula ships the C++ library and CLI tools without Python bindings.
  Bindings need a source build with `-DBUILD_PYTHON_BINDINGS=ON`. This is not yet
  done, so metric depth is unavailable in Python on this machine.

## What that means for depth

The two paths are not equivalent and the difference decides what can be built.

AVFoundation gives a 2D image. That is enough for fiducial localisation, so
ArUco based pose estimation can proceed on the permission fix alone.

Metric depth in millimetres comes from librealsense. Depth is what makes a
height delta measurable, and a height delta is the most robust signal available
for cap on versus cap off, since it does not depend on lighting, on a printed
marker, or on the object being non reflective. Until the SDK path works, any
depth based verification is unimplemented rather than untested.

## Unblocking, in order

1. Grant camera access to the responsible application that
   `python3 camera_probe.py` prints, in System Settings > Privacy & Security >
   Camera, then fully quit and reopen that application. Confirm with
   `python3 camera_probe.py`, which should move `opencv` to `OK`. This is a
   security setting and is deliberately left to a human.
2. For metric depth, build the Python bindings from source against the installed
   librealsense, and re-run the preflight. Expect the `driver_claimed` diagnosis
   to persist on macOS; a Linux host is the reliable route for the libusb
   backend.
3. Re-measure standoff and accuracy for the D405 specifically. Do not carry over
   D435 numbers.
