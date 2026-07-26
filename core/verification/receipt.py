"""Sealed run receipt: the verdict, plus enough provenance to argue with it.

The claim this project makes is that a verification verdict can be trusted. A
verdict nobody can audit afterwards is not a trustworthy verdict, it is a green
tick. So a run gets a receipt: which steps were verified, on what evidence, how
strong that evidence was allowed to claim to be, what actually happened (counted,
not asserted), and a SHA-256 seal over the whole thing so a later edit shows up.

Five rules hold here. Each one is stated because the obvious version of it is
wrong in a way that is not obvious until it bites.

**The digest cannot cover itself.** It is taken over the payload with the
integrity envelope removed. Hashing the whole receipt including the envelope
would mean a verifier has to guess what the envelope looked like before the
digest was written into it, and "does this receipt still match its seal" stops
being a well-defined question. ``seal`` and ``verify`` both hash the same bytes:
everything except ``integrity``.

**Nothing in the sealing path reads the clock.** A wall-clock read anywhere under
``seal`` would mean the same run seals to a different digest every time, which
destroys the only property that makes a digest worth having. Same for a random
value or an object address. A timestamp is caller-supplied evidence like any
other, so it is passed in and recorded as an assertion this module cannot check.
There is a test that greps this file for the usual offenders, because a clock
read does not fail today, it fails tomorrow.

**A tier is provenance, not a word the caller picked.** Every claimed tier is
clamped against a ceiling that lives in this file's source, so the strongest
thing a caller can do is claim less than the ceiling. Raising a ceiling is a
reviewed source edit that cites the bench record which earned it. A tier a caller
can raise at runtime is not provenance, it is a string in a dict.

**A conclusion is only as strong as its weakest input.** Hence minimum, not
average and not maximum. One modeled input in a fused verdict makes the verdict
modeled, because that is the part an auditor would attack first.

**str() is not a serialiser.** Coercing an arbitrary object into the payload
looks harmless and breaks determinism outright: the default repr embeds the
object's id, so the same run seals to a different digest on the next process. A
value that is not a JSON primitive is refused by name instead.

Pure standard library on purpose. A receipt has to be verifiable by someone who
has none of this rig, no numpy and no camera, on a laptop, months later.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

RECEIPT_VERSION = 1
KNOWN_RECEIPT_VERSIONS = frozenset({1})

DIGEST_ALGORITHM = "sha256"
INTEGRITY_KEY = "integrity"

# The top-level keys seal() always writes. verify() requires all of them, because
# an intact seal over a payload with no steps and no counts is a green tick over
# nothing: a truncated file, or a payload built by hand, used to come back "seal
# intact" while carrying no verdicts at all. A digest proves the bytes did not
# change. It does not prove the bytes say anything. Presence is checked, not
# absence of anything else, so a later version may still add keys.
SEALED_KEYS: tuple[str, ...] = (
    "receipt_version",
    "asserted",
    "rules",
    "counts",
    "evidence_tier",
    "steps",
)

# The exact serialisation the digest covers, recorded inside the receipt because
# a digest means nothing without the byte sequence it was taken over. It is a
# constant so that verify() can refuse a receipt claiming some other form rather
# than silently re-hashing under different rules.
CANONICAL_FORM = (
    "json;sort_keys=True;separators=(',',':');ensure_ascii=True;"
    "allow_nan=False;integrity-key-removed"
)


class ReceiptError(ValueError):
    """A run that cannot be sealed honestly.

    Subclasses ValueError so existing ``except ValueError`` handling still
    catches it. Raising is deliberate: quietly repairing a malformed record would
    seal a receipt that describes something other than the run.
    """


class Tier(str, Enum):
    """How strong one piece of evidence is allowed to claim to be.

    Four values, ordered weakest to strongest. The distinction that matters most
    is SIMULATED against MEASURED: a fabricated telemetry dict and a live read off
    the controller are the same shape and this module cannot tell them apart, so
    the difference has to be attested by whoever ran the cell and then clamped
    against what the project can actually back up.
    """

    MODELED = "modeled"
    SIMULATED = "simulated"
    MEASURED = "measured"
    HARDWARE_VALIDATED = "hardware-validated"


# Weakest first. Only used through min_tier / _weaker so the ordering lives in
# exactly one place.
TIER_ORDER: tuple[Tier, ...] = (
    Tier.MODELED,
    Tier.SIMULATED,
    Tier.MEASURED,
    Tier.HARDWARE_VALIDATED,
)
_TIER_RANK: dict[Tier, int] = {t: i for i, t in enumerate(TIER_ORDER)}
_TIER_BY_VALUE: dict[str, Tier] = {t.value: t for t in TIER_ORDER}

# Strongest tier each named evidence input may claim in a receipt sealed here.
#
# Every entry is SIMULATED because that is what this repo can defend today: the
# depth channel is exercised against synthetic numpy frames, the vision channel
# needs an OpenCV that is not installed, and the torque channel's thresholds
# (MIN_UNSCREW_TORQUE_NM, DROP_LO, DROP_HI in agents.py) carry no bench
# measurement. None of that is a criticism of the channels, it is the honest
# state of their provenance.
#
# HARDWARE_VALIDATED is in the vocabulary and unreachable through seal(). It
# should stay unreachable until a ceiling below is raised by an edit that names
# the run on the physical cell it came from. TUNABLE only in that sense: these
# are provenance claims, not tolerances, and no bench measurement backs raising
# any of them right now.
INPUT_TIER_CEILING: dict[str, Tier] = {
    "torque": Tier.SIMULATED,
    "vision": Tier.SIMULATED,
    "depth": Tier.SIMULATED,
}

# An input nobody has thought about hard enough to put in the table above is
# modeled. Fail closed: the default cannot be the interesting answer.
DEFAULT_TIER_CEILING = Tier.MODELED

TIER_RULE = (
    "A conclusion tier is the weakest of its input tiers. A claimed tier is "
    "clamped to the ceiling in INPUT_TIER_CEILING, which lives in source and "
    "cannot be raised by a caller. An input with no claim, or a claim outside "
    "the four-word vocabulary, is modeled. A verification with no attested "
    "inputs is modeled rather than unrated."
)

RETRY_RULE = (
    "A retry is a verification-bearing record whose attempt is above 1, counted "
    "once per (step, attempt) pair so that re-submitting the same record does "
    "not inflate the count. Records carrying no verification are not counted "
    "here, because a 'started' and a 'passed' event for the same attempt would "
    "otherwise count that attempt twice."
)

ESCALATION_RULE = (
    "An escalation is a verification-bearing record where the run can no longer "
    "proceed on its own: phase 'failed' (retry budget spent), phase 'escalated' "
    "(the orchestrator stopped rather than retried), or a 'disagreement' block "
    "in the verification data (independent channels contradict, so there is no "
    "known state to retry from). Counted once per record, so a record carrying "
    "more than one reason still counts as one escalation. Recording the reasons "
    "separately matters because a spent retry budget and a contradiction call "
    "for different things from the human who reads this."
)

CHANNEL_RULE = (
    "A channel counts as contributing when it carried a numeric confidence in "
    "verification data['channels']. A channel that abstained is absent from that "
    "dict and is not counted, which is the point of abstaining."
)

ASSERTED_NOTE = (
    "Declared by the caller and not checked by this module. seal() counts only "
    "the records it was handed, so run completeness is asserted by construction: "
    "a record dropped before sealing is invisible here. run_id and sealed_at are "
    "caller strings with no authority behind them."
)

# Phases that mean the run stopped rather than continued. These track the phase
# vocabulary uncap_aspirate.run yields; a phase this module does not know is
# recorded verbatim and counted as nothing, which is the fail-closed direction
# for a count but does mean a new terminal phase has to be added here.
_ESCALATION_BUDGET = "retry_budget_exhausted"
_ESCALATION_STOPPED = "orchestrator_escalated"
_ESCALATION_DISAGREEMENT = "channel_disagreement"

_ESCALATION_PHASES: dict[str, str] = {
    "failed": _ESCALATION_BUDGET,
    "escalated": _ESCALATION_STOPPED,
}

_MISSING = object()


# --- tier arithmetic -------------------------------------------------------

def parse_tier(value: Any) -> Tier | None:
    """A Tier from a caller-supplied value, or None if it is not one of the four.

    None rather than a default, so the caller of this helper has to decide what
    an unrecognised word means. Inside ``seal`` it means modeled and gets
    recorded as a rejected claim: a typo must never read as a stronger tier, and
    it must not vanish either.
    """
    if isinstance(value, Tier):
        return value
    if isinstance(value, str):
        return _TIER_BY_VALUE.get(value)
    return None


def min_tier(tiers: Iterable[Tier]) -> Tier:
    """The weakest of ``tiers``. Empty means modeled.

    Minimum is the whole point. Averaging tiers would let two strong inputs carry
    a weak one, which is exactly the reasoning an audit exists to catch, and an
    empty set of inputs is the weakest case of all rather than an unrated one.
    """
    weakest = Tier.HARDWARE_VALIDATED
    seen = False
    for tier in tiers:
        seen = True
        if _TIER_RANK[tier] < _TIER_RANK[weakest]:
            weakest = tier
    return weakest if seen else Tier.MODELED


def ceiling_for(input_name: str) -> Tier:
    """Strongest tier ``input_name`` may claim. Unlisted inputs are modeled."""
    return INPUT_TIER_CEILING.get(input_name, DEFAULT_TIER_CEILING)


def _grant(input_name: str, claimed: Tier) -> Tier:
    """Clamp a claim to its ceiling. This is the only way a tier is issued."""
    return min_tier((claimed, ceiling_for(input_name)))


def _claim_label(input_name: str, claimed: Any) -> str:
    """A note that a claim was refused, in a form that is safe to seal.

    ``repr()`` is out here for the same reason ``str()`` is not a serialiser: the
    default repr of an arbitrary object carries its address. This was a live bug,
    not a hypothetical one. Rejected claims are the one place a caller-supplied
    value gets written into the payload instead of raising, so formatting them
    with ``!r`` put a memory address in the receipt and the same run sealed to a
    different digest on the next process.

    A string claim is recorded verbatim, since only its content can vary.
    ``str.__repr__`` rather than ``repr`` so a str subclass cannot override the
    formatting and smuggle an address back in. Anything else is recorded by type
    name, which is all an auditor needs in order to see that something other than
    a tier arrived.
    """
    if isinstance(claimed, str):
        return f"{input_name}={str.__repr__(claimed)}"
    return f"{input_name}=<{type(claimed).__name__}>"


# --- canonical serialisation ----------------------------------------------

def _plain(value: Any, path: str) -> Any:
    """A JSON-primitive copy of ``value``, or a ReceiptError naming the offender.

    Refusing beats coercing. ``str()`` on an unexpected object embeds its id in
    the payload, so the digest would change between processes for an identical
    run, and the receipt would carry a memory address as if it were evidence.
    """
    # Enum first: Tier and depth_height.Verdict are str subclasses, so the
    # str check below would otherwise pass the enum member straight through and
    # leave an enum object sitting in a receipt that is meant to be plain data.
    if isinstance(value, Enum):
        return _plain(value.value, path)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise ReceiptError(
                f"{path} is {value!r}: a receipt cannot carry NaN or infinity, "
                "because neither survives a canonical serialisation and both "
                "would make the digest undefined"
            )
        return number
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReceiptError(
                    f"{path}: key {key!r} is not a string, so it has no stable "
                    "canonical ordering"
                )
            out[key] = _plain(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_plain(item, f"{path}[{i}]") for i, item in enumerate(value)]
    raise ReceiptError(
        f"{path} is {type(value).__name__}, which is not a JSON primitive. "
        "Convert it at the call site: stringifying it here would embed an "
        "object id and the same run would stop sealing to the same digest"
    )


def canonical(payload: Mapping[str, Any]) -> str:
    """The exact text the digest is taken over.

    Sorted keys, no whitespace, ASCII-escaped, NaN refused. Key order in a Python
    dict is not part of what a receipt says, so sorting makes two receipts with
    the same content hash the same; whitespace and unicode escaping are pinned so
    the bytes do not depend on the writer's json defaults.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def digest_of(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical payload with the integrity envelope removed."""
    body = {k: v for k, v in payload.items() if k != INTEGRITY_KEY}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


# --- reading step records --------------------------------------------------

def _get(record: Any, name: str, default: Any = None) -> Any:
    """Field lookup that works on a mapping or on an object with attributes.

    The workflow yields event dicts and the agents return a VerificationResult
    dataclass. Duck typing here keeps this module free of any project import, so
    receipt.py can be lifted out as one file and still check a receipt on a
    machine where the rest of the stack does not import.

    The limit of that, stated because the file alone does not deliver it:
    importing this as ``core.verification.receipt`` does pull agents.py in, since
    the package ``__init__`` re-exports it. The independence is a property of the
    file, not of that import path.
    """
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _verification_of(record: Any) -> Any | None:
    """The verification block of a record, nested or flat, or None if absent."""
    nested = _get(record, "verification", _MISSING)
    if nested is not _MISSING and nested is not None:
        return nested
    if _get(record, "ok", _MISSING) is not _MISSING:
        return record
    return None


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptError(f"{path} must be a non-empty string, got {value!r}")
    return value


def _attempt_of(record: Any, path: str) -> int:
    attempt = _get(record, "attempt", _MISSING)
    if attempt is _MISSING or attempt is None:
        raise ReceiptError(
            f"{path}.attempt is missing. Without it a retry cannot be counted, "
            "and a receipt must not guess at a count"
        )
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ReceiptError(f"{path}.attempt must be an int, got {attempt!r}")
    if attempt < 1:
        raise ReceiptError(f"{path}.attempt must be 1 or greater, got {attempt}")
    return attempt


def _confidence_of(verification: Any, path: str) -> float:
    raw = _get(verification, "confidence", _MISSING)
    if raw is _MISSING or raw is None:
        raise ReceiptError(f"{path}.confidence is missing")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ReceiptError(f"{path}.confidence must be a number, got {raw!r}")
    return _plain(float(raw), f"{path}.confidence")


def _numeric_channels(data: Mapping[str, Any] | Any) -> dict[str, float]:
    """The channels that carried a numeric confidence, in the order recorded."""
    channels = _get(data, "channels") if data is not None else None
    if not isinstance(channels, Mapping):
        return {}
    out: dict[str, float] = {}
    for name, value in channels.items():
        if isinstance(name, str) and not isinstance(value, bool) and isinstance(value, (int, float)):
            out[name] = float(value)
    return out


# --- sealing ---------------------------------------------------------------

def _evidence_block(
    channels: Mapping[str, float],
    claims: Any,
    path: str,
) -> dict[str, Any]:
    """Per-verification tier block: what was claimed, what was granted, why.

    Inputs are the channels that contributed, plus any extra input the caller
    named in its claims. Naming an extra input is allowed because agents like
    ``aspiration_ok`` publish no channel breakdown, and recording "the OT volume
    report, modeled" is more honest than recording nothing. It buys the caller
    nothing: an unlisted name's ceiling is modeled, so declaring it can only pull
    the conclusion tier down.
    """
    claim_map: dict[str, Any] = {}
    if isinstance(claims, Mapping):
        claim_map = {k: v for k, v in claims.items() if isinstance(k, str)}
    elif claims is not None:
        raise ReceiptError(
            f"{path} must be a mapping of input name to tier, got {claims!r}"
        )

    names = list(channels)
    names += [n for n in claim_map if n not in channels]

    inputs: dict[str, Any] = {}
    rejected: list[str] = []
    for name in names:
        claimed_raw = claim_map.get(name, _MISSING)
        if claimed_raw is _MISSING:
            claimed = Tier.MODELED
            claimed_label: Any = None
        else:
            parsed = parse_tier(claimed_raw)
            if parsed is None:
                # A word outside the vocabulary is not a stronger tier and not a
                # silent nothing. It becomes modeled and stays visible.
                claimed = Tier.MODELED
                claimed_label = Tier.MODELED.value
                rejected.append(_claim_label(name, claimed_raw))
            else:
                claimed = parsed
                claimed_label = parsed.value
        granted = _grant(name, claimed)
        inputs[name] = {
            "source": "channel" if name in channels else "declared",
            "claimed": claimed_label,
            "ceiling": ceiling_for(name).value,
            "granted": granted.value,
        }

    block: dict[str, Any] = {
        "tier": min_tier(
            _TIER_BY_VALUE[info["granted"]] for info in inputs.values()
        ).value,
        "inputs": inputs,
    }
    if rejected:
        block["rejected_claims"] = sorted(rejected)
    return block


def seal(
    steps: Sequence[Any],
    *,
    run_id: str,
    sealed_at: str | None,
) -> dict[str, Any]:
    """Build and seal a receipt for one run.

    ``steps`` is the run in execution order: each record carries the step key,
    the attempt number, the phase reached, and the VerificationResult fields
    (``ok``, ``confidence``, ``detail``, ``data``), either nested under
    ``verification`` (the shape the workflow already yields) or flat on the
    record. A VerificationResult dataclass is accepted directly.

    Per-input tier claims go in the record's ``evidence_tiers`` mapping. They are
    claims: each one is clamped to its source-level ceiling before it is written.

    ``sealed_at`` is a caller-supplied string, or None for "no time claimed". It
    is an argument rather than a clock read so the same run always seals to the
    same digest. Callers who want wall-clock time in the receipt pass it in and
    accept that this module cannot vouch for it.

    Raises ReceiptError rather than sealing a receipt that describes something
    other than what it was handed.
    """
    _require_str(run_id, "run_id")
    if sealed_at is not None and not isinstance(sealed_at, str):
        raise ReceiptError(
            f"sealed_at must be a string or None, got {sealed_at!r}. It is "
            "recorded verbatim and never parsed, so no format is imposed"
        )
    if isinstance(steps, (str, bytes, Mapping)):
        raise ReceiptError("steps must be an ordered sequence of step records")

    entries: list[dict[str, Any]] = []
    tiers: list[Tier] = []

    retry_pairs: set[tuple[str, int]] = set()
    attempts_seen: dict[str, set[int]] = {}
    channel_hits: dict[str, int] = {}
    escalation_reasons: dict[str, int] = {
        _ESCALATION_BUDGET: 0,
        _ESCALATION_STOPPED: 0,
        _ESCALATION_DISAGREEMENT: 0,
    }
    verifications = 0
    unverified = 0
    passed = 0
    not_passed = 0
    escalations = 0

    for index, record in enumerate(steps):
        path = f"steps[{index}]"
        step_key = _require_str(_get(record, "step"), f"{path}.step")
        attempt = _attempt_of(record, path)
        phase = _get(record, "phase")
        if phase is not None and not isinstance(phase, str):
            raise ReceiptError(f"{path}.phase must be a string or absent, got {phase!r}")

        entry: dict[str, Any] = {
            "index": index,
            "step": step_key,
            "attempt": attempt,
            "phase": phase,
        }

        verification = _verification_of(record)
        if verification is None:
            # Kept in the receipt and counted separately. It is not a pass: a
            # record with no verdict is exactly the thing this layer exists to
            # stop reading as green.
            unverified += 1
            entry["verification"] = None
            entries.append(entry)
            continue

        verifications += 1
        attempts_seen.setdefault(step_key, set()).add(attempt)
        if attempt > 1:
            retry_pairs.add((step_key, attempt))

        # Only a real True is a pass. A truthy string is not a verdict.
        ok = _get(verification, "ok") is True
        if ok:
            passed += 1
        else:
            not_passed += 1

        detail = _get(verification, "detail", "")
        if detail is None:
            detail = ""
        if not isinstance(detail, str):
            raise ReceiptError(f"{path}.detail must be a string, got {detail!r}")

        data = _get(verification, "data")
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ReceiptError(f"{path}.data must be a mapping or absent, got {data!r}")
        channels = _numeric_channels(data)
        for name in channels:
            channel_hits[name] = channel_hits.get(name, 0) + 1

        disagreement = data.get("disagreement")
        reasons: list[str] = []
        by_phase = _ESCALATION_PHASES.get(phase or "")
        if by_phase is not None:
            reasons.append(by_phase)
        if disagreement is not None:
            reasons.append(_ESCALATION_DISAGREEMENT)
        if reasons:
            escalations += 1
            for reason in reasons:
                escalation_reasons[reason] += 1

        evidence = _evidence_block(
            channels, _get(record, "evidence_tiers"), f"{path}.evidence_tiers"
        )
        tiers.append(_TIER_BY_VALUE[evidence["tier"]])

        # data is carried whole, not filtered down to the channels. The numbers a
        # human would actually argue with live in there (torque_peak_nm,
        # depth_delta_mm, depth_valid_fraction), and a receipt that quietly drops
        # the evidence it was handed is not a receipt. The cost is that an
        # unserialisable value in data refuses the seal, which is the right way
        # round: better a loud refusal than a receipt missing the one field the
        # argument turns on.
        entry["verification"] = {
            "ok": ok,
            "confidence": _confidence_of(verification, path),
            "detail": detail,
            "data": _plain(data, f"{path}.data"),
        }
        entry["evidence"] = evidence
        entry["escalated"] = bool(reasons)
        if reasons:
            entry["escalation_reasons"] = sorted(reasons)
        entries.append(entry)

    payload: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        # run_id and sealed_at live under "asserted" and nowhere else. A second
        # unlabelled copy at the top level would be the one a reader trusts, and
        # neither copy is a fact this module can stand behind.
        "asserted": {"note": ASSERTED_NOTE, "run_id": run_id, "sealed_at": sealed_at},
        "rules": {
            "tier": TIER_RULE,
            "tier_ceiling": {name: tier.value for name, tier in INPUT_TIER_CEILING.items()},
            "tier_ceiling_default": DEFAULT_TIER_CEILING.value,
            "retry": RETRY_RULE,
            "escalation": ESCALATION_RULE,
            "channel": CHANNEL_RULE,
        },
        "counts": {
            "step_records": len(entries),
            "verifications": verifications,
            "records_without_verification": unverified,
            "passed": passed,
            "not_passed": not_passed,
            "retries": len(retry_pairs),
            "escalations": escalations,
            "escalation_reasons": dict(escalation_reasons),
            "attempts_per_step": {k: len(v) for k, v in sorted(attempts_seen.items())},
            "channel_contributions": dict(sorted(channel_hits.items())),
            "distinct_channels": len(channel_hits),
        },
        "evidence_tier": min_tier(tiers).value,
        "steps": entries,
    }

    payload = _plain(payload, "receipt")
    payload[INTEGRITY_KEY] = {
        "algorithm": DIGEST_ALGORITHM,
        "canonicalisation": CANONICAL_FORM,
        "digest": digest_of(payload),
    }
    return payload


# --- verifying -------------------------------------------------------------

@dataclass(frozen=True)
class Audit:
    """Outcome of checking a receipt against its seal.

    ``ok`` is False on any doubt, including a receipt whose envelope describes
    rules this module does not implement. A verifier that cannot tell what it is
    checking has not checked anything.
    """

    ok: bool
    detail: str


def _hexdigest_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def verify(receipt: Any) -> Audit:
    """Does ``receipt`` still match its own seal?

    Any change to any field outside the integrity envelope changes the canonical
    text and so the digest. Key order does not, on purpose: it is not part of
    what the receipt says.

    The envelope itself is checked by shape rather than by digest, because it is
    the one part the digest cannot cover. Without that check, downgrading
    ``algorithm`` or rewriting ``canonicalisation`` would be an undetectable way
    to make a tampered payload verify under weaker rules.

    The payload is checked for shape too, for a different reason: a digest says
    the bytes did not change, and says nothing about whether they mean anything.
    A receipt with no ``steps`` and no ``counts`` is not a verified run, so it is
    refused instead of reported intact.
    """
    if not isinstance(receipt, Mapping):
        return Audit(False, f"not a receipt: {type(receipt).__name__}")

    envelope = receipt.get(INTEGRITY_KEY)
    if not isinstance(envelope, Mapping):
        return Audit(False, "no integrity envelope, so there is no seal to check")

    version = receipt.get("receipt_version")
    # bool is an int in Python, so True == 1 and a receipt whose version reads
    # ``true`` would otherwise be accepted as a version 1 receipt.
    if isinstance(version, bool) or version not in KNOWN_RECEIPT_VERSIONS:
        return Audit(
            False,
            f"receipt_version {version!r} is not one this module implements "
            f"{sorted(KNOWN_RECEIPT_VERSIONS)}, so its seal cannot be checked",
        )
    if envelope.get("algorithm") != DIGEST_ALGORITHM:
        return Audit(
            False,
            f"digest algorithm {envelope.get('algorithm')!r} is not "
            f"{DIGEST_ALGORITHM!r}; refusing to verify under rules this module "
            "did not write",
        )
    if envelope.get("canonicalisation") != CANONICAL_FORM:
        return Audit(
            False,
            "canonicalisation does not match the one this module computes, so "
            "the digest would cover different bytes than it claims to",
        )

    claimed = envelope.get("digest")
    if not _hexdigest_ok(claimed):
        return Audit(False, f"digest {claimed!r} is not a sha256 hex digest")

    # Shape before digest. A seal only says the bytes are unchanged, so an intact
    # seal over a payload with nothing in it has to read as a refusal rather than
    # as a checked run. Fail closed: absent evidence is not a pass here either.
    missing = [key for key in SEALED_KEYS if key not in receipt]
    if missing:
        return Audit(
            False,
            f"receipt is missing {missing}, so an intact seal would only say that "
            "nobody edited a payload carrying no verdicts",
        )
    if not isinstance(receipt["steps"], (list, tuple)):
        return Audit(
            False,
            f"steps is {type(receipt['steps']).__name__}, not a list of step "
            "records, so this receipt does not describe a run",
        )
    if not isinstance(receipt["counts"], Mapping):
        return Audit(
            False,
            f"counts is {type(receipt['counts']).__name__}, not a mapping of "
            "counted quantities",
        )

    try:
        actual = digest_of(receipt)
    except (ReceiptError, TypeError, ValueError) as exc:
        return Audit(False, f"receipt does not serialise canonically: {exc}")

    if actual != claimed:
        return Audit(
            False,
            f"seal mismatch: payload hashes to {actual}, receipt claims {claimed}. "
            "Some field changed after sealing",
        )
    return Audit(True, f"seal intact ({DIGEST_ALGORITHM}:{actual[:12]})")
