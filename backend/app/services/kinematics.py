"""Background loop that writes each arm's live TCP into the twin (W3).

Reads `ArmDriver.get_pose()` for every connected arm and folds it into the twin via
core.kinematics.update_arm_tcp, so `{arm}_tcp` / `{arm}_tool` / `gripper_cam` track the real
arm. Kinematics is the fast always-on pose source (fusion is the slower corrective one).

Runs modestly (default 12 Hz): get_pose is a blocking SDK socket read, so this is deliberately
not maxed out. No-ops cleanly until calibration has published a twin and the arms are connected.
"""
from __future__ import annotations

import threading

from core.kinematics import update_arm_tcp
from drivers import ConnectionState, InstrumentKind

from . import twin
from .device_manager import device_manager

KIN_HZ = 12.0


def _arm_ids() -> list[str]:
    return [d.device_id for d in device_manager.all() if d.info.kind == InstrumentKind.ARM]


def kinematics_once() -> int:
    """Update every connected arm's TCP in the twin. Returns how many updated."""
    wm = twin.get_world()
    if wm is None:
        return 0
    n = 0
    for arm_id in _arm_ids():
        try:
            drv = device_manager.get(arm_id)
            if drv.state != ConnectionState.CONNECTED:
                continue
            pose = drv.get_pose()
        except Exception:
            continue                       # a flaky read must not stall the loop
        if update_arm_tcp(wm, arm_id, pose):
            n += 1
    return n


class Kinematics:
    def __init__(self, hz: float = KIN_HZ) -> None:
        self.period = 1.0 / max(1.0, hz)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kinematics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                kinematics_once()
            except Exception as e:  # never let FK kill the app
                print(f"[kinematics] {e}")
            self._stop.wait(self.period)


kinematics = Kinematics()
