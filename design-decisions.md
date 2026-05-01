# Vera Bot — Design Decisions

**Last updated**: 2026-04-30
**Status**: Locked. Implementation reference + source-of-truth for the submission README.
**Scope**: All architectural and policy choices for the magicpin AI Challenge — Vera bot.

> Each section is keyed to the interview question (Q1-Q12) where the choice was made. "Why" lines capture the reason, so future-us (or a reviewer) can judge edge cases without re-litigating.

---

## 1. Deliverable scope (Q1)

- Ship **both** artifacts: a static `submission.jsonl` (30 composed messages) **and** a live HTTPS bot exposing the 5-endpoint API.
- One composition engine powers both. The composer is a pure function; the server and the batch script are thin shells around it.

**Why**: The two briefs describe different things (`challenge-brief.md` says JSONL, `challenge-testing-brief.md` says HTTP server). Building one engine + two presentations means no duplicated prompt logic and the JSONL is by-construction a deterministic snapshot of what the live bot would do.

---

## 2. LLM stack (Q2)

| Role | Model | Mode |
|---|---|---|
| Composer | Anthropic `claude-sonnet-4-6` | `temperature=0`, JSON output |
| Side-tasks (auto-reply detect, language detect, intent classify, hostile detect) | Anthropic `claude-haiku-4-5-20251001` | `temperature=0`, JSON output |
| Fallback (Anthropic outage / rate limit) | OpenAI `gpt-4o` | `temperature=0`, JSON mode |

- Optimization target: **best-score-per-dollar**, not pure quality and not pure cost.

**Why**: Sonnet 4.6 is the cost/quality sweet spot for nuanced voice + Hindi-English code-mix. Haiku 4.5 handles the high-volume cheap-classification path (auto-reply, language) at ~10× lower cost than running Sonnet there. Single fallback hop (OpenAI) keeps reliability without a multi-provider mesh.

---

## 3. Composition architecture (Q3)

- **Single Sonnet call per compose**. No two-stage planner.
- **Two skeleton system prompts**, picked by whether `customer` is `None`:
  - `MERCHANT_FACING_SYSTEM` (Vera's voice → merchant)
  - `CUSTOMER_FACING_SYSTEM` (merchant's voice → customer, drafted by Vera)
- **Playbook map** keyed on `trigger.kind`. ~14 entries:
  - External: `research_digest`, `regulation_change`, `festival`, `weather_heatwave`, `local_news_event`, `competitor_opened`, `category_trend_movement`
  - Internal: `perf_spike`, `perf_dip`, `milestone_reached`, `dormant_with_vera`, `renewal_due`, `review_theme_emerged`, `scheduled_recurring`
  - Customer-scope: `recall_due`, `customer_lapsed_soft`, `customer_lapsed_hard`, `appointment_tomorrow`, `unplanned_slot_open`
  - Reply-handling: `ACTION_MODE`, `QA_MODE`
- Each playbook is a 3-5 line framing snippet (which compulsion lever, which payload field is the anchor) injected next to the dynamic context, not a separate prompt.
- **Output is structured JSON**: `body, cta, suppression_key, send_as, rationale, anchor (private), lever (private)`.
- `rationale` doubles as chain-of-thought — judge reads it (free CoT), so model is told to think aloud there.
- Validator runs after compose; one re-prompt on hard fail; deterministic safe fallback if both fail.

**Why**: A single well-prompted Sonnet call with explicit structure beats a two-stage pipeline on cost (1 call instead of 2) without losing meaningful quality once `rationale` is doing CoT duty. Per-kind playbooks keep specificity high without splitting the prompt machinery.

---

## 4. Conversation control / reply state machine (Q4 + Q10)

### Classification — hybrid

Deterministic prefilters first (cheap, deterministic), Haiku fallback only on unclear cases:

1. Verbatim-dup hash check vs. all prior merchant turns in this conv → `auto_reply`
2. `AUTO_REPLY_PATTERNS` regex (e.g., "thank you for contacting", "we will get back", "automated assistant") → `auto_reply`
3. `HOSTILE_PATTERNS` regex (e.g., "stop messaging", "don't message", "spam") → `hostile`
4. `NOT_INTERESTED_PATTERNS` regex → `not_interested`
5. `INTENT_ACTION_PATTERNS` regex (e.g., "ok lets do it", "haan kar do", "go ahead", "yes please") → `intent_action`
6. `DEFER_PATTERNS` regex with duration extraction → `defer`
7. **No fast-match → Haiku call** classifying into 7 labels with confidence + extracted keyphrase.

### 9-row branch table

| Label | Action | Body source |
|---|---|---|
| `auto_reply` (1st) | `send` | Templated probe by trigger.kind (5 templates, owner-name + 1 anchor interpolated) |
| `auto_reply` (≥2nd) | `end` | Templated graceful exit, language-aware |
| `hostile` | `end` | Body included with apology keyword (off-spec deliberate; see §15) |
| `not_interested` | `end` | Templated 1-line ("Theek hai, samajh gayi. Best wishes!") |
| `defer` | `wait` | `wait_seconds` parsed from message (regex-mapped phrases, default 1800) |
| `intent_action` | `send` | Sonnet w/ `ACTION_MODE` playbook — concrete next step, no qualifying questions |
| `question` | `send` | Sonnet w/ `QA_MODE` playbook — answer from contexts only, honest "I don't have that" if missing |
| `engaged` | `send` | Main composer reused, conv_history injected into merchant block, "this is turn N, do not repeat" instruction |
| `unclear` | `send` | Templated binary clarifier (yes/no) |

### Auto-reply policy

- **One polite probe then exit.** First detection → templated probe. Second detection → graceful exit.
- Why one probe: Brief Pattern B holds it up as the gold-standard exit; preserves engagement when merchant is *legitimately* using a thank-you message and meant to engage afterward.

### ACTION-MODE playbook (the high-stakes one)

Reverse-engineered from `judge_simulator._intent` keyword detector:

```
ACTION_MODE = """
The merchant just signaled commitment ("ok lets do it" / "go ahead" / "haan kar do" / "yes please").

You MUST:
  - Open with confirmation language: "Done", "Sending now", "Drafted X — see below", "Pulling Y for you".
  - State the concrete next step you are taking (or already took) using the merchant's data.
  - End with a single low-friction confirmation ask, NOT a qualifying question.
  - Do NOT use: "would you like", "do you want", "what if", "have you considered", "can you tell me".

The previous Vera turn already pitched. Do not re-pitch. Action.
"""
```

### Anti-repetition + cadence

- Hash composed body; collision against any prior bot turn in this conv → re-prompt with "you already sent this; rephrase" instruction.
- Open-conversation gate (see §5 gate 4) blocks new triggers for that merchant. No proactive re-nudge inside an open conv. Conv expires silently with the trigger.

### Conversation phase state machine

States (in `ConversationStore`):
- `INITIATED` — bot just sent the first turn; no merchant reply yet
- `AWAITING_REPLY` — synonymous with INITIATED post-tick; held while waiting
- `ENGAGED` — merchant replied with `engaged`/`intent_action`/`question`/`unclear`/`defer`
- `AUTO_REPLY_SUSPECTED` — first auto-reply detected; one probe sent
- `EXITED` — terminal; no further sends in this conv

Transitions:
- (none) → `INITIATED` on tick emit
- `INITIATED`/`AWAITING_REPLY` → `ENGAGED` on `engaged`/`intent_action`/`question`/`unclear`/`defer` reply
- `INITIATED`/`AWAITING_REPLY`/`ENGAGED` → `AUTO_REPLY_SUSPECTED` on first `auto_reply` reply
- `AUTO_REPLY_SUSPECTED` → `EXITED` on second `auto_reply` reply
- any → `EXITED` on `hostile` or `not_interested` reply
- any → `EXITED` on `/v1/reply` returning `action: "end"`

Every transition emits `event=phase_transition` to the JSONL log: `{conversation_id, from, to, trigger_label}`.

**Why**: Most replies are unambiguous; running Haiku on every single one is overkill. The fast prefilters cover ~80% of cases for free. ACTION-MODE is given a dedicated playbook because it is *the* failure mode in the brief (Pattern D) and the simulator has an explicit detector for it. Explicit phase transitions make conversation flow auditable post-run.

---

## 5. Tick policy (Q5)

### 7-gate filter pipeline (first-fail wins)

Per `available_trigger`:

1. **Resolution** — trigger / merchant / category all known in store
2. **Stale** — `now > trigger.expires_at`
3. **Suppression** — `suppression_key` already in `sent_keys`
4. **Active-conversation** — open conv (`INITIATED`/`AWAITING_REPLY`/`ENGAGED`) with this merchant
5. **Cooldown** — last send to this `merchant_id` within last 6 simulated hours, **unless** `urgency >= 4` (renewals, perf cliffs, compliance deadlines override)
6. **Daily cap** — already sent ≥ 2 actions to this merchant today
7. **Customer consent** (only when `scope == "customer"`) — `consent.scope` doesn't include this kind, OR `state == "churned"`, OR `reminder_opt_in == false`

### Selection from survivors

- Group by `merchant_id`; **max 1 action per merchant per tick** (FAQ-mandated).
- Cap total at **3 actions per tick** (latency budget; 3 parallel Sonnet calls fit in 15s p95).
- Sort by `(urgency desc, expires_at asc)`, take top 3.
- **Compose in parallel** via `asyncio.gather`.

### Composer self-veto

- Composer is told it may return `body=""` + `rationale="skip: <reason>"` if the trigger genuinely doesn't fit this merchant. Validator drops empty bodies before they reach the judge.
- This is where "Decision Quality" gets scored well — actively choosing not to send weak matches.

### Hard timeout safety net

- The parallel `asyncio.gather` over Sonnet calls is wrapped in `asyncio.wait_for(..., timeout=25.0)`.
- 25s is a safety margin under the spec's 30s; protects against a single hung Sonnet call timing out the whole tick (judge penalty: -1 per timeout).
- On timeout: return whatever finished + log `event=tick_timeout`. Never block past 25s.
- `/v1/reply` gets the same wrapper — if classify+compose+validate exceeds 25s, return `action: "end"` with rationale `"timeout_safe_exit"` rather than blocking.

### State updates on emit

- Add `suppression_key` to `sent_keys` set
- Update `last_send_ts[merchant_id] = now`
- Increment `daily_send_count[merchant_id, date]`
- Create new conversation in `INITIATED` phase at `conversation_id = "conv_{merchant_id}_{trigger_id}"`

### API response stripping

Before returning to the caller, every action dict (in `/v1/tick` `actions[]`) and every reply dict (in `/v1/reply` response) is run through a single helper `to_public_action(composed)`. The helper drops the private fields: `anchor`, `lever`, `prompt_version`, `fallback_used`. These remain in the JSONL log for our own analysis. Same helper used by `make_submission.py`.

**Why**: Brief explicitly states "Restraint is rewarded; spam is penalized." The 7 gates are a strict spam filter; the cooldown carve-out for `urgency>=4` ensures high-value triggers (compliance, renewal) still fire when it matters; composer self-veto handles the long tail of "all gates passed but it's still a bad message."

---

## 6. State management & module split (Q6)

### Storage

- **In-memory dicts** for the live runtime.
- **`state_dump.json` snapshot** on graceful shutdown; auto-loaded on startup **iff** `BOT_DEV_MODE=1`. Judge run never sets that env var → starts empty as spec requires.
- Concurrency: single `asyncio.Lock` per store for writes; lock-free reads (CPython dict reads are atomic).

### Module layout

```
bot.py             # Pure compose() + classify_reply() + handle_reply().
                   # No HTTP, no globals, no I/O. Stateless functions.
state.py           # ContextStore (idempotent push), ConversationStore, SuppressionStore.
server.py          # FastAPI shell — 5 endpoints, ~150 lines glue.
make_submission.py # Standalone batch JSONL generator. Reads dataset JSON directly.
prompts/           # System prompts + playbook map. Versioned by file commit.
                   # Every rationale records prompt_version for traceability.
llm_client.py      # Anthropic + OpenAI fallback, cache breakpoints, response cache.
validator.py       # 6-rule deterministic validator + fallback templates.
classifiers.py     # Reply-classifier prefilters + Haiku fallback.
logs/              # Runtime JSONL event logs.
.cache/            # llm_responses.jsonl (gitignored).
```

### `/v1/context` idempotency contract

- Same `version` for same `(scope, context_id)` → **200, `accepted: true`** (no-op).
- Higher `version` → **200, `accepted: true`** (atomic replace).
- Lower `version` → **409, `accepted: false`, `current_version: N`**.
- Malformed `scope` (not in `{category, merchant, customer, trigger}`) → **400, `accepted: false, reason: "invalid_scope"`**.

> Note: brief's reference implementation (testing brief §7) returns 409 on equal version. This is wrong by the brief's own contract spec in §2.1 ("Re-posting the same version is a no-op"). We follow the contract.

### `/v1/teardown` (optional, per testing brief §11)

- Stub that clears all in-memory stores and returns `{ok: true}`.
- Not required by spec, but cheap insurance against the judge calling it and getting a 404.

**Why**: Pure-function `bot.py` means we can unit-test compositions without booting a server, and `make_submission.py` doesn't need the server running. `server.py` is small enough to swap frameworks if needed without touching the LLM code.

---

## 7. Submission test pairs (Q7)

- **Hand-picked 30-pair coverage matrix**, committed at `dataset/test_pairs.json`:
  - 5 categories × 6 trigger kinds = 30
  - **25 merchant-scope + 5 customer-scope** (matches judge §3 Phase 3 expected distribution)
  - Mix of urgencies (1-5), sources (external/internal)
  - Each pair pinned by explicit `merchant_id` + `trigger_id` for diff-stable iteration

| Category | research_digest | perf_dip/spike | regulation/competitor | festival/weather | customer-scope | renewal/milestone/review |
|---|---|---|---|---|---|---|
| dentists | T01 | T02 | T05 | T06 | T03 (recall) | T04 (renewal) |
| salons | T07 | T08 | T12 | T11 | T09 (lapsed) | T10 (milestone) |
| restaurants | T13 | T14 | T17 | T18 | T15 (appt) | T16 (review_theme) |
| gyms | T19 | T20 | T23 | T24 | T21 (lapsed) | T22 (perf_spike) |
| pharmacies | T25 | T26 | T29 | T28 | T27 (recall) | T30 (regulation) |

- **10-pair holdout set** at `dataset/holdout_pairs.json`. Different triggers, different merchants. Used **once** post-prompt-lockdown to detect overfitting. If holdout-avg < 0.9 × 30-pair-avg → prompts overfit; revisit.

**Why**: Hand-picked > random because every trigger kind we *don't* include is a kind we won't have tuned for when the judge surprises us. Holdout prevents the team-favorite-gotcha-prompt from passing the canonical set while regressing on real diversity.

---

## 8. Validator (Q8)

### 6-rule deterministic validator

Runs after every compose (and reply) call:

1. **Structural**:
   - body length 20-1000 chars
   - cta in `{open_ended, binary, none}`
   - suppression_key non-empty
2. **Anchor verifiability** (the big one):
   - `anchor` field must appear (normalized: lowercase, strip punct) in stringified union of category + merchant + trigger + customer payloads
   - Fabricated anchor → fail with error `"anchor_fabricated: '<value>'"`
3. **Vocab taboo**: substring scan over `body.lower()` for any word in `category.voice.vocab_taboo`
4. **Language match**:
   - Cheap regex/Devanagari/Hinglish heuristic first
   - Haiku call **only** when regex confidence < 0.8
   - Fail if detected language not in `merchant.identity.languages`
5. **Anti-repetition** (reply branch only): body hash collision against prior bot turns
6. **Send-as integrity**: `customer is None` ↔ `send_as=vera`; `customer` present ↔ `send_as=merchant_on_behalf`

### Re-prompt policy

- Errors found → **single retry** with the error list appended: *"Your previous output had these issues: [...]. Use only facts from the provided contexts. Compose again."*
- Second failure → **deterministic safe fallback** keyed on `trigger.kind` (1-line templates using guaranteed-present merchant identity fields). Logged as `fallback_used=true`.

### `anchor` field rules

- **Mandatory** for kinds with payload data: `research_digest, perf_dip, perf_spike, recall_due, renewal_due, regulation_change, milestone_reached, review_theme_emerged, competitor_opened, customer_lapsed_*, appointment_tomorrow, unplanned_slot_open, category_trend_movement, local_news_event`.
- **Optional** for strained kinds: `festival, weather_heatwave, dormant_with_vera, scheduled_recurring`. Validator branches on `trigger.kind`.
- Stripped before returning to the judge — internal validation artifact only.

**Why**: Judge has explicit -2 penalty per fabrication. One hallucinated citation costs more than the entire validator's compute. Single retry is cheap (one extra Sonnet call max) and only fires on actual failures, not every compose. Fallback prevents catastrophic empty-body returns.

---

## 9. Caching (Q9)

### Anthropic prompt-cache (provider-side, 5-min TTL, ~90% read discount)

Each Sonnet request has 4 ordered content blocks, **2 cache breakpoints**:

```
[
  {SYSTEM_PROMPT_FOR_SKELETON,  cache_control: ephemeral},  # ~1.5k tokens, stable
  {CATEGORY_CONTEXT_SERIALIZED, cache_control: ephemeral},  # ~2-4k tokens, stable per vertical
  {MERCHANT + TRIGGER + CUSTOMER + PLAYBOOK},                # ~1.5-2.5k tokens, varies per call
  # No cache_control on the dynamic block.
]
```

- **Tick batching by category**: when multiple actions in one tick, sort by `merchant.category_slug` so consecutive Sonnet calls re-hit the same category cache.
- `make_submission.py` processes 30 pairs in category-sorted order.
- 5-min TTL is naturally refreshed by tick cadence (every ~5 sim min ≈ ~3-5 real min). No manual keep-alive.

### Local response-cache (always-on, gitignored at `.cache/llm_responses.jsonl`)

```
cache_key = sha256(
  prompt_version
  + model_name
  + skeleton_id              # "merchant_facing" | "customer_facing"
  + hash(category_payload)
  + hash(merchant_payload)
  + hash(trigger_payload)
  + hash(customer_payload or "")
  + playbook_kind
  + hash(conversation_state) # for /v1/reply only
)
```

- **Hit**: return cached JSON. Zero LLM call. Byte-identical.
- **Miss**: call LLM, persist to cache before returning.
- **Auto-busts on context version change** (judge mid-test push) because payload hash changes → fresh compose. Anti-stale by construction.
- **Always-on** in dev, judge run, and `make_submission.py`.

### Determinism guarantees

- `temperature=0` everywhere
- Response cache → byte-identical reruns even if Anthropic ships a silent fix
- Cache key changes the moment a single byte of input changes → never returns stale messages under updated context

**Why**: User asked to optimize cost-per-score. Caching is the biggest lever. The two caches stack: prompt-cache cuts cold-path cost; response-cache cuts hot-path call entirely. Together: ~70-80% input-cost reduction across a full run, plus determinism that the brief implicitly requires.

---

## 10. Rationale + observability (Q11)

### Public rationale (judge-facing) — hybrid prose + structured suffix

```
"<one human sentence on why now and what it should achieve>. 
[anchor=<X>, lever=<curiosity|loss_aversion|social_proof|reciprocity|effort_externalization|specificity|asking|binary_commitment>, 
trigger=<kind>:u<urgency>, send_as=<vera|merchant_on_behalf>, prompt_v=<vN>]"
```

Examples:

```
"Dr. Meera's high-risk-adult cohort (124 patients) is the natural audience for the JIDA Oct
fluoride-recall finding; offering to do the patient-ed draft is low-friction.
[anchor=JIDA Oct 2026 p.14, lever=reciprocity+curiosity, trigger=research_digest:u2,
send_as=vera, prompt_v=v1]"

"5-month lapse on Priya's cleaning recall — slot offer with binary CTA matches her weekday-evening
preference.
[anchor=Wed 5 Nov 6pm, lever=loss_aversion, trigger=recall_due:u3,
send_as=merchant_on_behalf, prompt_v=v1]"
```

Both `anchor` and `lever` come from the composer's structured output — same fields the validator already uses. No extra LLM call.

### Private observability (`logs/run_{run_id}.jsonl`)

One JSON line per significant event. Greppable, parseable.

**Event types**: `compose`, `tick_skip`, `reply_classify`, `validator_fail`, `fallback_used`, `auto_reply_exit`, `cache_hit`, `cache_miss`.

**Common fields**:
```json
{
  "ts": "2026-04-26T10:30:01.234Z",
  "event": "compose",
  "conversation_id": "...",
  "merchant_id": "...",
  "trigger_id": "...",
  "model": "claude-sonnet-4-6",
  "prompt_version": "v1",
  "skeleton": "merchant_facing",
  "playbook": "research_digest",
  "cache_hit": false,
  "latency_ms": 3214,
  "input_tokens": {"cached": 4200, "uncached": 1100},
  "output_tokens": 320,
  "validation": {"errors": [], "retried": false, "fallback": false},
  "anchor": "JIDA Oct 2026 p.14",
  "lever": "reciprocity+curiosity",
  "body_hash": "sha256:abc123...",
  "rationale": "..."
}
```

Workflows powered by these logs:
1. Live debugging during dev: `tail -f logs/*.jsonl | jq`
2. Post-self-grade analysis: cross-reference judge scores with our logs to find score-killer patterns
3. Post-judge-run forensics: when team-results bundle returns, reconstruct exactly what happened

**Why**: Rationale is in the rubric (testing brief §14 FAQ). Hybrid format gives the judge a human-readable narrative AND machine-parseable metadata for "did rationale match output?" checks. Structured logs are cheap insurance — ~50 lines of code, massive debugging payoff.

---

## 11. Dataset generation (Q12a)

- **Run** `dataset/generate_dataset.py` once. Commit the expanded outputs (50 merchants, 200 customers, 100 triggers).
- Seeds (`merchants_seed.json`, etc.) remain authoring source; expansion is a committed build artifact.

**Why**: Seed has 10 merchants / 25 triggers — borderline for the 30-pair coverage matrix and tight for the 10-pair holdout. Expanded set gives diversity for replay testing.

---

## 12. Deployment (Q12b)

- **Primary**: ngrok tunnel from local uvicorn → submitted as the bot URL.
  - `uvicorn server:app --host 0.0.0.0 --port 8080`
  - `ngrok http 8080` → submit the HTTPS URL.
  - Free tier handles judge load (~3,600 reqs over 60 min ≪ ngrok limit).
- **Backup**: Dockerfile + fly.io setup. Built second; used only if ngrok flakes during judging window.

**Why**: ngrok is 5-min setup and matches the judge spec ("Any cloud, ngrok tunnel, any hosting"). Fly.io as backup gives a non-tunneled fallback if needed.

---

## 13. Dev loop & stopping rule (Q12c, Q12e)

### Self-grading

- `judge_simulator.py` configured with Anthropic Sonnet for the LLM judge role.
- Run `_full` scenario after each major prompt change: scores all 30 pairs.
- **Target**: ≥ 40/50 average on 30-pair set before submitting.
- Holdout (10-pair) run **once** post-lockdown to verify no overfit.

### Stopping rule

- **Cap step 8 at 6 self-grading runs.**
- If <40/50 by then, lock and ship anyway. Diminishing returns + judge-side LLM stochasticity dominates small prompt deltas past that point.

**Why**: Without a stopping rule, prompt iteration is a tar pit. 6 runs × ~30 messages each = ~180 LLM-judge calls of grading budget — affordable and bounded.

---

## 14. Implementation milestones (Q12d)

| # | Step | Time |
|---|---|---|
| 1 | Bootstrap: pyproject deps (anthropic, openai, fastapi, uvicorn, pydantic, httpx), module skeleton, `.cache/`, `logs/`, `prompts/` | ~30m |
| 2 | Dataset: run generator, commit expansion, author `test_pairs.json` (30) + `holdout_pairs.json` (10) | ~30m |
| 3 | LLM client: Anthropic + OpenAI fallback, 2-breakpoint prompt cache, response cache, smoke test | ~45m |
| 4 | Composer core: `bot.compose()` + merchant-facing skeleton + ONE playbook (`research_digest`) + validator + fallback. End-to-end on T01 (Dr. Meera). Eyeball before scaling | ~2h |
| 5 | Expand playbooks: ~14 trigger kinds + customer-facing skeleton; run all 30 pairs through compose, eyeball + initial self-grade | ~2h |
| 6 | Reply handler: `classify_reply()` (regex + Haiku fallback), state machine, ACTION-MODE playbook, templated probes/exits, anti-repetition hash | ~1.5h |
| 7 | Server endpoints: 5 endpoints, idempotency on `/v1/context` (200/409 semantics), 7-gate filter on `/v1/tick`, parallel compose, healthz + metadata | ~1h |
| 8 | Self-grade + tune: point judge_simulator at local bot, run `_full`, iterate prompts/playbooks until 30-pair avg ≥ 40/50. **Cap: 6 runs** | ~2h |
| 9 | `make_submission.py`: reads test_pairs.json, calls compose() in category-sorted order, writes JSONL | ~30m |
| 10 | Holdout check: run 10-pair holdout, compare to 30-pair score | ~30m |
| 11 | Deploy + final live test: ngrok up, judge_simulator against public URL, run all replay scenarios | ~45m |
| 12 | README + submit | ~30m |

**Total**: ~10-12 focused hours.

---

## 15. Off-spec deliberate deviations

| Spec rule | Our deviation | Why |
|---|---|---|
| `/v1/context` 409 semantics in ref impl (testing brief §7) returns 409 on equal version | We return 200 (no-op) | Brief contract §2.1 explicitly says "Re-posting the same version is a no-op." Ref impl contradicts its own contract; we follow the contract. |
| `action: end` carries no body in §2.3 examples | On hostile, we return `action: end` with a 1-line apology body | Simulator's hostile-test passes if either ends OR includes apology keyword. Both is safer. |

---

## 16. What we explicitly chose NOT to do

- **No two-stage planner** — chain-of-thought via `rationale` field gets us most of the benefit at half the latency.
- **No multi-provider mesh** — single Anthropic primary + single OpenAI fallback. More providers = more failure surface.
- **No persistent state DB** — in-memory + dev-only JSON snapshot. Judge spec says in-memory is fine; SQLite/Redis would be premature.
- **No custom dataset beyond what generator produces** — temptation to "improve" the data is a black hole.
- **No proactive re-nudging inside an open conversation** — open-conv gate handles it; "Restraint is rewarded."
- **No language detection on every message** — cheap regex first; Haiku only when uncertain.

---

## End of design decisions

This document is the **single source of truth** for what was decided and why. If implementation diverges from any choice here, update this file in the same commit. Don't let the doc drift.
