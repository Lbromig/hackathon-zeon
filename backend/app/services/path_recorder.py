"""Samples an arm's joints while an operator hand-guides it along a path.

Server-side rather than in the browser: a background tab gets throttled to ~1 Hz by the
browser, which would quietly shred the middle of a recording and leave the operator with
a path that cuts corners. Sampling here is unaffected by what the UI is doing.

Reading joints does not command motion, so this deliberately does NOT take the per-arm
command lock — the operator is moving the arm by hand, and blocking their /free_drive
toggle behind a recorder would be exactly wrong.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from drivers.capabilities.arm import ArmDriver

SAMPLE_HZ = 10.0
MAX_SAMPLES = 20_000            # ~33 min at 10 Hz; a runaway recorder must not eat RAM


@dataclass
class Recording:
    device_id: str
    name: str
    samples: list[list[float]] = field(default_factory=list)
    started_at: float = 0.0
    stopped: bool = False
    error: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class PathRecorder:
    """One recording at a time, per arm."""

    def __init__(self) -> None:
        self._active: dict[str, Recording] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()

    def is_recording(self, device_id: str) -> bool:
        with self._guard:
            return device_id in self._active

    def active(self, device_id: str) -> Recording | None:
        with self._guard:
            return self._active.get(device_id)

    def start(self, arm: ArmDriver, name: str) -> Recording:
        with self._guard:
            if arm.device_id in self._active:
                raise RuntimeError(
                    f"{arm.device_id} is already recording {self._active[arm.device_id].name!r}"
                )
            rec = Recording(device_id=arm.device_id, name=name,
                            started_at=time.monotonic())
            self._active[arm.device_id] = rec

        t = threading.Thread(target=self._loop, args=(arm, rec), daemon=True,
                             name=f"path-record-{arm.device_id}")
        self._threads[arm.device_id] = t
        t.start()
        return rec

    def stop(self, device_id: str) -> Recording:
        with self._guard:
            rec = self._active.pop(device_id, None)
        if rec is None:
            raise RuntimeError(f"{device_id} is not recording")
        rec.stopped = True
        t = self._threads.pop(device_id, None)
        if t is not None:
            t.join(timeout=2.0)     # the loop checks `stopped` every sample period
        return rec

    def _loop(self, arm: ArmDriver, rec: Recording) -> None:
        period = 1.0 / SAMPLE_HZ
        consecutive_failures = 0
        while not rec.stopped:
            began = time.monotonic()
            try:
                joints = arm.get_joints()
                if len(rec.samples) < MAX_SAMPLES:
                    rec.samples.append([float(j) for j in joints])
                consecutive_failures = 0
            except Exception as e:
                # A dropped read mid-recording is survivable; a dead connection is not.
                consecutive_failures += 1
                if consecutive_failures >= 10:
                    rec.error = f"lost the arm while recording: {e}"
                    rec.stopped = True
                    break
            time.sleep(max(0.0, period - (time.monotonic() - began)))


path_recorder = PathRecorder()
