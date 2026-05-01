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
  1. Loads .env (so ANTHROPIC_API_KEY / OPENAI_API_KEY work without hardcoding).
  2. Configures judge_simulator's module-level constants:
       LLM_PROVIDER ← env (default 'anthropic')
       LLM_API_KEY  ← env (ANTHROPIC_API_KEY or OPENAI_API_KEY by provider)
       LLM_MODEL    ← env (default 'claude-sonnet-4-6')
       BOT_URL      ← env (default 'http://localhost:8080')
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

# Load .env BEFORE importing judge_simulator (its config is module-scope)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import judge_simulator as js  # noqa: E402


# ---- 1. Override config from env --------------------------------------------

PROVIDER = os.getenv("JUDGE_LLM_PROVIDER", "anthropic")
js.LLM_PROVIDER = PROVIDER

if PROVIDER == "anthropic":
    js.LLM_API_KEY = os.getenv("ANTHROPIC_API_KEY", "") or js.LLM_API_KEY
    js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "claude-sonnet-4-6") or js.LLM_MODEL
elif PROVIDER == "openai":
    js.LLM_API_KEY = os.getenv("OPENAI_API_KEY", "") or js.LLM_API_KEY
    js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "gpt-4o") or js.LLM_MODEL
elif PROVIDER == "gemini":
    js.LLM_API_KEY = os.getenv("GEMINI_API_KEY", "") or js.LLM_API_KEY
elif PROVIDER == "deepseek":
    js.LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "") or js.LLM_API_KEY
elif PROVIDER == "groq":
    js.LLM_API_KEY = os.getenv("GROQ_API_KEY", "") or js.LLM_API_KEY
elif PROVIDER == "openrouter":
    js.LLM_API_KEY = os.getenv("OPENROUTER_API_KEY", "") or js.LLM_API_KEY
# ollama needs no key

js.BOT_URL = os.getenv("BOT_URL", js.BOT_URL)

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

    # Push ALL merchants too (stock pushes only first 5 → most pairs unresolvable)
    js.print_section("FULL MERCHANT + CUSTOMER PUSH (run_judge.py patch)")
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
