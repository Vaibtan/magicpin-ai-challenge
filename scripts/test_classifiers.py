"""Deterministic unit tests for classify_reply (regex path only — no Haiku call).

Run:
    python scripts/test_classifiers.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from classifiers import classify_reply  # noqa: E402


# Each case: (message, conv_history, expected_label, optional_expected_wait_seconds)
CASES = [
    # ---- auto_reply (verbatim duplicate) ----
    ("Thank you for contacting us! Our team will respond shortly.", None, "auto_reply", None),
    ("We will get back to you within 24 hours.", None, "auto_reply", None),
    ("This is an automated reply.", None, "auto_reply", None),
    ("I am an automated assistant.", None, "auto_reply", None),
    ("Currently out of office", None, "auto_reply", None),
    # Verbatim dup of prior merchant turn
    ("Same canned reply", [{"from": "merchant", "body": "Same canned reply"}], "auto_reply", None),

    # ---- hostile ----
    ("Stop messaging me. This is useless spam.", None, "hostile", None),
    ("Don't message me again", None, "hostile", None),
    ("This is spam, leave me alone", None, "hostile", None),
    ("Bakwas mat karo", None, "hostile", None),

    # ---- not_interested ----
    ("Not interested, thanks", None, "not_interested", None),
    ("No thanks", None, "not_interested", None),
    ("Please remove me from your list", None, "not_interested", None),
    ("Nahi chahiye", None, "not_interested", None),
    ("Unsubscribe", None, "not_interested", None),

    # ---- intent_action ----
    ("Ok lets do it. Whats next?", None, "intent_action", None),
    ("Go ahead", None, "intent_action", None),
    ("Yes please send", None, "intent_action", None),
    ("Send it now", None, "intent_action", None),
    ("haan kar do", None, "intent_action", None),
    ("Mujhe magicpin judrna hai", None, "intent_action", None),
    ("ok", None, "intent_action", None),

    # ---- defer (with wait_seconds) ----
    ("Send tomorrow", None, "defer", 86400),
    ("Send me later", None, "defer", 3600),
    ("In 30 minutes", None, "defer", 1800),
    ("In 2 hours please", None, "defer", 7200),
    ("Next week", None, "defer", 604800),
    ("Kal baat karte hain", None, "defer", 86400),
    ("Day after tomorrow", None, "defer", 172800),
    ("In half an hour", None, "defer", 1800),
]


async def main() -> int:
    print("classify_reply (regex prefilter path)")
    print("-" * 50)
    failures = 0
    for msg, hist, expected_label, expected_wait in CASES:
        result = await classify_reply(msg, hist, conversation_id="test")
        label = result["label"]
        wait = result.get("wait_seconds")
        ok = (label == expected_label)
        if expected_wait is not None:
            ok = ok and (wait == expected_wait)
        status = "OK  " if ok else "FAIL"
        wait_str = f" wait={wait}" if expected_wait is not None else ""
        expected_wait_str = f" expected_wait={expected_wait}" if expected_wait is not None else ""
        print(f"  [{status}] {msg[:50]!r:50} -> {label}{wait_str}  "
              f"(expected {expected_label}{expected_wait_str})")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"FAILED: {failures} / {len(CASES)}")
        return 1
    print(f"ALL {len(CASES)} CASES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
