"""Drive bot.compose() on a single canonical test pair and print the output.

The S06 eyeball gate: hand-inspect output before scaling to all 14 playbooks.

Usage:
    python scripts/compose_one.py T01
    python scripts/compose_one.py T03   # customer-scope (will fail until S11)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402


DATASET = ROOT / "dataset"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_pair(test_id: str) -> dict:
    """Find the test pair across test_pairs.json and holdout_pairs.json."""
    for fname in ("test_pairs.json", "holdout_pairs.json"):
        data = _load_json(DATASET / fname)
        for pair in data["pairs"]:
            if pair["test_id"] == test_id:
                return pair
    raise SystemExit(f"test_id {test_id!r} not found in test_pairs.json or holdout_pairs.json")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_id", help="e.g. T01, T15, H01")
    args = parser.parse_args()

    pair = _resolve_pair(args.test_id)

    merchant = _load_json(DATASET / "merchants" / f"{pair['merchant_id']}.json")
    trigger = _load_json(DATASET / "triggers" / f"{pair['trigger_id']}.json")
    category = _load_json(DATASET / "categories" / f"{merchant['category_slug']}.json")
    customer = (_load_json(DATASET / "customers" / f"{pair['customer_id']}.json")
                if pair.get("customer_id") else None)

    print(f"=== {args.test_id} ===")
    print(f"merchant: {merchant['identity']['name']} ({merchant['merchant_id']})")
    print(f"category: {merchant['category_slug']}")
    print(f"trigger:  {trigger['kind']} (urgency {trigger['urgency']})")
    print(f"customer: {customer['identity']['name'] if customer else '(none)'}")
    print()

    composed = await compose(category, merchant, trigger, customer, test_id=args.test_id)

    print("=== ComposedMessage (full, including private fields) ===")
    print(json.dumps({
        "body": composed.body,
        "cta": composed.cta,
        "send_as": composed.send_as,
        "suppression_key": composed.suppression_key,
        "rationale": composed.rationale,
        "anchor": composed.anchor,
        "lever": composed.lever,
        "prompt_version": composed.prompt_version,
        "fallback_used": composed.fallback_used,
        "skip_reason": composed.skip_reason,
        "model": composed.model,
        "cache_hit": composed.cache_hit,
        "latency_ms": composed.latency_ms,
        "input_tokens": {"cached": composed.input_tokens_cached,
                         "uncached": composed.input_tokens_uncached},
        "output_tokens": composed.output_tokens,
        "validation_errors": composed.validation_errors,
        "validation_retried": composed.validation_retried,
    }, ensure_ascii=False, indent=2))

    print()
    print("=== Public output (what /v1/tick + submission.jsonl would emit) ===")
    print(json.dumps(composed.public(), ensure_ascii=False, indent=2))

    print()
    print(f"Body chars: {len(composed.body)}")
    if composed.is_skip():
        print("STATUS: SKIP (composer self-veto)")
    elif composed.fallback_used:
        print("STATUS: FALLBACK (validator exhausted)")
    elif composed.validation_retried:
        print("STATUS: PASS (after retry)")
    else:
        print("STATUS: PASS (first try)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
