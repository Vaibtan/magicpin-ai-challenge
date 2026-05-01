"""Standalone batch JSONL generator.

Reads dataset/test_pairs.json or dataset/holdout_pairs.json, loads category +
merchant + trigger + (customer) JSON for each pair, calls bot.compose(),
writes one line per pair to submission.jsonl (or holdout_outputs.jsonl).

Pairs are processed in category-sorted order so consecutive Anthropic calls
re-hit the same prompt-cache prefix → max prompt-cache reuse.

Usage:
    python make_submission.py --pair T01            # single pair (S07)
    python make_submission.py --all-merchant         # the 25 merchant-scope (S10)
    python make_submission.py --all                  # all 30 (S19)
    python make_submission.py --holdout              # the 10 holdout pairs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402
from obs import log_event  # noqa: E402


DATASET = ROOT / "dataset"
SUBMISSION_FILE = ROOT / "submission.jsonl"
HOLDOUT_OUTPUT_FILE = ROOT / "holdout_outputs.jsonl"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_pair_inputs(pair: dict[str, Any]) -> tuple[dict, dict, dict, dict | None]:
    merchant = _load_json(DATASET / "merchants" / f"{pair['merchant_id']}.json")
    trigger = _load_json(DATASET / "triggers" / f"{pair['trigger_id']}.json")
    category = _load_json(DATASET / "categories" / f"{merchant['category_slug']}.json")
    customer = (_load_json(DATASET / "customers" / f"{pair['customer_id']}.json")
                if pair.get("customer_id") else None)
    return category, merchant, trigger, customer


def _select_pairs(args: argparse.Namespace) -> tuple[list[dict[str, Any]], Path]:
    """Resolve which pairs to process and which output file to write."""
    if args.holdout:
        pairs = _load_json(DATASET / "holdout_pairs.json")["pairs"]
        return pairs, HOLDOUT_OUTPUT_FILE
    if args.pair:
        pairs = _load_json(DATASET / "test_pairs.json")["pairs"]
        matched = [p for p in pairs if p["test_id"] == args.pair]
        if not matched:
            # try holdout
            pairs = _load_json(DATASET / "holdout_pairs.json")["pairs"]
            matched = [p for p in pairs if p["test_id"] == args.pair]
        if not matched:
            raise SystemExit(f"test_id {args.pair!r} not found in test_pairs.json or holdout_pairs.json")
        return matched, SUBMISSION_FILE
    pairs = _load_json(DATASET / "test_pairs.json")["pairs"]
    if args.all_merchant:
        pairs = [p for p in pairs if not p.get("customer_id")]
    return pairs, SUBMISSION_FILE


def _category_sort(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by merchant.category_slug so consecutive composes re-hit the
    Anthropic prompt-cache prefix. Within each category, preserve original
    order (test_id ascending) for diff stability."""
    cat_for: dict[str, str] = {}
    for p in pairs:
        if p["merchant_id"] not in cat_for:
            m = _load_json(DATASET / "merchants" / f"{p['merchant_id']}.json")
            cat_for[p["merchant_id"]] = m["category_slug"]
    return sorted(pairs, key=lambda p: (cat_for[p["merchant_id"]], p["test_id"]))


async def _process_one(pair: dict[str, Any]) -> dict[str, Any]:
    category, merchant, trigger, customer = _resolve_pair_inputs(pair)
    composed = await compose(category, merchant, trigger, customer, test_id=pair["test_id"])
    line = {"test_id": pair["test_id"], **composed.public()}
    return {"line": line, "composed": composed, "pair": pair}


async def main() -> int:
    parser = argparse.ArgumentParser()
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--pair", metavar="TEST_ID", help="Run a single test pair (e.g. T01)")
    g.add_argument("--all-merchant", action="store_true", help="Run the 25 merchant-scope pairs")
    g.add_argument("--all", action="store_true", help="Run all 30 pairs")
    g.add_argument("--holdout", action="store_true", help="Run the 10-pair holdout set")
    parser.add_argument("--score", action="store_true",
                        help="(holdout only) Also score outputs via the judge LLM")
    args = parser.parse_args()

    pairs, out_file = _select_pairs(args)
    pairs = _category_sort(pairs)

    print(f"Processing {len(pairs)} pair(s) → {out_file.name}")
    print(f"  category-sorted order: {[p['test_id'] for p in pairs]}")
    print()

    start = time.monotonic()

    # Sequential to maximize prompt-cache hits in category-sorted order.
    # (Parallel composes within a category would race the cache write.)
    results: list[dict[str, Any]] = []
    for i, pair in enumerate(pairs, 1):
        t0 = time.monotonic()
        try:
            res = await _process_one(pair)
        except Exception as exc:
            print(f"  [ERROR] {pair['test_id']}: {exc}")
            log_event("make_submission_error", test_id=pair["test_id"], error=str(exc),
                      error_type=type(exc).__name__)
            continue
        results.append(res)
        c = res["composed"]
        marker = "FALLBACK" if c.fallback_used else ("SKIP" if c.is_skip() else "OK")
        print(
            f"  [{marker:8}] {pair['test_id']}  "
            f"({int((time.monotonic()-t0)*1000)}ms, model={c.model}, "
            f"cache_hit={c.cache_hit}, body_chars={len(c.body)}, errs={len(c.validation_errors)})"
        )

    # Write JSONL atomically (write to .tmp then rename)
    tmp = out_file.with_suffix(out_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res["line"], ensure_ascii=False) + "\n")
    tmp.replace(out_file)

    elapsed = int(time.monotonic() - start)
    print()
    print(f"Wrote {len(results)} lines to {out_file} in {elapsed}s")

    # Summary stats
    n_fb = sum(1 for r in results if r["composed"].fallback_used)
    n_skip = sum(1 for r in results if r["composed"].is_skip())
    n_cache = sum(1 for r in results if r["composed"].cache_hit)
    avg_chars = sum(len(r["composed"].body) for r in results) / max(1, len(results))
    print(f"  fallbacks: {n_fb}  skips: {n_skip}  cache_hits: {n_cache}  avg_body_chars: {avg_chars:.0f}")

    if args.score:
        return await _score_results(results, holdout=args.holdout)

    return 0


async def _score_results(results: list[dict[str, Any]], *, holdout: bool) -> int:
    """Score the just-composed results via the same LLMScorer as judge_simulator.

    Used for the holdout overfit check (S19): if the holdout average is
    significantly below the 30-pair tuned average, prompts are overfit.
    """
    print()
    print("Scoring composed outputs via LLM judge…")

    # Re-use judge_simulator's scorer + provider creation
    sys.path.insert(0, str(ROOT / "scripts"))
    # Load env config (so judge knows which provider to use)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import os
    import judge_simulator as js

    provider = os.getenv("JUDGE_LLM_PROVIDER", "anthropic")
    js.LLM_PROVIDER = provider
    if provider == "anthropic":
        js.LLM_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "claude-sonnet-4-6")
    elif provider == "openai":
        js.LLM_API_KEY = os.getenv("OPENAI_API_KEY", "")
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "gpt-4o")
    else:
        js.LLM_API_KEY = os.getenv("LLM_API_KEY", "")

    if not js.LLM_API_KEY:
        print(f"  [SKIP] No API key for provider {provider!r}; cannot score offline.")
        return 0

    llm = js.create_provider()
    dataset = js.DatasetLoader(DATASET)
    if not dataset.load():
        print("  [ERROR] dataset load failed")
        return 1
    scorer = js.LLMScorer(llm, dataset)

    totals: list[int] = []
    for res in results:
        pair = res["pair"]
        merchant = dataset.merchants.get(pair["merchant_id"], {})
        trigger = dataset.triggers.get(pair["trigger_id"], {})
        customer = dataset.customers.get(pair.get("customer_id")) if pair.get("customer_id") else None
        category = dataset.categories.get(merchant.get("category_slug", ""), {})

        # Build an action-shaped dict for the scorer
        line = res["line"]
        action = {
            "body": line["body"],
            "cta": line["cta"],
            "send_as": line["send_as"],
            "rationale": line["rationale"],
        }
        score = scorer.score(action, category, merchant, trigger, customer)
        totals.append(score.total)
        print(f"  [{score.total:>2}/50] {pair['test_id']}  spec={score.specificity} cat={score.category_fit} "
              f"merch={score.merchant_fit} dec={score.decision_quality} eng={score.engagement_compulsion}")

    if totals:
        avg = sum(totals) / len(totals)
        print()
        print(f"AVERAGE: {avg:.1f}/50  ({len(totals)} pairs)")

        log_path = ROOT / "logs" / ("holdout_score.txt" if holdout else "submission_score.txt")
        log_path.write_text(
            f"avg={avg:.2f}\nn={len(totals)}\nscores={totals}\n",
            encoding="utf-8",
        )
        print(f"  → {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
