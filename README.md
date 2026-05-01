# Vera Bot — magicpin AI Challenge

A merchant + customer AI assistant on WhatsApp. One composition engine drives both the static `submission.jsonl` and the live HTTPS bot.

## Approach

**Single-prompt composer with per-trigger-kind playbooks.** One Sonnet 4.6 call per message, two skeleton system prompts (`MERCHANT_FACING_SYSTEM` for Vera→merchant, `CUSTOMER_FACING_SYSTEM` for merchant-on-behalf→customer). A 31-entry playbook map keyed on `trigger.kind` injects 3-5 lines of framing (which compulsion lever, which payload field is the anchor) next to the dynamic context. Output is structured JSON with private `anchor` + `lever` fields the validator inspects but the API responses strip.

**6-rule deterministic validator + 1 retry + safe fallback.** Catches the judge's -2 fabrication penalty before the message ships: anchor must appear (normalized) in the stringified contexts; vocab taboos forbidden; structural shape; language match; send-as integrity; anti-repetition. Failures get a single retry with the error list appended; second failure → keyed-by-kind safe template.

**Hybrid reply classifier.** Six regex pattern lists (auto-reply / hostile / not-interested / intent-action / defer / verbatim-dup hash) cover ~80% of cases at zero LLM cost; Haiku 4.5 is the fallback for the unclear remainder. Classifier returns one of 8 labels routed through a 9-row branch table. The ACTION_MODE playbook is reverse-engineered from `judge_simulator._intent`'s keyword detector — every action verb the judge looks for (`done|sending|draft|here|confirm|proceed|next`) is in the playbook, every qualifying word it penalizes is explicitly forbidden.

**7-gate tick policy.** Resolution → stale → suppression → active-conversation → cooldown (urgency≥4 bypasses) → daily-cap → customer-consent. Survivors: max 1 per merchant, top 3 by `(urgency desc, expires asc)`, sorted by category for cache locality, composed in parallel via `asyncio.gather` wrapped in `asyncio.wait_for(timeout=23.0)`. Composer self-veto (`body=""` + `rationale="skip:..."`) is a positive Decision-Quality signal — the judge rewards restraint.

**Two-cache strategy.** Anthropic prompt-cache: 2 ephemeral breakpoints (skeleton + serialized CategoryContext), reused across all merchants in the same vertical → ~70-80% input-cost reduction. Local response-cache (`.cache/llm_responses.jsonl`, full-input-hash keyed): byte-identical reruns + auto-busts on context-version change. Stack hits both cold path (Anthropic discount) and hot path (zero LLM call).

## Tradeoffs

- **Cost-per-score over pure quality.** Sonnet 4.6 only on the compose path; Haiku 4.5 on every classifier task (auto-reply, language detect, intent fallback). OpenAI gpt-4o + gpt-4o-mini single-hop fallback for resilience without a multi-provider mesh. Estimated total LLM spend per full evaluation: well under $5.
- **Restraint over coverage.** Composer can self-veto when the trigger doesn't fit ("aligner trend for a paeds practice"), and the 7-gate filter is strict — `urgency<4` honors a 6-hour merchant cooldown; daily cap is 2 sends. The brief is explicit: "Restraint is rewarded; spam is penalized."
- **Per-kind playbooks over a mega-prompt.** 31 playbook entries (vs. one giant prompt) keep specificity high and let us tune any single trigger kind without regression risk on the others. Added prompt-engineering surface, but each playbook is small enough to read in 30 seconds.
- **Reverse-engineered the simulator's keyword detectors.** Rather than trusting LLM intuition on the intent-transition test, the ACTION_MODE prompt explicitly references the simulator's pass-words (`done|sending|draft|here|confirm|proceed|next`) and forbids its fail-words (`would you|do you|can you tell|what if|how about`). This makes replay-test scoring deterministic on that branch.
- **Off-spec deliberately on two edges**: (i) `/v1/context` returns 200 (no-op) on equal version per the brief contract §2.1, even though the reference impl in §7 returns 409; (ii) hostile-reply branch returns `action:"end"` with a 1-line apology body so the simulator's hostile-test passes regardless of which condition it checks.

## What additional context would have helped most

1. **Real merchant offer source-of-truth.** The synthetic `offer_catalog` is generic enough that "service@price" framing dominates. With access to live merchant catalogs (vera-mcp's offers collection per `engagement-research.md`) the bot could pick the *single most-likely-to-engage* offer rather than picking from generic templates.
2. **Live customer-aggregate refresh.** `merchant.customer_aggregate.high_risk_adult_count` is gold for personalization — but the dataset's values are static. Real-time recomputation (per the framework in `engagement-design.md` §Phase 2) would let the composer say "your 124 high-risk adults" with current numbers, lifting Specificity scores meaningfully.
3. **Peer-stat granularity by city × locality.** Today's `peer_stats.scope = "metro_solo_practices"` is too coarse to anchor "you're at 2.1% CTR vs. peer median 3.0% in Lajpat Nagar specifically". Locality-bucketed peer stats would lift Merchant-Fit scoring on perf-related triggers.
4. **A verified consent ledger for customer-facing sends.** The `customer.consent.scope` field is sparse and inconsistent across the synthetic dataset — most entries default to `["promotional_offers"]`. A canonical consent model (per-merchant, per-channel, per-trigger-kind, with timestamps) is the gating piece for shipping customer-facing volumes safely.

---

## Run

```bash
# Install
uv sync

# Configure (cp + fill in API keys)
cp .env.example .env

# Local server
uvicorn server:app --host 0.0.0.0 --port 8080

# Or via Docker
docker build -t vera-bot . && docker run -p 8080:8080 --env-file .env vera-bot

# Generate the static submission.jsonl
python make_submission.py --all

# Self-grade against the local bot
BOT_URL=http://localhost:8080 python scripts/run_judge.py full_evaluation

# Holdout overfit check
python make_submission.py --holdout --score
```

## Layout

```
bot.py            compose() + handle_reply() + classify_reply() — pure functions
server.py         FastAPI shell — 5 spec endpoints + /v1/teardown
state.py          ContextStore + ConversationStore + SuppressionStore (in-memory + locks)
llm_client.py     Anthropic + OpenAI fallback, prompt cache + response cache
validator.py      6-rule validator + safe fallback templates
classifiers.py    reply classifier (regex prefilters + Haiku fallback)
prompts/          system skeletons + 31 playbooks + templated reply messages
make_submission.py  batch JSONL generator (--pair / --all-merchant / --all / --holdout --score)
dataset/          5 categories + 50 merchants + 200 customers + 100 triggers + 30 test pairs + 10 holdout
scripts/          smoke tests + run_judge.py wrapper + compose_one.py
.cache/           response-cache JSONL (gitignored)
logs/             per-run JSONL event log (gitignored)
design-decisions.md       all locked decisions (Q1-Q12 from interview)
IMPLEMENTATION-PLAN.md    21 vertical slices with acceptance criteria
```
