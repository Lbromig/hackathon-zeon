# Note for S3 — camera identity, salvaged from `scripts/validate_camera.py`

Date 2026-07-26. Written during Wave 0 as the deletion pass removed `scripts/validate_camera.py`
(992 LOC of bench diagnostics). **This is preserved knowledge, not an implementation.** S3 owns
R-CAM-6/7/8 and decides what to build; this file exists so S3 does not have to rediscover the
four dead ends that script already paid for.

Related: [REQUIREMENTS.md §17.3](REQUIREMENTS.md) (R-CAM-6…9, and the constraint table on the
mechanism), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) D18/D19, and
[GAP_ANALYSIS.md §3.1](GAP_ANALYSIS.md) finding 2 (the slot named `gripper_left_cam` is
physically the *right* arm's camera).

---

## 1. The RealSense SDK serial listing — what the deleted `--list` stage did

`stage_list(rs, devices)` in `validate_camera.py` was the only reliable identity route in the
repo. Its shape, in full, because it is small:

```python
import pyrealsense2 as rs

devices = list(rs.context().query_devices())
for d in devices:
    def _get(key: str) -> str:           # every field can raise on a half-open device
        try:
            return d.get_info(getattr(rs.camera_info, key))
        except Exception:
            return "?"
    name     = _get("name")              # e.g. "Intel RealSense D435"
    serial   = _get("serial_number")     # <-- the value cfg.enable_device() matches on
    firmware = _get("firmware_version")
```

The serial obtained this way is the one to pin a slot with, because
`rs.config().enable_device(serial)` compares against **exactly this string**.

The script then printed a ready-to-paste `.env` block, deriving the slot list from
`core/config.py` rather than restating it (`CAM_ENV` × the `realsense` entries of
`DEFAULT_FLEET`), so a fleet edit could not silently desync the output. Assignment was
**positional and therefore a guess** — the script said so, and told the operator to reorder it
to match which camera is physically where. That guess is precisely what R-CAM-6 replaces.

It also warned when more units were attached than there were fleet slots, and closed with the
sentence worth keeping verbatim:

> Leaving a serial blank binds by enumeration order, which is stable only with a single camera —
> with two or more, an unpinned rig silently swaps viewpoints between runs and every pose it
> reports is attributed to the wrong camera.

**Cost of this route:** `pyrealsense2` on macOS needs **root** (it claims the UVC interface from
macOS's own driver through libusb; without root `libusb_claim_interface` returns
`RS2_USB_STATUS_ACCESS`, surfacing as "failed to set power state" — which reads like a missing
camera but is a privilege problem). There is also no macOS wheel on PyPI. This is the reason the
bench moved to plain UVC/OpenCV in the first place, and therefore the reason the SDK serial is a
*preference* (R-CAM-6: "prefer the RealSense SDK serial whenever a slot runs the `realsense`
driver") rather than the mechanism.

## 2. The documented fact: the USB-descriptor serial ≠ the SDK serial

From `_usb_root_hubs()`'s docstring, and repeated in `stage_list`'s:

> The USB descriptor exposes a **different** serial than the SDK reports, so pairing the two
> would invent a mapping.

Concretely: the serial printed by `ioreg -p IOUSB -w0 -l` / `system_profiler` is **not** the
value `enable_device()` matches. **Pinning a camera with a USB-descriptor serial binds nothing,
silently** — the slot looks configured and opens whatever enumeration order hands it.

Consequence for S3: do not build identity on `ioreg`/`system_profiler` output, and do not try to
correlate a USB serial with an SDK serial. The requirements table already marks USB enumeration
as "**no** — cannot be mapped to a cv2 index".

`ioreg` *is* still useful for one thing that is not identity: counting units and the distinct USB
root hubs they sit on (`locationID >> 24` is the root hub), which is what decides whether a
per-camera-fine bandwidth budget is collectively fatal. That is a bandwidth diagnostic, not an
identity.

## 3. The second dead end: device *names* cannot identify a cv2 index

`excluded_indices()`' docstring records a measured failure:

> This replaces an earlier attempt to identify an index by its AVFoundation *name*, which was
> wrong: ffmpeg and OpenCV enumerate the same devices in different orders. Verified on this
> bench — **ffmpeg reports index 3 as the D405, while OpenCV's index 3 is the built-in MacBook
> camera.** Any protection derived from that correspondence protects the wrong device.

So `ffmpeg -f avfoundation -list_devices true -i ""` tells you *what is attached* — it is a
legitimate inventory — but its index column is not OpenCV's index column. The same warning is
duplicated at `core/config.py`'s `camera_exclude_indices` comment, which survives the deletion
pass.

## 3b. The route the requirements table does not mention — `scripts/identify_cameras.py`

**Read this before implementing R-CAM-6.** `scripts/identify_cameras.py` (289 LOC) and
`scripts/avfsnap.swift` (159 LOC) **survive the deletion pass** and are not on the delete list.
They implement a third identity route that §17.3's table does not list, and it is better than
the content fingerprint alone:

**AVFoundation `uniqueID` embeds the USB `locationID`.**

```
0x124300080860b5b
  ^^^^  locationID 0x01243000  -> the physical USB port
                               -> ioreg gives that port's "USB Serial Number"
```

So `uniqueID` identifies a **physical port**, and the USB serial identifies the **unit plugged
into it**. Both survive macOS renumbering, and — unlike librealsense — **neither needs root**.

The pieces already written:

| Function | What it does |
|---|---|
| `avf_cameras()` | `system_profiler SPCameraDataType` → `[{name, unique_id}]`. system_profiler reports uniqueID; **ffmpeg does not**. |
| `usb_serials()` | `ioreg -p IOUSB -l -w 0` → `{locationID_hex: {product, serial}}` |
| `location_of(unique_id)` | `uniqueID` → the locationID it embeds (`'0x124300080860b5b'` → `'1243000'`) |
| `ffmpeg_order()` | printed **for reference only**, so a stale `CAM_*` index in `.env` can be recognised as stale. Deliberately not used to select a device. |
| `avfsnap.swift` | captures via `AVCaptureDevice(uniqueID:)`, and **fails loudly** when there is no such device |

Three measured warnings from that file, all of which are the reason it fingerprints every frame
rather than trusting the binding:

1. **macOS renumbers video devices between two consecutive `ffmpeg -list_devices` calls seconds
   apart, with nothing touched.** Not only on replug. This is stronger than the instability
   `startup_snapshot.py` documents.
2. `ffmpeg -f avfoundation -i "<exact device name>"` *does* bind correctly and refuses an
   unknown name — **but the two D405s share one name**, so a name cannot separate them.
3. `ffmpeg -f avfoundation -i "<uniqueID>"` **does not match the uniqueID.** It silently opens
   the *default* device and exits zero. A bogus uniqueID also "succeeds". So you get a plausible
   frame from the wrong camera with a green exit status — the exact failure R-CAM-7 forbids.

Reconciling this with §2's "USB enumeration cannot be mapped to a cv2 index": both are true and
they are not in conflict. `ioreg` alone cannot be mapped to a cv2 index — correct. But
`uniqueID` **can** be mapped to a USB unit *and* opened directly (via AVFoundation, not cv2),
which sidesteps the cv2 index entirely. If S3 opens cameras through cv2 indices, §2 stands and
the content fingerprint is the mechanism; if a child process can capture via `avfsnap`, the
port+serial pair is an exact identity. **Either way the fingerprint stays**, because of warning 3.

## 4. Two operational facts worth carrying into the child process

* **Settle frames.** `SETTLE_FRAMES = 5` — "auto-exposure needs a few frames; frame 1 routinely
  reads saturated." The same constant is in `startup_snapshot.py`. A fingerprint computed from
  frame 1 is a fingerprint of an over-exposed frame.
* **Never open a non-lab device.** The script probed cv2 indices 0…9 but opened only units whose
  AVFoundation name contained `realsense`, because the built-in MacBook camera, Desk View, a
  Continuity iPhone and screen capture are *always present* — so a misconfigured slot backed by
  one of them "looks healthy" while streaming the operator's face. `CAM_EXCLUDE_INDICES` exists
  for exactly this and is still honoured by `core/config.py`.

## 5. What this leaves S3 to build

Nothing here implements R-CAM-6/7. The decision already taken (REQUIREMENTS §17.3) is:

1. **Content fingerprint** as the general mechanism — probe each cv2 index in the child, capture
   past the settle frames, classify by channel saturation (colour vs IR — already validated,
   it separated all 51 capture files correctly), resolution, and similarity to the stored
   per-slot reference frame that `startup_snapshot` writes every boot.
2. **Prefer the SDK serial** (§1 above) whenever a slot runs the `realsense` driver.
3. An unresolvable slot is **unavailable with a stated reason** — never a silent substitution.

The config schema for `identity` / `resolution` / `servo_cameras` is frozen in Wave 0's
`core/config.py`; S3 fills in the resolution logic behind it.
