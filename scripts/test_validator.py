"""Deterministic unit tests for validator.validate (no LLM required).

Run:
    python scripts/test_validator.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import ComposedMessage  # noqa: E402
from validator import _detect_language, _normalize, fallback, validate  # noqa: E402


# ---- fixtures (shaped like real dataset payloads) --------------------------

DENTIST_CATEGORY = {
    "slug": "dentists",
    "voice": {
        "tone": "peer_clinical",
        "vocab_allowed": ["fluoride varnish", "scaling", "caries"],
        "vocab_taboo": ["guaranteed", "100% safe", "miracle", "best in city"],
    },
    "peer_stats": {"avg_ctr": 0.030, "avg_review_count": 62},
    "digest": [
        {
            "id": "d_2026W17_jida_fluoride",
            "title": "3-month fluoride varnish recall outperforms 6-month for high-risk adult caries",
            "source": "JIDA Oct 2026, p.14",
            "trial_n": 2100,
        },
    ],
}

MERCHANT = {
    "merchant_id": "m_001",
    "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera",
                 "languages": ["en", "hi"], "city": "Delhi", "locality": "Lajpat Nagar"},
}

TRIGGER_RESEARCH = {
    "id": "trg_001",
    "kind": "research_digest",
    "scope": "merchant",
    "source": "external",
    "urgency": 2,
    "suppression_key": "research:dentists:2026-W17",
    "payload": {"top_item_id": "d_2026W17_jida_fluoride"},
}


def _ok(body: str, anchor: str, **overrides) -> ComposedMessage:
    base = dict(
        body=body,
        cta="open_ended",
        send_as="vera",
        suppression_key=TRIGGER_RESEARCH["suppression_key"],
        rationale="ok rationale",
        anchor=anchor,
        lever="reciprocity",
    )
    base.update(overrides)
    return ComposedMessage(**base)


def expect(name: str, errors: list[str], should_fail: bool, *, contains: str | None = None) -> None:
    failed = bool(errors)
    if failed != should_fail:
        print(f"  [FAIL] {name}: expected {'FAIL' if should_fail else 'PASS'}, got {errors}")
        sys.exit(1)
    if contains and not any(contains in e for e in errors):
        print(f"  [FAIL] {name}: expected error containing {contains!r}, got {errors}")
        sys.exit(1)
    status = "FAIL (expected)" if should_fail else "PASS"
    print(f"  [OK]   {name}: {status}")


def main() -> int:
    print("validator.validate tests")
    print("------------------------")

    # 1. Anchor verifiable — passes when anchor is in contexts
    composed = _ok(
        body="Dr. Meera, this week's JIDA Oct 2026, p.14 dropped — 2100-patient trial. Want the abstract?",
        anchor="JIDA Oct 2026, p.14",
    )
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("anchor present in contexts", errors, should_fail=False)

    # 2. Anchor fabricated
    composed = _ok(
        body="Dr. Meera, the 4500-patient AIIMS trial dropped this week. Want the abstract?",
        anchor="4500-patient AIIMS trial",
    )
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("anchor fabricated", errors, should_fail=True, contains="anchor_fabricated")

    # 3. Anchor missing when mandatory
    composed = _ok(body="Dr. Meera, quick check-in. Anything I can help with?", anchor="")
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("anchor missing (mandatory)", errors, should_fail=True, contains="anchor: missing")

    # 4. Anchor missing when optional → pass
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=False)
    expect("anchor missing (optional kind)", errors, should_fail=False)

    # 5. Vocab taboo
    composed = _ok(
        body="Dr. Meera, our guaranteed approach gives the best results. Want the abstract?",
        anchor="JIDA Oct 2026, p.14",
    )
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("taboo word used", errors, should_fail=True, contains="taboo_used")

    # 6. Body too short
    composed = _ok(body="Hi", anchor="JIDA Oct 2026, p.14")
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("body too short", errors, should_fail=True, contains="too short")

    # 7. Invalid CTA
    composed = _ok(
        body="Dr. Meera, this week's JIDA Oct 2026, p.14 dropped. Want the abstract?",
        anchor="JIDA Oct 2026, p.14",
        cta="something_else",
    )
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("invalid cta", errors, should_fail=True, contains="invalid cta")

    # 8. send_as integrity (customer present but send_as=vera)
    composed = _ok(
        body="Hi Priya, your recall window opened. Reply YES to schedule.",
        anchor="JIDA Oct 2026, p.14", send_as="vera",
    )
    customer = {"customer_id": "c_001", "identity": {"name": "Priya"}}
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=customer, anchor_required=True)
    expect("send_as wrong for customer", errors, should_fail=True, contains="send_as:")

    # 9. Skip-veto — empty body with rationale starting "skip:" → pass
    composed = ComposedMessage(
        body="", cta="none", send_as="vera",
        suppression_key=TRIGGER_RESEARCH["suppression_key"],
        rationale="skip: aligner trend doesn't fit a peds-only practice",
        anchor="", lever="", skip_reason="aligner trend doesn't fit a peds-only practice",
    )
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True)
    expect("skip-veto recognized", errors, should_fail=False)

    # 10. Anti-repetition (with prior_bot_hashes)
    composed = _ok(
        body="Dr. Meera, this week's JIDA Oct 2026, p.14 dropped — 2100-patient trial. Want the abstract?",
        anchor="JIDA Oct 2026, p.14",
    )
    from validator import _hash_body_norm
    prior = {_hash_body_norm(composed.body)}
    errors = validate(composed, category=DENTIST_CATEGORY, merchant=MERCHANT,
                      trigger=TRIGGER_RESEARCH, customer=None, anchor_required=True,
                      prior_bot_hashes=prior)
    expect("anti-repetition", errors, should_fail=True, contains="repeats_prior")

    print()
    print("language detection")
    print("------------------")
    cases = [
        ("Hi Dr. Meera, this week's JIDA digest dropped. Want the abstract?", {"en"}),
        ("Aapka Google profile abhi 62.5% complete hai — quick yes/no?", {"hi", "en"}),
        ("नमस्ते डॉक्टर साहब, यह सप्ताह का", {"hi"}),
    ]
    for text, expected in cases:
        det = _detect_language(text)
        ok = bool(det["langs"] & expected)
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {text[:40]!r:40} -> {sorted(det['langs'])}  (expected {sorted(expected)}, conf={det['confidence']})")
        if not ok:
            sys.exit(1)

    print()
    print("fallback templates")
    print("------------------")
    fb = fallback(TRIGGER_RESEARCH, MERCHANT, None)
    print(f"  body: {fb.body!r}")
    print(f"  fallback_used: {fb.fallback_used}")
    assert fb.fallback_used is True
    assert "Dr. Meera" in fb.body, f"expected Dr. salutation; got {fb.body!r}"

    # Customer fallback
    fb_c = fallback({"id": "trg_x", "kind": "recall_due", "urgency": 3, "suppression_key": "k"},
                    MERCHANT, {"identity": {"name": "Priya"}})
    print(f"  customer fb body: {fb_c.body!r}")
    assert "Priya" in fb_c.body
    assert "Dr. Meera's Dental Clinic" in fb_c.body
    assert fb_c.send_as == "merchant_on_behalf"

    print()
    print("ALL TESTS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
