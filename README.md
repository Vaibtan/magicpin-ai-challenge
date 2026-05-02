# Vera Bot - magicpin AI Challenge

## Approach

One composition engine powers both deliverables: a synchronous `bot.compose(...) -> dict` for the static submission contract and an async `acompose()` path used by the FastAPI bot and batch generator. The live bot exposes the five required `/v1/*` endpoints and stores pushed category, merchant, customer, and trigger contexts in memory with versioned idempotency.

The composer uses two skeleton prompts, merchant-facing and customer-facing, plus a per-`trigger.kind` playbook map. A deterministic validator checks structure, send-as integrity, taboo vocabulary, verifiable anchors, language fit, and anti-repetition. Reply handling uses regex prefilters for auto-replies, hostile replies, not-interested replies, explicit action intent, and defer requests, with a small classifier fallback only for unclear cases.

The tick loop uses a seven-gate policy: resolution, stale-with-grace, suppression, active conversation, cooldown, daily cap, and customer consent. Surviving triggers are capped to one per merchant and three per tick, then composed in parallel. The project is run with `uv`.

## Tradeoffs

The bot optimizes cost-per-score, not maximum model spend: Sonnet is used for composition, Haiku for cheap classification, and OpenAI is the single fallback. The default LLM timeout is short enough to leave room for fallback inside the judge's 30s budget.

The validator is intentionally strict on fabricated anchors because one invented citation can destroy a score. If validation fails twice, the bot emits a safe deterministic fallback rather than timing out or returning malformed JSON. That protects operations, but fallback quality is lower than live LLM output.

The consent gate is conservative but maps the dataset's real consent vocabulary, such as `winback_offers`, `refill_reminders`, `recall_alerts`, and `promotional_offers`, to customer trigger kinds. This preserves customer-facing coverage without ignoring consent.

## What Additional Context Would Have Helped Most

Real merchant offer source-of-truth would let the composer choose the strongest service-at-price hook instead of relying on synthetic catalog entries.

Fresh customer aggregates and per-customer consent ledgers would make customer-facing sends safer and more specific.

Peer stats by city and locality would improve performance-trigger messages, especially CTR and review comparisons.
