"""The sealed run receipt.

Four properties carry the module, and each has a failure mode worth naming:

- Determinism. A digest that moves between processes proves nothing, so nothing
  in the sealing path may read a clock or stringify an object.
- Tamper detection on every field, including the integrity envelope itself, which
  the digest cannot cover.
- Tier minimum, so two strong inputs cannot carry a weak one.
- Counts taken from the records rather than asserted.

No camera, no robot, no numpy. The receipt has to be checkable by someone who has
none of this rig.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from core.verification import receipt as rc
from core.verification.agents import VerificationResult
from core.verification.receipt import (
    CANONICAL_FORM,
    DEFAULT_TIER_CEILING,
    ReceiptError,
    Tier,
    canonical,
    ceiling_for,
    digest_of,
    min_tier,
    seal,
    verify,
)

RUN_ID = "run-0001"
STAMP = "2026-07-25T14:00:00Z"


def rec(step, attempt, phase, ok, confidence, *, channels=None, disagreement=None,
        tiers=None, detail="") -> dict:
    """One step record in the shape uncap_aspirate.run already yields."""
    data: dict = {}
    if channels is not None:
        data["channels"] = dict(channels)
    if disagreement is not None:
        data["disagreement"] = dict(disagreement)
    out = {
        "step": step,
        "attempt": attempt,
        "phase": phase,
        "verification": {"ok": ok, "confidence": confidence, "detail": detail, "data": data},
    }
    if tiers is not None:
        out["evidence_tiers"] = dict(tiers)
    return out


def a_run() -> list[dict]:
    """A run with a retry, a disagreement, and a give-up: one of each outcome."""
    return [
        rec("uncap", 1, "retrying", False, 0.31,
            channels={"torque": 0.30, "depth": 0.32},
            tiers={"torque": "simulated", "depth": "simulated"}),
        rec("uncap", 2, "passed", True, 0.71,
            channels={"torque": 0.80, "depth": 0.62},
            tiers={"torque": "simulated", "depth": "simulated"}),
        rec("transport", 1, "passed", True, 0.68,
            tiers={"gripper_width": "simulated"}),
        rec("present", 1, "verifying", False, 0.0,
            channels={"torque": 0.90, "depth": 0.00},
            disagreement={"high": "torque", "low": "depth", "spread": 0.90,
                          "would_have_fused_to": 0.45},
            tiers={"torque": "simulated", "depth": "simulated"}),
        rec("aspirate", 1, "retrying", False, 0.10),
        rec("aspirate", 2, "retrying", False, 0.10),
        rec("aspirate", 3, "failed", False, 0.10),
    ]


# --- determinism -----------------------------------------------------------

def test_the_same_run_seals_to_the_same_digest():
    first = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)
    second = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)
    assert first["integrity"]["digest"] == second["integrity"]["digest"]
    assert verify(first).ok and verify(second).ok


def test_key_order_is_not_part_of_what_the_receipt_says():
    """Canonicalisation earns its keep here: same content, different insertion
    order, same digest. Without sort_keys this passes or fails on dict luck."""
    plain = a_run()
    shuffled = []
    for record in plain:
        ver = record["verification"]
        reordered = {"data": ver["data"], "detail": ver["detail"],
                     "confidence": ver["confidence"], "ok": ver["ok"]}
        item = {"verification": reordered, "phase": record["phase"],
                "attempt": record["attempt"], "step": record["step"]}
        if "evidence_tiers" in record:
            item["evidence_tiers"] = record["evidence_tiers"]
        shuffled.append(item)
    assert (seal(plain, run_id=RUN_ID, sealed_at=STAMP)["integrity"]["digest"]
            == seal(shuffled, run_id=RUN_ID, sealed_at=STAMP)["integrity"]["digest"])


def test_a_different_run_seals_differently():
    other = a_run()
    other[1]["verification"]["confidence"] = 0.72
    assert (seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)["integrity"]["digest"]
            != seal(other, run_id=RUN_ID, sealed_at=STAMP)["integrity"]["digest"])


def test_no_timestamp_appears_unless_the_caller_passes_one():
    r = seal(a_run(), run_id=RUN_ID, sealed_at=None)
    assert r["asserted"]["sealed_at"] is None
    assert "sealed_at" not in canonical(
        {k: v for k, v in r.items() if k != "asserted"})
    assert verify(r).ok


def test_the_run_id_and_time_are_only_ever_in_the_asserted_block():
    """One copy, labelled. An unlabelled second copy at the top level would be
    the one a reader trusts, and neither is a fact this module can vouch for."""
    r = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)
    assert r["asserted"]["run_id"] == RUN_ID
    assert "run" not in r
    assert RUN_ID not in canonical({k: v for k, v in r.items() if k != "asserted"})


def test_the_sealing_path_cannot_reach_a_clock():
    """A source guard, deliberately.

    Determinism cannot be shown by observing one process: a clock read looks fine
    today and produces a different digest tomorrow. So check the imports instead,
    since none of these can be called without being imported. Scanning the whole
    source for the words does not work, because the module docstring has to be
    allowed to explain which calls are banned and why.

    What this does not cover: a dynamic ``__import__`` inside a function. It is a
    guard against the accident, not against someone determined.
    """
    src = Path(rc.__file__).read_text(encoding="utf-8")
    imports = [
        line for line in src.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    for line in imports:
        for forbidden in ("time", "datetime", "random", "uuid", "secrets", "os"):
            assert forbidden not in line, f"{line!r} puts nondeterminism in reach"
    # And nothing bound one at runtime either.
    for name in ("time", "datetime", "random", "uuid", "os"):
        assert name not in vars(rc), f"{name} is bound in the module namespace"


def test_a_rejected_tier_claim_cannot_put_an_object_address_in_the_receipt():
    """A regression, not a hypothetical.

    rejected_claims is the only path that writes a caller-supplied value into the
    payload instead of raising on it, and it used to format that value with !r.
    The default repr of an object carries its address, so the same run sealed to a
    different digest in every process: exactly the failure the module docstring
    says refusing to stringify prevents.

    Two runs that differ only in which throwaway object was handed over are the
    same run, so they have to seal the same.
    """
    def claiming(claim):
        return [rec("uncap", 1, "passed", True, 0.71,
                    channels={"torque": 0.8}, tiers={"torque": claim})]

    first = seal(claiming(object()), run_id=RUN_ID, sealed_at=STAMP)
    second = seal(claiming(object()), run_id=RUN_ID, sealed_at=STAMP)
    assert first["integrity"]["digest"] == second["integrity"]["digest"]
    evidence = first["steps"][0]["evidence"]
    assert evidence["rejected_claims"] == ["torque=<object>"]
    assert evidence["inputs"]["torque"]["granted"] == "modeled"
    # No address anywhere in the sealed bytes, whatever the claim was.
    assert "0x" not in canonical(first)


def test_the_digest_is_sha256_over_the_bytes_the_envelope_describes():
    """The outside check, recomputed with hashlib and json and nothing else.

    Every other digest assertion in this file runs through digest_of or canonical,
    so all of them would still pass if canonical() changed shape or digest_of
    switched algorithm while the envelope kept claiming sha256. This one spells
    the bytes out from the envelope's own description of them.
    """
    r = sealed()
    assert r["integrity"]["algorithm"] == "sha256"
    body = {k: v for k, v in r.items() if k != "integrity"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == r["integrity"]["digest"]


def test_caller_supplied_time_is_recorded_as_an_assertion_not_a_fact():
    r = seal(a_run(), run_id=RUN_ID, sealed_at="whenever")
    assert r["asserted"]["sealed_at"] == "whenever"
    assert "not checked by this module" in r["asserted"]["note"]


# --- tamper detection ------------------------------------------------------

def sealed() -> dict:
    return seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)


def test_an_untouched_receipt_verifies():
    audit = verify(sealed())
    assert audit.ok is True
    assert "intact" in audit.detail


@pytest.mark.parametrize(
    "mutate, what",
    [
        (lambda r: r["steps"][1]["verification"].__setitem__("ok", False), "a verdict"),
        (lambda r: r["steps"][1]["verification"].__setitem__("confidence", 0.99), "a confidence"),
        (lambda r: r["steps"][1]["verification"].__setitem__("detail", "looked fine"), "a detail"),
        (lambda r: r["steps"][1]["verification"]["data"]["channels"].__setitem__("depth", 0.95),
         "a channel"),
        (lambda r: r["steps"][3]["verification"]["data"]["disagreement"].__setitem__("spread", 0.01),
         "a nested disagreement field"),
        (lambda r: r["steps"][3]["verification"]["data"].pop("disagreement"),
         "a disagreement erased"),
        (lambda r: r["steps"][1].__setitem__("attempt", 1), "an attempt number"),
        (lambda r: r["steps"][1].__setitem__("step", "uncap_v2"), "a step key"),
        (lambda r: r["steps"][6].__setitem__("phase", "passed"), "a phase"),
        (lambda r: r["steps"][1]["evidence"].__setitem__("tier", "measured"), "a step tier"),
        (lambda r: r.__setitem__("evidence_tier", "hardware-validated"), "the run tier"),
        (lambda r: r["counts"].__setitem__("retries", 0), "a count"),
        (lambda r: r["counts"].__setitem__("escalations", 0), "an escalation count"),
        (lambda r: r["rules"].__setitem__("escalation", "anything goes"), "a stated rule"),
        (lambda r: r["asserted"].__setitem__("run_id", "run-9999"), "the run id"),
        (lambda r: r["asserted"].__setitem__("sealed_at", "2020-01-01T00:00:00Z"), "the timestamp"),
        (lambda r: r["asserted"].__setitem__("note", "verified on the bench"), "the asserted note"),
        (lambda r: r["steps"].pop(3), "a whole step removed"),
        (lambda r: r["steps"].append({"index": 9, "step": "extra", "attempt": 1,
                                      "phase": "passed", "verification": None}),
         "a step appended"),
        (lambda r: r["counts"].pop("retries"), "a count deleted"),
        (lambda r: r.__setitem__("note", "approved by hand"), "a field added"),
    ],
)
def test_verify_catches(mutate, what):
    r = sealed()
    mutate(r)
    audit = verify(r)
    assert audit.ok is False, f"{what} went undetected"
    assert "mismatch" in audit.detail or "not a sha256" in audit.detail


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda r: r["integrity"].__setitem__("digest", "0" * 64), "digest replaced"),
        (lambda r: r["integrity"].__setitem__("digest", "abc"), "digest truncated"),
        (lambda r: r["integrity"].__setitem__("digest", None), "digest removed"),
        (lambda r: r["integrity"].__setitem__("algorithm", "md5"), "algorithm downgraded"),
        (lambda r: r["integrity"].__setitem__("canonicalisation", "json"), "rules rewritten"),
        (lambda r: r.__setitem__("integrity", None), "envelope removed"),
        (lambda r: r.__setitem__("receipt_version", 99), "version from the future"),
    ],
)
def test_verify_refuses_a_doctored_envelope(mutate, why):
    """The envelope is the one part the digest cannot cover, so it is checked by
    shape. Otherwise swapping the algorithm is a free pass."""
    r = sealed()
    mutate(r)
    assert verify(r).ok is False, why


def test_the_digest_covers_the_payload_without_the_envelope():
    """Verifying has to be well defined, which means both sides hash the same
    bytes. The digest is over the payload with 'integrity' removed."""
    r = sealed()
    assert r["integrity"]["digest"] == digest_of(r)
    body = {k: v for k, v in r.items() if k != "integrity"}
    assert digest_of(body) == r["integrity"]["digest"]
    assert '"integrity"' not in canonical(body)
    assert r["integrity"]["canonicalisation"] == CANONICAL_FORM


def test_verify_rejects_things_that_are_not_receipts():
    for junk in (None, "receipt", 7, [], {}):
        assert verify(junk).ok is False


def test_an_intact_seal_over_an_empty_payload_is_not_a_verified_receipt():
    """Fail closed, the same rule the agents follow.

    A digest says the bytes did not change since somebody hashed them. It says
    nothing about whether the bytes contain a verdict. This payload is honestly
    sealed and carries no steps and no counts, and it used to come back "seal
    intact", which a consumer doing `if verify(r).ok` reads as a checked run.
    """
    hollow = {"receipt_version": 1}
    hollow["integrity"] = {"algorithm": "sha256", "canonicalisation": CANONICAL_FORM,
                           "digest": digest_of(hollow)}
    audit = verify(hollow)
    assert audit.ok is False
    assert "missing" in audit.detail


@pytest.mark.parametrize(
    "field, value",
    [
        ("steps", "four steps, all fine"),
        ("steps", None),
        ("counts", "all green"),
    ],
)
def test_a_resealed_receipt_of_the_wrong_shape_is_refused(field, value):
    """Re-sealed by hand, so the digest matches and only the shape check is left.

    Hashing is not signing: anyone holding this module can compute a fresh digest.
    So the parts of a receipt that the digest cannot argue about have to be checked
    for what they are, or a payload whose whole step list is a sentence verifies.
    """
    r = sealed()
    r[field] = value
    r["integrity"]["digest"] = digest_of(r)
    assert verify(r).ok is False


def test_a_version_of_true_rather_than_1_is_refused():
    """bool is an int in Python, so True == 1 and `receipt_version: true` walked
    straight through the version gate. Re-sealed here so the digest is not what
    catches it."""
    r = sealed()
    r["receipt_version"] = True
    r["integrity"]["digest"] = digest_of(r)
    assert verify(r).ok is False


# --- tier arithmetic -------------------------------------------------------

def test_min_tier_is_the_weakest_input():
    assert min_tier([Tier.MEASURED, Tier.MODELED]) is Tier.MODELED
    assert min_tier([Tier.HARDWARE_VALIDATED, Tier.SIMULATED]) is Tier.SIMULATED
    assert min_tier([Tier.MEASURED, Tier.SIMULATED, Tier.HARDWARE_VALIDATED]) is Tier.SIMULATED
    assert min_tier([Tier.MEASURED]) is Tier.MEASURED
    assert min_tier([Tier.HARDWARE_VALIDATED, Tier.HARDWARE_VALIDATED]) is Tier.HARDWARE_VALIDATED


def test_min_tier_of_nothing_is_the_weakest_not_the_strongest():
    """Fail closed: no attested input is the weakest case, not an unrated one."""
    assert min_tier([]) is Tier.MODELED


def test_the_vocabulary_is_exactly_four_words():
    assert [t.value for t in Tier] == [
        "modeled", "simulated", "measured", "hardware-validated"]


def test_a_caller_cannot_claim_hardware_validated():
    """The load-bearing test. Every claim in this run is the strongest word in
    the vocabulary; none of it survives the ceiling."""
    run = [
        rec("uncap", 1, "passed", True, 0.71,
            channels={"torque": 0.80, "vision": 0.75, "depth": 0.62},
            tiers={"torque": "hardware-validated", "vision": "hardware-validated",
                   "depth": "hardware-validated"}),
    ]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    inputs = r["steps"][0]["evidence"]["inputs"]
    for name in ("torque", "vision", "depth"):
        assert inputs[name]["claimed"] == "hardware-validated"
        assert inputs[name]["granted"] == "simulated"
    assert r["steps"][0]["evidence"]["tier"] == "simulated"
    assert r["evidence_tier"] == "simulated"
    # Nowhere in the sealed bytes does this run claim to be hardware-validated
    # except in the record of what was asked for.
    granted = [v["granted"] for v in inputs.values()]
    assert "hardware-validated" not in granted


def test_no_ceiling_in_this_repo_reaches_hardware_validated():
    """Not an arbitrary rule. Nothing here has been validated against the
    physical cell, so nothing may claim it was."""
    assert DEFAULT_TIER_CEILING is Tier.MODELED
    assert all(c is not Tier.HARDWARE_VALIDATED for c in rc.INPUT_TIER_CEILING.values())


def test_an_unlisted_input_is_capped_at_modeled_and_drags_the_conclusion_down():
    """Minimum, observed through the public API: one weak input decides."""
    run = [
        rec("uncap", 1, "passed", True, 0.71,
            channels={"torque": 0.80, "hunch": 0.99},
            tiers={"torque": "measured", "hunch": "measured"}),
    ]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    inputs = r["steps"][0]["evidence"]["inputs"]
    assert ceiling_for("hunch") is Tier.MODELED
    assert inputs["torque"]["granted"] == "simulated"
    assert inputs["hunch"]["granted"] == "modeled"
    assert r["steps"][0]["evidence"]["tier"] == "modeled"


def test_an_unclaimed_input_is_modeled_not_assumed_good():
    run = [rec("uncap", 1, "passed", True, 0.71, channels={"torque": 0.80})]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    torque = r["steps"][0]["evidence"]["inputs"]["torque"]
    assert torque["claimed"] is None
    assert torque["granted"] == "modeled"


def test_a_word_outside_the_vocabulary_is_rejected_and_stays_visible():
    run = [
        rec("uncap", 1, "passed", True, 0.71, channels={"torque": 0.80},
            tiers={"torque": "bench-proven, trust me"}),
    ]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    evidence = r["steps"][0]["evidence"]
    assert evidence["inputs"]["torque"]["granted"] == "modeled"
    assert evidence["rejected_claims"] == ["torque='bench-proven, trust me'"]


def test_a_verification_with_no_attested_input_is_modeled():
    run = [rec("aspirate", 1, "passed", True, 0.66)]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    assert r["steps"][0]["evidence"]["inputs"] == {}
    assert r["steps"][0]["evidence"]["tier"] == "modeled"
    assert r["evidence_tier"] == "modeled"


def test_a_declared_input_is_labelled_as_declared_not_as_a_channel():
    run = [rec("aspirate", 1, "passed", True, 0.66,
               channels={"torque": 0.8}, tiers={"ot_volume_report": "measured"})]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    inputs = r["steps"][0]["evidence"]["inputs"]
    assert inputs["torque"]["source"] == "channel"
    assert inputs["ot_volume_report"]["source"] == "declared"


def test_the_run_tier_is_the_weakest_step_tier():
    run = [
        rec("uncap", 1, "passed", True, 0.71, channels={"torque": 0.8},
            tiers={"torque": "measured"}),
        rec("aspirate", 1, "passed", True, 0.66),  # no attested input -> modeled
    ]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    assert r["steps"][0]["evidence"]["tier"] == "simulated"
    assert r["steps"][1]["evidence"]["tier"] == "modeled"
    assert r["evidence_tier"] == "modeled"


def test_the_ceiling_table_is_recorded_in_the_receipt():
    """An auditor needs the rule that was applied, not just the outcome."""
    r = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)
    assert r["rules"]["tier_ceiling"]["torque"] == "simulated"
    assert r["rules"]["tier_ceiling_default"] == "modeled"


# --- counting --------------------------------------------------------------

def test_retries_and_escalations_are_counted():
    c = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)["counts"]
    assert c["verifications"] == 7
    assert c["passed"] == 2
    assert c["not_passed"] == 5
    # uncap attempt 2, aspirate attempts 2 and 3.
    assert c["retries"] == 3
    assert c["attempts_per_step"] == {"aspirate": 3, "present": 1, "transport": 1, "uncap": 2}
    # The disagreement on 'present' and the exhausted budget on 'aspirate'.
    assert c["escalations"] == 2
    assert c["escalation_reasons"] == {
        "retry_budget_exhausted": 1, "orchestrator_escalated": 0, "channel_disagreement": 1}


def test_the_orchestrators_escalated_phase_is_counted_as_an_escalation():
    """uncap_aspirate.run yields 'escalated' as a terminal phase distinct from
    'failed'. A receipt that only knew about 'failed' would report a stopped run
    as a plain non-pass, which is the distinction the workflow exists to draw."""
    run = [
        rec("uncap", 1, "escalated", False, 0.0,
            channels={"torque": 0.90, "depth": 0.00},
            disagreement={"high": "torque", "low": "depth", "spread": 0.90,
                          "would_have_fused_to": 0.45}),
    ]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    assert r["counts"]["escalations"] == 1
    assert r["counts"]["escalation_reasons"]["orchestrator_escalated"] == 1
    assert r["counts"]["escalation_reasons"]["retry_budget_exhausted"] == 0
    assert r["steps"][0]["escalation_reasons"] == [
        "channel_disagreement", "orchestrator_escalated"]
    assert r["counts"]["retries"] == 0


def test_a_record_that_is_both_failed_and_contradictory_counts_once():
    run = [
        rec("uncap", 3, "failed", False, 0.0,
            channels={"torque": 0.9, "depth": 0.0},
            disagreement={"high": "torque", "low": "depth", "spread": 0.9,
                          "would_have_fused_to": 0.45}),
    ]
    c = seal(run, run_id=RUN_ID, sealed_at=STAMP)["counts"]
    assert c["escalations"] == 1
    assert c["escalation_reasons"] == {
        "retry_budget_exhausted": 1, "orchestrator_escalated": 0, "channel_disagreement": 1}
    assert seal(run, run_id=RUN_ID, sealed_at=STAMP)["steps"][0]["escalation_reasons"] == [
        "channel_disagreement", "retry_budget_exhausted"]


def test_a_clean_run_escalates_nothing():
    run = [rec("uncap", 1, "passed", True, 0.71, channels={"torque": 0.8, "depth": 0.62})]
    c = seal(run, run_id=RUN_ID, sealed_at=STAMP)["counts"]
    assert c["retries"] == 0
    assert c["escalations"] == 0
    assert set(c["escalation_reasons"].values()) == {0}


def test_a_phase_this_module_does_not_know_counts_as_no_escalation():
    """Recorded verbatim, counted as nothing. That is the fail-closed direction
    for a count (no invented escalation), but it does mean a new terminal phase
    in the workflow has to be added to _ESCALATION_PHASES here."""
    run = [rec("uncap", 1, "abandoned", False, 0.0, channels={"torque": 0.1})]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    assert r["steps"][0]["phase"] == "abandoned"
    assert r["steps"][0]["escalated"] is False
    assert r["counts"]["escalations"] == 0


def test_resubmitting_the_same_attempt_does_not_inflate_the_retry_count():
    """Counted per (step, attempt), so a duplicated event is not a second retry."""
    run = a_run() + [a_run()[1]]
    assert seal(run, run_id=RUN_ID, sealed_at=STAMP)["counts"]["retries"] == 3


def test_only_channels_that_carried_a_number_are_counted_as_contributing():
    """An abstaining channel is absent from data['channels'], which is the whole
    point of abstaining. A non-numeric entry is not a confidence either."""
    run = [
        rec("uncap", 1, "retrying", False, 0.3, channels={"torque": 0.3}),
        rec("uncap", 2, "passed", True, 0.71, channels={"torque": 0.8, "depth": 0.62}),
        rec("present", 1, "passed", True, 0.68, channels={"depth": "unknown"}),
    ]
    c = seal(run, run_id=RUN_ID, sealed_at=STAMP)["counts"]
    assert c["channel_contributions"] == {"depth": 1, "torque": 2}
    assert c["distinct_channels"] == 2


def test_a_record_with_no_verification_is_never_counted_as_a_pass():
    run = [{"step": "uncap", "attempt": 1, "phase": "started"}]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    c = r["counts"]
    assert c["step_records"] == 1
    assert c["verifications"] == 0
    assert c["records_without_verification"] == 1
    assert c["passed"] == 0
    assert r["steps"][0]["verification"] is None


def test_a_truthy_non_bool_is_not_a_verdict():
    """Fail closed. 'ok': 'false' is truthy in Python, and a receipt that read it
    as a pass would be the exact failure this layer exists to prevent."""
    run = [rec("uncap", 1, "passed", "false", 0.71, channels={"torque": 0.8})]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    assert r["steps"][0]["verification"]["ok"] is False
    assert r["counts"]["passed"] == 0


def test_the_evidence_data_is_carried_whole_not_trimmed_to_the_channels():
    """The numbers a human would argue with are in data, not in the confidence.
    A receipt that kept only the fused channels would drop exactly the fields
    that show why a channel said what it said."""
    run = [rec("uncap", 1, "passed", True, 0.71, channels={"torque": 0.8, "depth": 0.62})]
    run[0]["verification"]["data"] |= {
        "torque_peak_nm": 1.4, "torque_final_nm": 0.2, "depth_delta_mm": 15.3,
        "depth_valid_fraction": 0.82, "depth_verdict": "cap_off",
        "vision_datum_referenced": False,
    }
    data = seal(run, run_id=RUN_ID, sealed_at=STAMP)["steps"][0]["verification"]["data"]
    assert data["torque_peak_nm"] == 1.4
    assert data["depth_valid_fraction"] == 0.82
    assert data["vision_datum_referenced"] is False
    assert data["channels"] == {"torque": 0.8, "depth": 0.62}


def test_the_receipt_states_the_rules_it_counted_by():
    rules = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)["rules"]
    assert "attempt" in rules["retry"]
    assert "disagreement" in rules["escalation"]
    assert "weakest" in rules["tier"]


def test_completeness_is_labelled_as_asserted_not_counted():
    """seal() counts the records it was handed and cannot know about one that was
    dropped, so the receipt says so instead of implying coverage."""
    note = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)["asserted"]["note"]
    assert "asserted by construction" in note
    assert "dropped before sealing" in note


# --- what it refuses to seal ----------------------------------------------

def test_a_real_verification_result_is_accepted():
    """The dataclass the agents actually return, not just the event dict."""
    result = VerificationResult(
        True, 0.71, "torque=0.80, depth=0.62",
        {"channels": {"torque": 0.80, "depth": 0.62}},
    )
    run = [{"step": "uncap", "attempt": 1, "phase": "passed", "verification": result,
            "evidence_tiers": {"torque": "simulated"}}]
    r = seal(run, run_id=RUN_ID, sealed_at=STAMP)
    assert r["steps"][0]["verification"]["confidence"] == 0.71
    assert r["counts"]["channel_contributions"] == {"depth": 1, "torque": 1}
    assert verify(r).ok


def test_nan_confidence_is_refused():
    run = [rec("uncap", 1, "passed", True, float("nan"))]
    with pytest.raises(ReceiptError, match="NaN"):
        seal(run, run_id=RUN_ID, sealed_at=STAMP)


def test_an_unserialisable_value_is_refused_by_name_not_stringified():
    """str() of an arbitrary object embeds its id, so the same run would seal to
    a different digest next process. Refuse instead."""
    run = [rec("uncap", 1, "passed", True, 0.71)]
    run[0]["verification"]["data"]["channels"] = {"torque": 0.8}
    run[0]["verification"]["data"]["reference"] = object()
    with pytest.raises(ReceiptError, match="not a JSON primitive"):
        seal(run, run_id=RUN_ID, sealed_at=STAMP)


@pytest.mark.parametrize(
    "broken, match",
    [
        ({"attempt": 1, "phase": "passed"}, "step"),
        ({"step": "uncap", "phase": "passed"}, "attempt"),
        ({"step": "uncap", "attempt": 0, "phase": "passed"}, "1 or greater"),
        ({"step": "uncap", "attempt": "two", "phase": "passed"}, "must be an int"),
        ({"step": "uncap", "attempt": 1, "phase": 7}, "phase"),
    ],
)
def test_a_malformed_record_is_refused_rather_than_guessed_at(broken, match):
    with pytest.raises(ReceiptError, match=match):
        seal([broken], run_id=RUN_ID, sealed_at=STAMP)


def test_a_missing_confidence_is_refused():
    run = [{"step": "uncap", "attempt": 1, "phase": "passed",
            "verification": {"ok": True, "detail": "", "data": {}}}]
    with pytest.raises(ReceiptError, match="confidence"):
        seal(run, run_id=RUN_ID, sealed_at=STAMP)


def test_a_run_needs_an_id():
    with pytest.raises(ReceiptError, match="run_id"):
        seal(a_run(), run_id="", sealed_at=STAMP)


def test_a_bare_mapping_is_not_a_run():
    with pytest.raises(ReceiptError, match="ordered sequence"):
        seal({"step": "uncap"}, run_id=RUN_ID, sealed_at=STAMP)


def test_an_empty_run_seals_and_claims_nothing():
    r = seal([], run_id=RUN_ID, sealed_at=STAMP)
    assert r["counts"]["verifications"] == 0
    assert r["counts"]["passed"] == 0
    assert r["evidence_tier"] == "modeled"
    assert verify(r).ok


def test_the_real_workflow_event_stream_seals_and_counts():
    """Counted from the events uncap_aspirate.run actually yields, not a fixture.

    Every other counting test in this file feeds seal() records written by hand in
    this file, so all of them would keep passing if the orchestrator's event shape
    drifted away from them: they would be counting the fixture. This one runs the
    real plan with no devices attached. cap_removed then has no channel to read and
    fails closed three times, so the receipt has to find two retries and one spent
    budget in the real dicts, where the verdict arrives as result.__dict__ nested
    under 'verification' and the started/verifying events carry no verdict at all.

    Imported inside the test to keep the module-level cost of this file at
    stdlib plus agents: the workflow pulls in drivers and core.config.
    """
    from backend.app.workflows import uncap_aspirate as wf

    class _NoDevices:
        """Every lookup misses, which the workflow swallows. No arm, no camera."""

        def get(self, device_id):
            raise KeyError(device_id)

    plan = [wf.Step("uncap", "dual_arm_manipulation", ["left", "right"],
                    "cap_removed", {"turning_arm": "right", "holding_arm": "left"})]
    events = list(wf.run(_NoDevices(), plan=plan,
                         execute=lambda step, dm, sample: None))
    assert [e["phase"] for e in events][-1] == "failed"

    r = seal(events, run_id=RUN_ID, sealed_at=STAMP)
    c = r["counts"]
    assert c["step_records"] == len(events)
    assert c["verifications"] == 3                    # one verdict per attempt
    assert c["records_without_verification"] == 6     # started + verifying, each attempt
    assert c["passed"] == 0
    assert c["retries"] == 2
    assert c["escalations"] == 1
    assert c["escalation_reasons"]["retry_budget_exhausted"] == 1
    assert c["escalation_reasons"]["channel_disagreement"] == 0
    # No channel could read anything, so nothing is attested.
    assert c["distinct_channels"] == 0
    assert r["evidence_tier"] == "modeled"
    assert verify(r).ok


def test_the_receipt_is_plain_json():
    """Someone months from now has to be able to read this with json.loads and
    nothing else installed."""
    r = seal(a_run(), run_id=RUN_ID, sealed_at=STAMP)
    round_tripped = json.loads(json.dumps(r))
    assert round_tripped == r
    assert verify(round_tripped).ok


def test_verifying_does_not_mutate_the_receipt():
    r = sealed()
    before = copy.deepcopy(r)
    assert verify(r).ok
    assert r == before
