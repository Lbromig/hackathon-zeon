"""User-built action sequences — compose, pre-flight, step through, run.

Fleet-level rather than per-arm: a sequence may drive both arms (hand the tube from one
to the other), so it cannot live under ``/api/arms/{id}``.

Two ways to execute, sharing one code path:

* ``/step`` runs a single step, so the operator can walk through a new sequence one
  action at a time and watch what the arm actually does.
* ``/run`` runs the lot and returns every event.

Both take the per-arm command lock for the arm they touch, so a sequence can never race
the teach panel's jog buttons.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from core import sequences

from ..schemas import (
    SequenceModel,
    SequenceRunResult,
    SequenceStepModel,
    SequenceStepResult,
)
from ..services.device_manager import device_manager
from .teach import _arm, _command, _require_movable

router = APIRouter(prefix="/api/sequences", tags=["sequences"])


def _to_core(model: SequenceModel) -> sequences.Sequence:
    return sequences.Sequence(
        name=model.name,
        steps=[sequences.Step(**s.model_dump()) for s in model.steps],
        note=model.note,
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _to_model(seq: sequences.Sequence) -> SequenceModel:
    return SequenceModel(
        name=seq.name,
        steps=[SequenceStepModel(**vars(s)) for s in seq.steps],
        note=seq.note,
        updated_at=seq.updated_at,
    )


def _arms_for(seq: sequences.Sequence) -> dict:
    """Live drivers for every device the sequence mentions, skipping unknown ones so
    pre-flight can report them as problems rather than raising."""
    out = {}
    for step in seq.steps:
        if step.device in out:
            continue
        try:
            out[step.device] = device_manager.get(step.device)
        except KeyError:
            pass
    return out


@router.get("", response_model=list[SequenceModel])
def list_sequences() -> list[SequenceModel]:
    return [_to_model(sequences.get(n)) for n in sorted(sequences.load())]


@router.get("/{name}", response_model=SequenceModel)
def get_sequence(name: str) -> SequenceModel:
    try:
        return _to_model(sequences.get(name))
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.put("/{name}", response_model=SequenceModel)
def save_sequence(name: str, body: SequenceModel) -> SequenceModel:
    """Create or replace. The whole sequence is sent each time — an editor that PATCHes
    individual steps has to solve ordering conflicts, and this does not need to."""
    body.name = name
    seq = _to_core(body)
    try:
        sequences.save(seq)
    except sequences.SequenceError as e:
        raise HTTPException(400, str(e))
    return _to_model(seq)


@router.delete("/{name}", response_model=list[SequenceModel])
def delete_sequence(name: str) -> list[SequenceModel]:
    if not sequences.delete(name):
        raise HTTPException(404, f"no sequence {name!r}")
    return list_sequences()


@router.post("/{name}/preflight", response_model=SequenceRunResult)
def preflight_sequence(name: str) -> SequenceRunResult:
    """Static checks over the whole sequence — poses taught, devices present, joint
    targets inside the soft limits. Nothing moves."""
    try:
        seq = sequences.get(name)
    except LookupError as e:
        raise HTTPException(404, str(e))
    problems = sequences.preflight(seq, _arms_for(seq))
    return SequenceRunResult(
        ok=not problems, problems=problems,
        events=[SequenceStepResult(index=0, phase="preflight", ok=not problems,
                                   detail="; ".join(problems) or f"{len(seq.steps)} steps ready")],
    )


@router.post("/{name}/step", response_model=SequenceRunResult)
def run_one_step(name: str, index: int) -> SequenceRunResult:
    """Run a single step, 1-based. This is the walkthrough.

    Deliberately does NOT pre-flight the whole sequence: stepping through is how you
    build one, and half-finished sequences are expected to have gaps further down.
    """
    try:
        seq = sequences.get(name)
    except LookupError as e:
        raise HTTPException(404, str(e))
    if not 1 <= index <= len(seq.steps):
        raise HTTPException(400, f"step {index} out of range (1..{len(seq.steps)})")

    step = seq.steps[index - 1]
    _arm(step.device)                      # 404s if it is not an arm in the fleet

    def action(arm) -> str:
        _require_movable(arm)
        return sequences.run_step(step, arm)

    result = _command(step.device, action)
    return SequenceRunResult(
        ok=result.ok,
        events=[SequenceStepResult(index=index, phase="done" if result.ok else "failed",
                                   ok=result.ok, detail=result.detail)],
    )


@router.post("/{name}/run", response_model=SequenceRunResult)
def run_sequence(name: str, skip_preflight: bool = False) -> SequenceRunResult:
    """Run every step in order, stopping at the first failure.

    Each step takes its arm's command lock individually rather than holding both for the
    whole run: a sequence that grabbed every lock up front would block the e-stop's
    sibling endpoints for its entire duration.
    """
    try:
        seq = sequences.get(name)
    except LookupError as e:
        raise HTTPException(404, str(e))

    arms = _arms_for(seq)
    if not skip_preflight:
        problems = sequences.preflight(seq, arms)
        if problems:
            return SequenceRunResult(
                ok=False, problems=problems,
                events=[SequenceStepResult(index=0, phase="preflight", ok=False,
                                           detail="; ".join(problems))],
            )

    events: list[SequenceStepResult] = [
        SequenceStepResult(index=0, phase="preflight", ok=True,
                           detail=f"{len(seq.steps)} steps ready")
    ]
    for i, step in enumerate(seq.steps, start=1):
        def action(arm, _step=step) -> str:
            _require_movable(arm)
            return sequences.run_step(_step, arm)

        result = _command(step.device, action)
        events.append(SequenceStepResult(
            index=i, phase="done" if result.ok else "failed",
            ok=result.ok, detail=f"{step.describe()} — {result.detail}",
        ))
        if not result.ok:
            return SequenceRunResult(ok=False, events=events)

    events.append(SequenceStepResult(index=len(seq.steps), phase="complete", ok=True,
                                     detail=f"ran {len(seq.steps)} steps"))
    return SequenceRunResult(ok=True, events=events)
