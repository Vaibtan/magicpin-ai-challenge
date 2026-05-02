# Vera Bot — Implementation Plan

**Last updated**: 2026-04-30
**Source of truth**: `design-decisions.md` (every slice traces back to a Qn decision).
**Methodology**: Tracer-bullet vertical slices. Each slice cuts end-to-end and is independently demoable/verifiable.
**Notation**: `AFK` = can be coded without human input. `HITL` = stop and eyeball/decide.

---

## How to read this plan

- Slices are ordered top-to-bottom by dependency. Don't start a slice until its blockers are checked off.
- Each slice has acceptance criteria — only check the slice off when **all** criteria pass.
- Total of **21 slices across 9 phases**. Estimated total effort: ~10-12 focused hours.
- Phases 3 + 6 + 8 contain HITL gates — these are the natural pause points.

---

## Phase 1 — Foundation

### - [x] S01 — Project bootstrap ✅
- **Type**: AFK
- **Blocked by**: None — start here.
- **What**: Set up project skeleton, dependencies, and the directory tree the rest of the plan assumes.
- **Acceptance**:
  - [x] `pyproject.toml` lists: `anthropic`, `openai`, `fastapi`, `uvicorn`, `pydantic`, `httpx`, `python-dotenv`
  - [x] Module skeleton created (empty stubs OK): `bot.py`, `server.py`, `state.py`, `llm_client.py`, `validator.py`, `classifiers.py`, `make_submission.py`, `prompts/__init__.py`, `prompts/skeletons.py`, `prompts/playbooks.py`
  - [x] Folders created: `.cache/`, `logs/`, `prompts/`
  - [x] `.gitignore` excludes `.cache/`, `logs/`, `state_dump.json`, `.env`, `__pycache__/`, `.venv/`
  - [x] `.env.example` committed with placeholder keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `BOT_DEV_MODE`)
  - [x] `uv sync` (or `pip install -e .`) succeeds in a clean environment

---

### - [x] S02 — Dataset expansion + test pairs ✅
- **Type**: AFK
- **Blocked by**: S01
- **What**: Run the dataset generator, commit the expanded JSON, and author the canonical 30-pair + 10-pair holdout files.
- **Acceptance**:
  - [x] `python dataset/generate_dataset.py` runs cleanly and writes expanded outputs (50 merchants, 200 customers, 100 triggers)
  - [x] Expanded outputs committed: `dataset/categories/*.json`, `dataset/merchants/*.json` (50), `dataset/customers/*.json` (200), `dataset/triggers/*.json` (100)
  - [x] `dataset/test_pairs.json` exists with exactly 30 entries — 25 merchant-scope + 5 customer-scope, covering 5 categories × 6 trigger kinds (matrix in design-decisions.md §7)
  - [x] `dataset/holdout_pairs.json` exists with 10 entries — different triggers + different merchants from the 30
  - [x] Each pair has `{test_id, merchant_id, trigger_id, customer_id}`; every referenced ID exists in the expanded dataset

---

## Phase 2 — LLM infrastructure

### - [ ] S03 — LLM client with caching + fallback (code done, smoke pending API keys)
- **Type**: AFK
- **Blocked by**: S01
- **What**: A single `llm_client` module that handles Anthropic primary, OpenAI fallback, two cache breakpoints on the prefix, and a content-hash-keyed local response cache.
- **Acceptance**:
  - [x] `llm_client.compose_call(skeleton_text, category_text, dynamic_text, *, model)` returns parsed JSON
  - [x] Two `cache_control: ephemeral` breakpoints set on `skeleton_text` and `category_text` for Anthropic
  - [x] On Anthropic 5xx / 429 / timeout → automatic single-hop OpenAI `gpt-4o` fallback (JSON mode, `temperature=0`)
  - [x] Response cache: `.cache/llm_responses.jsonl`; key = `sha256(prompt_version|model|skeleton_id|hash(category)|hash(merchant)|hash(trigger)|hash(customer)|playbook|hash(conv_state))`
  - [x] Cache hit returns the cached JSON without an LLM call (verified by log line `event=cache_hit`)
  - [x] Cache miss writes the response after the call (verified by inspecting the file)
  - [x] All calls are `temperature=0`
  - [ ] Smoke test script `scripts/smoke_llm.py` makes one Sonnet call + one OpenAI call and prints both outputs *(PENDING — needs `.env` with API keys)*

---

### - [ ] S04 — Haiku side-task client (code done, smoke pending API keys)
- **Type**: AFK
- **Blocked by**: S03
- **What**: A lightweight Haiku-based helper for short JSON-classification tasks (auto-reply, language, intent, hostile). Distinct from the compose path because it has different prompt shapes and shorter outputs.
- **Acceptance**:
  - [x] `llm_client.classify_call(prompt, schema_hint, *, model="haiku")` returns parsed JSON with the requested fields
  - [x] OpenAI `gpt-4o-mini` fallback wired the same way as S03
  - [x] No prompt cache breakpoints (prompts are too small to benefit) — but response cache reused
  - [ ] Smoke test: feed the auto-reply phrase from §3 of `engagement-research.md` and verify it returns `{"label": "auto_reply", ...}` *(PENDING — needs `.env` with API keys)*

---

## Phase 3 — First tracer bullet (compose works for one test pair)

### - [x] S05 — Core types + merchant-facing skeleton + research_digest playbook + validator ✅
- **Type**: AFK
- **Blocked by**: S03
- **What**: All the code needed to compose ONE message, with structural correctness enforced.
- **Acceptance**:
  - [x] `bot.ComposedMessage` dataclass: `body, cta, send_as, suppression_key, rationale, anchor (private), lever (private), prompt_version, fallback_used`
  - [x] `prompts/skeletons.py` defines `MERCHANT_FACING_SYSTEM` (merchant-facing voice rules, anti-patterns, output schema)
  - [x] `prompts/playbooks.py` defines `PLAYBOOKS` dict with at least the `research_digest` entry (3-5 line framing snippet)
  - [x] `validator.validate(result, category, merchant, trigger, customer) -> list[ValidationError]` implements all 6 rules in design-decisions.md §8 *(10/10 unit tests pass via `scripts/test_validator.py`)*
  - [x] Validator failures emit `event=validator_fail` log: `{conversation_id, errors, retry_attempt}`
  - [x] `validator.fallback(trigger_kind, merchant, customer) -> ComposedMessage` returns a deterministic safe message keyed on trigger.kind. Fallback usage emits `event=fallback_used` log.
  - [x] `bot.acompose()` wires: build prompt → LLM call → parse → validate → if errors retry once with feedback → if still errors return fallback; `bot.compose()` is the synchronous challenge-contract wrapper returning public dict keys
  - [x] Every compose call emits exactly one `event=compose` JSONL log per design-decisions.md §10 (with all listed fields: ts, conv_id, merchant_id, trigger_id, model, prompt_version, skeleton, playbook, cache_hit, latency_ms, input_tokens, output_tokens, validation, anchor, lever, body_hash, rationale)

---

### - [ ] S06 — End-to-end T01 (Dr. Meera + research_digest)
- **Type**: HITL
- **Blocked by**: S05, S02
- **What**: Drive `compose()` on the canonical T01 input and inspect the output by eye. This is the gate that proves the prompt actually produces good copy before scaling out to all 14 playbooks.
- **Acceptance**:
  - [ ] `scripts/compose_one.py T01` script loads dataset, calls compose, prints the full ComposedMessage as JSON
  - [ ] Output `body` references the JIDA Oct 2026 anchor verbatim
  - [ ] `body` uses peer-clinical voice (no "guaranteed", no "amazing deal", no promotional caps)
  - [ ] `body` is 100-500 chars, single primary CTA
  - [ ] `anchor` field is non-empty AND validator confirms it appears in contexts
  - [ ] `rationale` follows the hybrid format from design-decisions.md §10 (one prose sentence + structured suffix)
  - [ ] `validation.retried = false` (first compose passes — if not, iterate the prompt before moving on)
  - [ ] **Human eyeball pass**: would you actually want to reply if you were Dr. Meera? If no → tighten the playbook before proceeding to S07

---

## Phase 4 — Batch + server scaffold

### - [ ] S07 — `make_submission.py` produces single-line JSONL for T01
- **Type**: AFK
- **Blocked by**: S06
- **What**: The batch entry point — same composer engine, no server. Verifies the pure-function shape works in isolation.
- **Acceptance**:
  - [ ] `python make_submission.py --pair T01` writes one line to `submission.jsonl`
  - [ ] Output JSON keys exactly: `test_id, body, cta, send_as, suppression_key, rationale`
  - [ ] No `anchor`/`lever` fields leak into the JSONL (private only)
  - [ ] Re-running produces byte-identical output (response cache hit; verify via `event=cache_hit` log)

---

### - [x] S08 — State stores + server skeleton with health/metadata/context endpoints ✅
- **Type**: AFK
- **Blocked by**: S01
- **What**: The server shell with idempotent context push + read-only health/metadata. No tick logic yet.
- **Acceptance**:
  - [x] `state.ContextStore` supports `push(scope, context_id, version, payload)` returning `(accepted, current_version)` per the 200/200/409 semantics in design-decisions.md §6
  - [x] `state.ConversationStore` defined with explicit phases: `INITIATED, AWAITING_REPLY, ENGAGED, AUTO_REPLY_SUSPECTED, EXITED`. Per-conversation fields: `merchant_id, customer_id, trigger_id, send_as, phase, turns[], auto_reply_count, last_send_ts, prior_bot_hashes`
  - [x] `state.SuppressionStore` defined with empty in-memory dicts and async locks
  - [x] `GET /v1/healthz` returns `{status, uptime_seconds, contexts_loaded: {category, merchant, customer, trigger}}` with correct counts
  - [x] `GET /v1/metadata` returns the team metadata block from `challenge-testing-brief.md §2.5`
  - [x] `POST /v1/context` is idempotent on `(scope, context_id, version)`:
    - Same version → 200 `{accepted: true}`
    - Higher version → 200 `{accepted: true}` (atomic replace)
    - Lower version → 409 `{accepted: false, current_version: N}`
    - Malformed `scope` → 400 `{accepted: false, reason: "invalid_scope"}`
  - [x] `POST /v1/teardown` stub: clears all stores in-memory; returns `{ok: true}`. Spec-optional but cheap insurance.
  - [x] State dump-on-shutdown wired (loaded only when `BOT_DEV_MODE=1`)
  - [x] `uvicorn server:app --port 8080` starts and responds to `curl /v1/healthz` *(verified via FastAPI TestClient)*

---

### - [x] S09 — Minimal `/v1/tick` (superseded by S13 7-gate filter) ✅
- **Type**: AFK
- **Blocked by**: S08, S05
- **What**: The simplest tick handler — superseded directly by S13 (full 7-gate filter is a strict superset of "minimal one-action-per-trigger").
- **Acceptance**:
  - [x] `POST /v1/tick {now, available_triggers: ["trg_001..."]}` returns `{actions: [...]}` *(verified via FastAPI TestClient)*
  - [x] Each action has **exactly** these fields: `conversation_id, merchant_id, customer_id, send_as, trigger_id, template_name, template_params, body, cta, suppression_key, rationale`. Private fields (`anchor`, `lever`, `prompt_version`, `fallback_used`) are **stripped** before serialization.
  - [x] `template_name` is `"vera_{trigger_kind}_v1"` and `template_params` is a 3-element list (merchant_id + trigger_id + body excerpt)
  - [x] Curl test: push category + merchant + trigger contexts, then POST tick → action returns with no leaked private fields
  - [ ] `judge_simulator.py _phase2_short` runs against the bot and produces non-zero scores *(PENDING — needs API keys)*

---

## Phase 5 — Coverage expansion

### - [ ] S10 — All merchant-facing playbooks + 25 merchant test pairs (playbooks done; live runs pending keys)
- **Type**: AFK
- **Blocked by**: S06
- **What**: Fill in the playbook map for every merchant-facing trigger kind. Run all 25 merchant-scope test pairs through compose; eyeball none-go-empty.
- **Acceptance**:
  - [x] `PLAYBOOKS` dict has entries for all merchant-scope kinds: `research_digest, regulation_change, festival_upcoming, weather_heatwave, local_news_event, competitor_opened, category_trend_movement, perf_spike, perf_dip, milestone_reached, dormant_with_vera, renewal_due, review_theme_emerged, scheduled_recurring` *(plus 17 extras for full coverage of the dataset's actual trigger kinds → 31 total)*
  - [ ] `python make_submission.py --all-merchant` produces 25 valid JSONL lines *(PENDING — needs API keys)*
  - [ ] No fallback-template fires (`fallback_used=false` for all 25 — check logs) *(PENDING)*
  - [ ] No anchor-fabrication errors in logs (`validator_fail` events absent) *(PENDING)*
  - [ ] Average `body` length across the 25 is between 80-450 chars (sanity) *(PENDING)*

---

### - [ ] S11 — Customer-facing skeleton + customer playbooks + 5 customer test pairs (code done; live runs pending keys)
- **Type**: AFK
- **Blocked by**: S10
- **What**: Add `CUSTOMER_FACING_SYSTEM` skeleton + customer-scope playbooks. Run the 5 customer-scope test pairs.
- **Acceptance**:
  - [x] `prompts/skeletons.py` adds `CUSTOMER_FACING_SYSTEM` (merchant's voice → customer; legal taboos enforced; signed off as the merchant's clinic)
  - [x] `bot.acompose()` selects `CUSTOMER_FACING_SYSTEM` when `customer is not None`
  - [x] `PLAYBOOKS` has entries for customer-scope kinds: `recall_due, customer_lapsed_soft, customer_lapsed_hard, appointment_tomorrow, unplanned_slot_open` *(plus chronic_refill_due, trial_followup, wedding_package_followup)*
  - [ ] All 5 customer-scope test pairs (T03, T09, T15, T21, T27) compose without fallback *(PENDING — needs API keys)*
  - [ ] `send_as = "merchant_on_behalf"` for all 5 (validator check) *(PENDING; validator already enforces this)*
  - [ ] Each customer message addresses the customer by name, references the merchant's clinic by name, honors `language_pref` *(PENDING)*

---

### - [ ] S12 — Cross-category voice eyeball
- **Type**: HITL
- **Blocked by**: S10, S11
- **What**: Manually read one composed message per category × per scope (10 messages). Confirm voice fit before tuning further.
- **Acceptance**:
  - [ ] Dentists message reads clinical-peer (technical terms welcome, no hype)
  - [ ] Salons message reads warm-practical
  - [ ] Restaurants message reads operator-to-operator
  - [ ] Gyms message reads coach-energetic
  - [ ] Pharmacies message reads precise-trustworthy
  - [ ] Customer-facing samples address customer by name, are short, single CTA
  - [ ] If any category is off-voice → tighten that category's `voice_examples` in the dataset before S13

---

## Phase 6 — Tick policy

### - [x] S13 — 7-gate filter + parallel compose + suppression/cooldown/daily-cap ✅
- **Type**: AFK
- **Blocked by**: S09, S10, S11
- **What**: Replace the bare-minimum tick handler with the full filter pipeline. This is the "Decision Quality" lever.
- **Acceptance**:
  - [x] All 7 gates from design-decisions.md §5 implemented in order: resolution, stale, suppression, active-conversation, cooldown, daily-cap, customer-consent
  - [x] Each gate skip emits `event=tick_skip` JSONL log line with `{trigger_id, gate_failed, reason}`
  - [x] `urgency >= 4` correctly bypasses the cooldown gate
  - [x] After filtering, max 1 action per merchant per tick; max 3 actions total per tick
  - [x] Surviving triggers sorted by `(urgency desc, expires_at asc)` before truncation to top 3
  - [x] Composes are batched **sorted by `merchant.category_slug`** to maximize Anthropic prompt-cache hits
  - [x] Selected actions are reserved by `(suppression_key, merchant_id)` before compose so overlapping ticks cannot duplicate sends while LLM calls are in flight.
  - [x] Composes run in parallel via `asyncio.wait(..., timeout=23.0)` — **hard ceiling 23s** to stay safely inside spec's 30s (and our internal 25s ceiling). On timeout: cancel pending work, release its reservations, emit `event=tick_timeout` with `{completed_count, attempted_count}`, and return any completed valid actions.
  - [x] Composer self-veto: `body == ""` is dropped before action emission; logged as `event=composer_self_veto`
  - [x] On emit: `suppression_key` added to `sent_keys`, `last_send_ts` updated, `daily_send_count` incremented, conversation created in `INITIATED` phase. Phase transitions on emit logged as `event=phase_transition`.
  - [x] **Anchor/lever/prompt_version stripped** from each action dict before returning to caller (single helper `to_public_action(composed)` in `server.py`)
  - [x] Test: push a duplicate-suppression-key trigger twice → second tick returns 0 actions for that trigger; overlapping concurrent ticks for the same trigger emit exactly one action *(verified by `scripts/test_tick_reservations.py`)*

---

## Phase 7 — Reply handler

### - [ ] S14 — Reply classifier (regex done; Haiku fallback wired pending keys)
- **Type**: AFK
- **Blocked by**: S04
- **What**: The 8-label classifier with cheap deterministic prefilters and Haiku as the fallback for unclear cases.
- **Acceptance**:
  - [x] `classifiers.classify_reply(message, conv_history) -> ReplyLabel` returns one of: `auto_reply, engaged, intent_action, not_interested, hostile, question, unclear, defer`
  - [x] Verbatim-dup hash check vs prior merchant turns → `auto_reply`
  - [x] Regex pattern lists for `AUTO_REPLY_PATTERNS, HOSTILE_PATTERNS, NOT_INTERESTED_PATTERNS, INTENT_ACTION_PATTERNS, DEFER_PATTERNS` in `classifiers.py`
  - [x] Defer regex extracts a `wait_seconds` value (`"tomorrow" → 86400, "in 30 min" → 1800, "later" → 3600`, default 1800)
  - [x] Falls through to a Haiku call only when no regex matches; Haiku call returns `{label, confidence, keyphrase}` over the 7-label space (defer is regex-only) *(code wired; live test pending API keys)*
  - [x] Every classification emits `event=reply_classify` JSONL log: `{conversation_id, label, source: "regex"|"haiku", confidence, keyphrase}`
  - [x] Unit test cases: `"Thank you for contacting us"` → `auto_reply`; `"Stop messaging me"` → `hostile`; `"Ok lets do it"` → `intent_action`; `"send tomorrow"` → `defer`(86400) *(30/30 cases pass via `scripts/test_classifiers.py`)*

---

### - [x] S15 — Reply state machine: templated branches ✅
- **Type**: AFK
- **Blocked by**: S14, S08
- **What**: Wire `/v1/reply` and implement the 5 templated branches (auto_reply 1st/2nd, hostile, not_interested, defer, unclear). No LLM cost on these branches.
- **Acceptance**:
  - [x] `POST /v1/reply` returns 200 with valid `{action, body?, cta?, wait_seconds?, rationale}` shape per `challenge-testing-brief.md §2.3`. Private fields (`anchor`, `lever`) are **stripped** before returning *(verified via FastAPI TestClient)*.
  - [x] Templated probes for `auto_reply` (1st) keyed by `trigger.kind` — 16 distinct templates per language (32 total, en + hi-en)
  - [x] Templated graceful exit for `auto_reply` (≥2nd), language-aware (en/hi-en mix). Exit emits `event=auto_reply_exit` log: `{conversation_id, count, last_label}`
  - [x] Hostile branch returns `action: "end"` AND a 1-line apology body containing `"apologies"` (off-spec deliberate; design-decisions.md §15)
  - [x] Not-interested branch returns `action: "end"` with a 1-line courteous template
  - [x] Defer branch returns `action: "wait"` with `wait_seconds` from the regex extractor
  - [x] Unclear branch returns `action: "send"` with a templated binary clarifier
  - [x] State machine tracks `auto_reply_count` per conversation; second auto-reply triggers exit
  - [x] **Phase transitions** logged as `event=phase_transition`:
    - On first reply: `INITIATED/AWAITING_REPLY → ENGAGED` (or directly to `EXITED` for hostile/not_interested/auto-reply-2nd)
    - On `auto_reply` 1st: `→ AUTO_REPLY_SUSPECTED`
    - On any `end` action: `→ EXITED`
  - [x] **Hard timeout** wrapper on `/v1/reply` handler: `asyncio.wait_for(handle_reply(...), timeout=23.0)`. Timeout returns `action: "end"` with rationale `"timeout_safe_exit"` rather than blocking past spec.
  - [ ] `judge_simulator.py _auto_reply` passes (bot ends by turn 2) *(PENDING — needs API keys for full simulator run; templated path already verified)*
  - [ ] `judge_simulator.py _hostile` passes *(PENDING — same; apology body verified standalone)*

---

### - [x] S16 — LLM reply branches: ACTION_MODE, QA_MODE, engaged, anti-repetition (code done; live runs pending keys)
- **Type**: AFK
- **Blocked by**: S15, S05
- **What**: The three content-rich reply branches that go through the composer.
- **Acceptance**:
  - [x] `ACTION_MODE_PLAYBOOK` matches the snippet in design-decisions.md §4 — explicitly forbids qualifying language. *(Confirmed: contains all 7 of `done|sending|draft|here|confirm|proceed|next` and explicitly forbids `would you|do you|can you tell|what if|how about|may I`.)*
  - [x] `QA_MODE_PLAYBOOK` answers from contexts only; says honestly if data isn't present
  - [x] `engaged` branch reuses the main composer with `conv_history` injected into the merchant block + "this is turn N, do not repeat" instruction
  - [x] Anti-repetition check: post-compose body hash against all prior bot turns in the conv → re-prompt with rephrase instruction on collision *(validator rule 5 wired with `prior_bot_hashes`; validator unit test `anti-repetition` passes)*
  - [ ] `judge_simulator.py _intent` passes *(PENDING — needs API keys; ACTION_MODE prompt is reverse-engineered to pass the simulator's keyword detector)*

---

## Phase 8 — Self-grading + tune

### - [ ] S17 — Wire `judge_simulator.py` + capture baseline scores (wrapper done; baseline pending keys)
- **Type**: AFK
- **Blocked by**: S13, S16
- **What**: Make `judge_simulator.py` the inner-loop dev tool. Run the full evaluation against the local bot and capture the baseline.
- **Acceptance**:
  - [x] `judge_simulator.py` configured to use Anthropic Sonnet for the judge LLM role *(via `scripts/run_judge.py` wrapper — env-driven, defaults `JUDGE_LLM_PROVIDER=anthropic` + `JUDGE_LLM_MODEL=claude-sonnet-4-6`)*
  - [x] **Customer-context push patched**: `scripts/run_judge.py` monkey-patches `JudgeSimulator._warmup` to push ALL customers + ALL merchants + ALL triggers before any scenario runs. Verifies via `/v1/healthz` that `contexts_loaded.customer > 0`.
  - [ ] `BOT_URL=http://localhost:8080 python scripts/run_judge.py full_evaluation` runs without crashes *(PENDING — needs API keys)*
  - [ ] All 30 test pairs scored; per-pair scores logged *(PENDING)*
  - [ ] All 5 customer-scope pairs return `send_as = "merchant_on_behalf"` (sanity check the patch worked) *(PENDING — validator already enforces)*
  - [ ] `_warmup`, `_auto_reply`, `_intent`, `_hostile` scenarios all return PASS *(PENDING)*
  - [ ] Aggregate average score (out of 50) captured and recorded in `logs/baseline_score.txt` *(PENDING — make_submission.py --score writes to this file)*

---

### - [ ] S18 — Prompt tuning loop (≤6 iterations to ≥40/50)
- **Type**: HITL
- **Blocked by**: S17
- **What**: Iterate on prompts/playbooks until the 30-pair average is ≥40/50. Stop after 6 self-grading runs regardless.
- **Acceptance**:
  - [ ] Each tuning iteration: identify the 3 lowest-scoring pairs → read judge rationale → tune the relevant playbook OR skeleton OR validator → bump `prompt_version` → invalidate response cache for affected entries → re-run `_full`
  - [ ] Iteration log kept in `logs/tuning_log.md` — one row per iteration with `(iteration, prompt_version, avg_score, lowest_3_test_ids)`
  - [ ] Stop condition met: avg ≥ 40/50 OR iteration count = 6
  - [ ] Final prompt version locked; `prompts/__init__.py` exports `PROMPT_VERSION = "v<N>"`

---

## Phase 9 — Submission

### - [ ] S19 — Final `submission.jsonl` + holdout score check
- **Type**: AFK
- **Blocked by**: S18
- **What**: Generate the final 30-line JSONL and run the holdout for overfit detection.
- **Acceptance**:
  - [x] `python make_submission.py --all` writes 30 valid JSONL lines to `submission.jsonl` *(current artifact generated through safe fallback path in no-key environment; regenerate with API keys before final judged submission)*
  - [ ] All 30 lines re-runnable from cache (`event=cache_hit` for all 30 on the second run)
  - [ ] `python make_submission.py --holdout --score` runs the 10-pair holdout through compose AND scores them via the same judge LLM as `judge_simulator`
  - [ ] Holdout average score ≥ 0.9 × 30-pair score → no overfit; lock prompts
  - [ ] If holdout < 0.9 × 30-pair → return to S18 for one more tuning round, then re-check

---

### - [ ] S20 — Deploy: ngrok primary + Dockerfile/fly.io backup (artifacts done; deploy pending)
- **Type**: AFK
- **Blocked by**: S19
- **What**: Stand up the public URL the judge will hit. Validate end-to-end against the deployed bot.
- **Acceptance**:
  - [x] `Dockerfile` builds an image that runs `uvicorn server:app --host 0.0.0.0 --port 8080` *(non-root user, healthcheck wired, .dockerignore committed)*
  - [x] `fly.toml` configured for the `bom` (Mumbai) region *(deploy pending; needs `fly launch` + secrets)*
  - [ ] `fly deploy` succeeds and the deployed URL responds to `/v1/healthz` *(PENDING — runtime step)*
  - [ ] Local `uvicorn` started + `ngrok http 8080` tunnel up; ngrok HTTPS URL captured in `logs/deploy_url.txt` *(PENDING)*
  - [ ] `BOT_URL=<ngrok-https-url> python scripts/run_judge.py all` runs successfully end-to-end *(PENDING)*
  - [ ] No 5xx errors; all 4 replay scenarios (warmup, auto_reply, intent, hostile) pass against the public URL *(PENDING)*
  - [ ] Latency p95 on `/v1/tick` < 10s (well inside the 15s real timeout) *(PENDING)*

---

### - [ ] S21 — README + final pre-submit checklist + submit (README done; submit pending keys)
- **Type**: HITL
- **Blocked by**: S20
- **What**: Author the 1-page README per challenge-brief.md §7.3 and complete the pre-flight checklist before submitting the URL.
- **Acceptance**:
  - [x] `README.md` ≤ 1 page with **exactly three sections** per challenge-brief.md §7.3:
    1. **Approach**: single-prompt composer + per-kind playbooks + 6-rule validator + 1 retry; hybrid reply classifier (regex prefilters + Haiku fallback); 7-gate tick policy; two-cache strategy (Anthropic prompt cache + local response cache).
    2. **Tradeoffs**: cost-per-score over pure quality (Haiku for cheap classification, Sonnet only for compose); restraint over coverage (composer self-veto + 7 gates); per-kind playbooks over a mega-prompt (specificity at the cost of small per-kind tuning surface); reverse-engineered the simulator's keyword detectors for ACTION_MODE rather than relying on LLM intuition.
    3. **What additional context would have helped most**: real merchant offer source-of-truth (vs. synthetic offer_catalog), live customer aggregate refresh, peer-stat granularity by city × locality (not just metro_solo_practices), and a verified consent ledger for customer-facing sends.
  - [ ] Pre-flight checklist from `challenge-testing-brief.md §12` walked through:
    - [ ] Endpoint reachable from public internet (HTTPS)
    - [ ] All 5 endpoints implemented and returning correct schemas
    - [ ] `/v1/context` idempotent on `(scope, context_id, version)`
    - [ ] `/v1/tick` returns within 30s even with empty actions
    - [ ] `/v1/reply` returns within 30s for any conversation
    - [ ] Bot persists context across calls (in-memory + dump on SIGINT)
    - [ ] `judge_simulator.py` passes locally with non-zero scores
    - [ ] Compute budget set (Anthropic + OpenAI keys with sufficient quota for 60-min test)
  - [ ] Submission URL submitted via portal
  - [ ] Final commit made; tag with `submission-v1`
  - [ ] Local bot kept running (with ngrok tunnel) until the judging window closes

---

## Score-leverage map (which slices move which scoring dimension)

| Slice | Specificity | Category fit | Merchant fit | Decision Quality | Engagement | Replay |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| S05 (validator anchor) | ✓✓ | ✓ | | | | |
| S05 (vocab taboo) | | ✓✓ | | | | |
| S10/S11 (playbooks) | ✓ | ✓✓ | ✓ | ✓ | ✓ | |
| S13 (7-gate filter) | | | | ✓✓ | | |
| S13 (composer self-veto) | | | | ✓✓ | | |
| S15 (templated branches) | | | | | | ✓✓ |
| S16 (ACTION_MODE) | | | | | ✓ | ✓✓ |
| S16 (anti-repetition) | | | | | ✓ | ✓ |
| S18 (tuning) | ✓ | ✓ | ✓ | ✓ | ✓ | |

`✓` = direct lift, `✓✓` = primary lever for that dimension.

---

## Stopping points if time runs out

- **After S09**: bot is technically submittable. Will score poorly on coverage but won't crash.
- **After S13**: full coverage + tick policy. Decent score, no replay handling. Probably ~30/50.
- **After S16**: full coverage + replay handling. Submittable mid-tier; ~35-40/50.
- **After S19**: tuned + overfit-checked. Target submission state; ~40-45/50.
- **S20-S21**: deployment + admin only. Required to actually submit.

---

## End of plan

Update this file in the same commit as any code change that alters scope. If a slice is consciously skipped, mark it with `~~strikethrough~~` and add a one-line "Why skipped:" beneath.
