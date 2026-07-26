#!/usr/bin/env python3
"""Record synchronised footage from every camera slot, for training data.

Writes one JPEG per frame per camera plus a manifest, rather than an encoded video:

* frames stay individually addressable, which is what labelling tools want;
* no inter-frame compression, so a labelled frame is exactly the pixels the model sees;
* a dropped camera costs you that camera's frames, not a corrupt container.

Layout (under <capture_dir>/../recordings, gitignored):

    temp/recordings/20260726T0142Z/
      manifest.json                  # per-frame timestamps + camera metadata
      overview_cam/frame_000000.jpg
      handover_cam/frame_000000.jpg
      ...

Non-live slots are skipped by default. A `still` slot replays one saved frame forever, so
recording it would emit hundreds of byte-identical images — that is not training data, it
is one sample with a misleading count, and it will quietly bias whatever is trained on it.
`--include-still` overrides, and the manifest marks those cameras `live: false` either way.

Cameras are pulled round-robin from a single thread so the frames of one tick belong to the
same moment; a thread per camera would drift them apart with nothing recording the offset.

Usage:
    .venv/bin/python scripts/record_footage.py                     # 15 s at 10 fps
    .venv/bin/python scripts/record_footage.py --seconds 60 --fps 15
    .venv/bin/python scripts/record_footage.py --slots overview_cam handover_cam
    .venv/bin/python scripts/record_footage.py --quality 95        # bigger, less artefact

Note the frames capture whatever is in front of the bench, people included — worth knowing
before the set is shared or used to train something that gets distributed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

DEFAULT_SECONDS = 15.0
DEFAULT_FPS = 10.0
DEFAULT_QUALITY = 92
SETTLE_FRAMES = 5          # auto-exposure settles before the first recorded frame


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS)
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="JPEG quality")
    ap.add_argument("--slots", nargs="*", default=None,
                    help="fleet ids to record (default: every camera slot)")
    ap.add_argument("--include-still", action="store_true",
                    help="also record slots replaying a saved frame (see the caveat above)")
    ap.add_argument("--out", default=None, help="output directory (default temp/recordings)")
    args = ap.parse_args()

    import cv2

    from core.config import CAMERA_TYPES, Settings
    from drivers import build_driver

    settings = Settings.load()
    entries = [e for e in settings.fleet if e.get("type") in CAMERA_TYPES]
    if args.slots:
        entries = [e for e in entries if e.get("id") in args.slots]
        missing = set(args.slots) - {e.get("id") for e in entries}
        if missing:
            print(f"no such camera slot(s): {', '.join(sorted(missing))}")
            return 2
    if not entries:
        print("no camera slots to record")
        return 2

    root = args.out or os.path.join(os.path.dirname(settings.capture_dir), "recordings")
    session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = os.path.join(root, session)

    # --- open everything first -------------------------------------------------------
    # Nothing is recorded until every camera that is going to work is up, so the streams
    # start together and frame N means the same instant across cameras.
    opened: list[tuple[str, object, dict]] = []
    try:
        for e in entries:
            eid = e.get("id", "?")
            try:
                drv = build_driver(e)
                drv.connect()
            except Exception as exc:
                print(f"  {eid}: unavailable ({exc}) — skipped")
                continue
            live = bool(drv.info.meta.get("live", True))
            if not live and not args.include_still:
                print(f"  {eid}: replaying a saved frame, not live — skipped "
                      f"(use --include-still to force)")
                drv.disconnect()
                continue
            opened.append((eid, drv, {"live": live, "type": e.get("type"),
                                      "source": e.get("source")}))
            print(f"  {eid}: recording ({e.get('type')})")

        if not opened:
            print("no live cameras to record")
            return 3

        for eid, _drv, _meta in opened:
            os.makedirs(os.path.join(out_dir, eid), exist_ok=True)

        for _ in range(SETTLE_FRAMES):
            for _eid, drv, _meta in opened:
                try:
                    drv.capture()
                except Exception:
                    pass

        # --- record ------------------------------------------------------------------
        period = 1.0 / max(0.1, args.fps)
        deadline = time.time() + args.seconds
        frames: dict[str, list[dict]] = {eid: [] for eid, _d, _m in opened}
        dropped: dict[str, int] = {eid: 0 for eid, _d, _m in opened}
        encode = [int(cv2.IMWRITE_JPEG_QUALITY), int(args.quality)]
        index = 0
        started = time.time()

        print(f"\nrecording {args.seconds:g}s at {args.fps:g} fps -> {out_dir}")
        while time.time() < deadline:
            tick = time.time()
            for eid, drv, _meta in opened:
                try:
                    frame = drv.capture()
                except Exception:
                    dropped[eid] += 1
                    continue
                name = f"frame_{index:06d}.jpg"
                cv2.imwrite(os.path.join(out_dir, eid, name), frame, encode)
                frames[eid].append({"file": name, "t": round(time.time() - started, 4)})
            index += 1
            # Sleep only the remainder: encoding time is part of the period, so a slow
            # disk lowers the achieved rate rather than silently skewing the timestamps.
            time.sleep(max(0.0, period - (time.time() - tick)))

        elapsed = time.time() - started
    finally:
        for eid, drv, _meta in opened:
            try:
                drv.disconnect()
            except Exception:
                pass

    # --- manifest --------------------------------------------------------------------
    manifest = {
        "session": session,
        "started_utc": session,
        "seconds_requested": args.seconds,
        "seconds_elapsed": round(elapsed, 3),
        "fps_requested": args.fps,
        "jpeg_quality": args.quality,
        "cameras": {
            eid: {
                **meta,
                "frames": len(frames[eid]),
                "dropped": dropped[eid],
                "fps_actual": round(len(frames[eid]) / elapsed, 2) if elapsed else 0.0,
                # Recorded per camera because a slot's intrinsics decide whether its
                # pixels can be back-projected later; absent on the UVC path.
                "intrinsics": (drv.intrinsics() if hasattr(drv, "intrinsics") else None),
                "index": frames[eid],
            }
            for eid, drv, meta in opened
        },
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    total_bytes = sum(
        os.path.getsize(os.path.join(dirpath, f))
        for dirpath, _d, files in os.walk(out_dir) for f in files
    )
    print()
    for eid, _drv, _meta in opened:
        got, drop = len(frames[eid]), dropped[eid]
        rate = got / elapsed if elapsed else 0.0
        flag = "" if drop == 0 else f"  ({drop} dropped)"
        print(f"  {eid:<14} {got:>5} frames  {rate:5.1f} fps{flag}")
    print(f"\n{out_dir}\n{_human(total_bytes)} total, manifest.json alongside")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
