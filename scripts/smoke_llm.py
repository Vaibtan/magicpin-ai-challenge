"""Smoke test for llm_client: one Sonnet compose call + one Haiku classify call.

Usage:
    python scripts/smoke_llm.py                 # both providers
    python scripts/smoke_llm.py --openai-only   # force OpenAI fallback path
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Make the project importable when run as `python scripts/smoke_llm.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_client import classify_call, compose_call  # noqa: E402


SKELETON = """You are a test composer. You output one JSON object with exactly
these keys: {"body": str, "anchor": str, "lever": str}. Nothing else."""

CATEGORY_TEXT = """[CATEGORY: dentists]
voice: peer-clinical, technical vocab welcome.
peer_stat: avg_ctr 0.030.
"""

DYNAMIC_TEXT = """[MERCHANT: Dr. Test Clinic, Delhi]
[TRIGGER: research_digest, urgency=2]
anchor candidate: "JIDA Oct 2026 p.14"

Compose a one-line greeting + body that uses the anchor.
"""

CLASSIFY_PROMPT = """Classify the following merchant reply into one of:
auto_reply | engaged | intent_action | not_interested | hostile | question | unclear

Reply with JSON: {"label": "<label>", "confidence": 0.0-1.0}.

Merchant message: "Thank you for contacting us! Our team will respond shortly."
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openai-only", action="store_true",
                        help="Unset ANTHROPIC_API_KEY for this run to force fallback")
    args = parser.parse_args()

    if args.openai_only:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    print("=== compose_call (Sonnet primary) ===")
    result = await compose_call(
        SKELETON, CATEGORY_TEXT, DYNAMIC_TEXT,
        skeleton_id="smoke", category_id="smoke",
        prompt_version="smoke_v1",
        log_context={"smoke": True},
    )
    print(f"  model:       {result.model}")
    print(f"  cache_hit:   {result.cache_hit}")
    print(f"  fallback:    {result.fallback_used}")
    print(f"  latency_ms:  {result.latency_ms}")
    print(f"  tokens:      cached={result.input_tokens_cached} "
          f"uncached={result.input_tokens_uncached} out={result.output_tokens}")
    print(f"  json:        {json.dumps(result.json, ensure_ascii=False, indent=2)}")

    print()
    print("=== classify_call (Haiku primary) ===")
    classification = await classify_call(
        CLASSIFY_PROMPT,
        prompt_version="smoke_v1",
        log_context={"smoke": True},
    )
    print(f"  json: {json.dumps(classification, ensure_ascii=False, indent=2)}")

    print()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
