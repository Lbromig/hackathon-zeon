"""The blackboard — **FROZEN at the end of Wave 0**. Exactly five named slots, and nothing else.

`frame`, `tip`, `tube`, `offset`, `selected_offset`. That is the whole vocabulary an action
can use to pass a value to a later action.

**What this deliberately is not** (review S2, S3):

* **No generic reference resolver.** The design draft had `Ref(slot, field)` where `field` was
  "an optional dotted path inside the slot value". That is an interpreter — and an interpreter
  is a thing you debug during bring-up, at the bench, while an arm is holding an open tube.
  Here an action names one of five `Literal` slot names and the runner does one dict lookup.
* **No dotted-path navigation.** A slot holds one typed value. If an action needs a field of
  it, the *handler* reads that field in Python, where a typo is a `AttributeError` at
  development time rather than a `None` at runtime.
* **No condition expression language.** `control.loop` has one named termination predicate
  with a threshold. An expression language is also the part an LLM is most likely to author
  wrongly, and a wrongly authored expression fails in a way nobody can read.

The five are enough because the workflow's only data flow is the servo loop: snapshot ->
identify tip -> identify tube -> solve offset -> move the liquid handler by it. Anything more
general is scope that has to be maintained and cannot be tested against a real use.

Per-camera keying: `frame`, `tip` and `tube` are naturally per-camera — the loop reads two
views — so those slots are keyed by device id underneath. That is not a second addressing
scheme; it is the same five names, with the camera the value came from. `offset` and
`selected_offset` are single-valued, since the solve is one weighted solve across the views
(D15: there is no best-single-view selection).
"""
from __future__ import annotations

import threading
from typing import Any, Iterator, Literal

SlotName = Literal["frame", "tip", "tube", "offset", "selected_offset"]

SLOT_NAMES: tuple[SlotName, ...] = ("frame", "tip", "tube", "offset", "selected_offset")

#: The three slots that hold one value per camera. Keyed by device id inside the slot.
PER_CAMERA_SLOTS: frozenset[str] = frozenset({"frame", "tip", "tube"})


class SlotEmpty(LookupError):
    """A slot was read before anything wrote it.

    A distinct exception rather than `None`, because the two mean different things to a servo
    loop: "no tip detection yet" and "the tip was looked for and not found" must not be the
    same value. `IdentifyOutputs.found is False` is the second one, and it is a written value.
    """


class Blackboard:
    """Five named slots, thread-safe.

    Thread-safe because the runner thread writes while the API thread may read a snapshot for
    the UI (D1: one worker thread, asyncio for fan-out). A plain dict here would let a
    snapshot see a half-updated loop iteration.

    `set`/`get` raise on an unknown slot name rather than creating one. That is the property
    that keeps this from growing into the generic store S2 cut: adding a sixth slot has to be
    a contract change with a reason, not a typo that works.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._single: dict[str, Any] = {}
        self._per_camera: dict[str, dict[str, Any]] = {s: {} for s in PER_CAMERA_SLOTS}

    # --- writing -------------------------------------------------------------
    def set(self, slot: str, value: Any, *, device: str | None = None) -> None:
        """Write a slot. `device` is required for the per-camera slots.

        Required rather than defaulted: a `frame` written with no camera would be readable as
        any camera's frame, which is precisely the mix-up that makes a two-view offset solve
        wrong in a way that still looks plausible.
        """
        self._check(slot)
        with self._lock:
            if slot in PER_CAMERA_SLOTS:
                if not device:
                    raise ValueError(f"slot {slot!r} is per-camera; a device id is required")
                self._per_camera[slot][device] = value
            else:
                if device:
                    raise ValueError(f"slot {slot!r} is single-valued; it takes no device id")
                self._single[slot] = value

    # --- reading -------------------------------------------------------------
    def get(self, slot: str, *, device: str | None = None) -> Any:
        """Read a slot, raising `SlotEmpty` if nothing has been written to it."""
        self._check(slot)
        with self._lock:
            if slot in PER_CAMERA_SLOTS:
                if not device:
                    raise ValueError(f"slot {slot!r} is per-camera; a device id is required")
                store = self._per_camera[slot]
                if device not in store:
                    raise SlotEmpty(f"nothing in slot {slot!r} for {device!r}")
                return store[device]
            if slot not in self._single:
                raise SlotEmpty(f"nothing in slot {slot!r}")
            return self._single[slot]

    def peek(self, slot: str, *, device: str | None = None, default: Any = None) -> Any:
        """`get` with a default, for the places where absence is an expected branch."""
        try:
            return self.get(slot, device=device)
        except SlotEmpty:
            return default

    def has(self, slot: str, *, device: str | None = None) -> bool:
        try:
            self.get(slot, device=device)
            return True
        except SlotEmpty:
            return False

    def devices(self, slot: str) -> tuple[str, ...]:
        """Which cameras have written to a per-camera slot. This is how the offset solve
        discovers which views it actually has, rather than assuming both are present."""
        self._check(slot)
        if slot not in PER_CAMERA_SLOTS:
            raise ValueError(f"slot {slot!r} is single-valued")
        with self._lock:
            return tuple(sorted(self._per_camera[slot]))

    # --- lifecycle -----------------------------------------------------------
    def clear(self, slot: str | None = None) -> None:
        """Clear one slot or all of them.

        The caller that matters is `Runner._clear_iteration_slots`: every loop iteration clears
        `frame`, `tip`, `tube` and the loop's `watch_slot` as it is materialized, so iteration 4
        cannot solve against iteration 3's detections if a capture fails — a stale detection
        silently reused is the same class of bug as a stale frame (D20), and just as hard to see.
        Before that call existed this docstring described an invariant nothing enforced, and the
        review reproduced the consequence: an aborted iteration-2 snapshot let iteration 2's
        solve complete against iteration 1's frame and the loop re-commanded an offset it had
        already applied.

        The other half of the contract belongs to the handlers: **write your slot on every
        outcome, including a refusal.** A `vision.solve_offset` that refuses and returns without
        writing used to leave the previous offset in place for the liquid handler to re-apply;
        now it leaves the slot empty, and `SlotEmpty` downstream is the loud, honest failure.
        """
        with self._lock:
            if slot is None:
                self._single.clear()
                for store in self._per_camera.values():
                    store.clear()
                return
            self._check(slot)
            if slot in PER_CAMERA_SLOTS:
                self._per_camera[slot].clear()
            else:
                self._single.pop(slot, None)

    def snapshot(self) -> dict[str, Any]:
        """A plain-dict copy, for the UI and the log. Shallow by design: the values are
        already immutable-ish typed outputs models, and deep-copying an image array into a log
        record is exactly what R-LOG-8 forbids."""
        with self._lock:
            out: dict[str, Any] = dict(self._single)
            for name, store in self._per_camera.items():
                if store:
                    out[name] = dict(store)
            return out

    def __iter__(self) -> Iterator[SlotName]:
        return iter(SLOT_NAMES)

    def __repr__(self) -> str:
        filled = sorted(self.snapshot())
        return f"<Blackboard filled={filled or '[]'}>"

    @staticmethod
    def _check(slot: str) -> None:
        if slot not in SLOT_NAMES:
            raise KeyError(
                f"{slot!r} is not a blackboard slot. There are exactly five: "
                f"{', '.join(SLOT_NAMES)}. Adding one is a contract change (review S2) — "
                f"do not create it here.")
