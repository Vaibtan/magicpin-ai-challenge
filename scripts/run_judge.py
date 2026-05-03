"""Wrapper around judge_simulator.py — handles env-config + the customer-
context push patch (closes the judge_simulator gap noted in design-decisions.md
§13 / IMPLEMENTATION-PLAN S17).

Why a wrapper, not edits to judge_simulator.py:
  - judge_simulator.py is the magicpin-supplied harness. Keeping it pristine
    means we can drop in a newer version without losing our patches.
  - The customer-push fix is a coverage gap — without it our 5 customer-scope
    test pairs (T03/T09/T15/T21/T27) are scored against `customer=None` and
    tank, and the simulator never tests our customer-facing skeleton.

What this wrapper does:
  1. Loads .env so provider keys work without hardcoding.
  2. Configures judge_simulator's module-level provider/model/BOT_URL constants
     from JUDGE_LLM_PROVIDER, JUDGE_LLM_MODEL, and provider API key env vars.
  3. Monkey-patches JudgeSimulator._warmup to also push ALL customer contexts.
  4. Delegates to judge_simulator.main() for the requested scenario.

Usage:
    python scripts/run_judge.py                         # runs TEST_SCENARIO=all
    python scripts/run_judge.py full_evaluation         # runs _full
    python scripts/run_judge.py auto_reply_hell         # specific scenario
    BOT_URL=https://my-tunnel.ngrok-free.app python scripts/run_judge.py all
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.judge_provider_overrides import (  # noqa: E402
    configure_judge_from_env,
    configure_utf8_stdio,
    patch_judge_simulator,
)

configure_utf8_stdio()

# Load .env BEFORE importing judge_simulator (its config is module-scope)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import judge_simulator as js  # noqa: E402


patch_judge_simulator(js)
configure_judge_from_env(js, default_gemini_model="gemini-2.5-pro")

# Pick scenario from positional arg (preferred) or env (fallback)
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    js.TEST_SCENARIO = sys.argv[1]
else:
    js.TEST_SCENARIO = os.getenv("TEST_SCENARIO", js.TEST_SCENARIO)


# ---- 2. Customer-context push patch -----------------------------------------
# Closes the judge_simulator gap: the stock _warmup pushes only categories +
# first-5 merchants. Our 5 customer-scope test pairs need their CustomerContext
# in the bot's store before /v1/tick will route them through CUSTOMER_FACING_SYSTEM.

_orig_warmup = js.JudgeSimulator._warmup


def _patched_warmup(self) -> bool:
    if not _orig_warmup(self):
        return False

    js.print_section("FULL MERCHANT + CUSTOMER PUSH (run_judge.py patch)")

    # Re-push ALL categories too. The stock warmup already does this, but local
    # uvicorn --reload can restart between warmup and this patch while editing
    # files, wiping in-memory contexts.
    cat_pushed = 0
    for slug, cat in self.dataset.categories.items():
        data, err, _ = self.client.push_context("category", slug, 1, cat)
        if data and data.get("accepted"):
            cat_pushed += 1
    js.print_success(f"categories pushed: {cat_pushed}/{len(self.dataset.categories)}")

    # Push ALL merchants too (stock pushes only first 5 → most pairs unresolvable)
    m_pushed = 0
    for mid, m in self.dataset.merchants.items():
        data, err, _ = self.client.push_context("merchant", mid, 1, m)
        if data and data.get("accepted"):
            m_pushed += 1
    js.print_success(f"merchants pushed: {m_pushed}/{len(self.dataset.merchants)}")

    # Push ALL customers (stock skips this entirely)
    c_pushed = 0
    for cid, c in self.dataset.customers.items():
        data, err, _ = self.client.push_context("customer", cid, 1, c)
        if data and data.get("accepted"):
            c_pushed += 1
    js.print_success(f"customers pushed: {c_pushed}/{len(self.dataset.customers)}")

    # Push ALL triggers (stock _full pushes them later; we push here for replay scenarios too)
    t_pushed = 0
    for tid, t in self.dataset.triggers.items():
        data, err, _ = self.client.push_context("trigger", tid, 1, t)
        if data and data.get("accepted"):
            t_pushed += 1
    js.print_success(f"triggers pushed: {t_pushed}/{len(self.dataset.triggers)}")

    # Verify via /v1/healthz
    data, err, _ = self.client.healthz()
    if data:
        loaded = data.get("contexts_loaded", {})
        js.print_info(f"contexts_loaded after full push: {loaded}")
        if loaded.get("customer", 0) == 0:
            js.print_warn("customer count is 0 — customer-scope pairs will route to merchant-facing skeleton")
    return True


js.JudgeSimulator._warmup = _patched_warmup


# ---- 3. Delegate ------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(js.main())
