"""W1: the twin is safe under concurrent fusion writes + projection/API reads.

Without the RLock in WorldModel this raises `RuntimeError: dictionary changed size
during iteration` (readers iterate entities while a writer adds) and can compose
half-updated transforms. With it, readers and writers interleave cleanly.
"""
from __future__ import annotations

import threading

from core.worldmodel import Entity, EntityKind, build_skeleton, from_xyz_rpy


def test_concurrent_writers_and_readers_do_not_crash():
    wm = build_skeleton()
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        i = 0
        try:
            while not stop.is_set():
                i += 1
                wm.add(Entity(f"probe_{i % 50}", EntityKind.TUBE, parent="world"))
                wm.set_world_pose("overview_cam", from_xyz_rpy(x=i * 1e-4))
        except BaseException as e:  # noqa: BLE001 - surface any race
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                wm.snapshot()            # iterates + composes every entity
                wm.by_kind(EntityKind.TUBE)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(2)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    threading.Event().wait(0.4)          # let them race for a bit
    stop.set()
    for t in threads:
        t.join(2.0)

    assert not errors, f"twin race: {errors[:3]}"


def test_lock_is_reentrant_for_atomic_batches():
    wm = build_skeleton()
    # nested acquisition (batch) must not deadlock, and inner ops still work
    with wm.lock:
        wm.set_world_pose("overview_cam", from_xyz_rpy(z=0.3))
        assert wm.by_kind(EntityKind.CAMERA)          # re-acquires the same RLock
        wm.reparent("tube_1", "world") if "tube_1" in wm.entities else None
