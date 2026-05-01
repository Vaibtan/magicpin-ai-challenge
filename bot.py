"""Pure composer + reply handler. No HTTP, no globals (beyond imports), no I/O.

Public surface (stable; called by server.py and make_submission.py):
    compose(category, merchant, trigger, customer=None, *, conversation_history=None)
        -> ComposedMessage
    classify_reply(message, conv_history) -> ReplyLabel    [S14]
    handle_reply(conv_state, message)     -> ReplyAction   [S15-S16]

This module is async-friendly. compose() awaits llm_client.compose_call.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from llm_client import compose_call, hash_payload
from obs import log_event
from prompts import PROMPT_VERSION
from prompts.playbooks import (
    ACTION_MODE_PLAYBOOK,
    QA_MODE_PLAYBOOK,
    get_playbook,
    is_anchor_mandatory,
)
from prompts.skeletons import CUSTOMER_FACING_SYSTEM, MERCHANT_FACING_SYSTEM
from prompts.templates import (
    auto_reply_exit, auto_reply_probe, defer_rationale,
    hostile_exit, not_interested_exit, unclear_clarifier,
)
import classifiers
import validator

# ---- types -----------------------------------------------------------------


@dataclass
class ComposedMessage:
    """The composer's structured output. Private fields are stripped before
    being returned over the wire (server.py / make_submission.py); they remain
    in JSONL logs for our own audit.
    """

    body: str
    cta: str                              # "open_ended" | "binary" | "none"
    send_as: str                          # "vera" | "merchant_on_behalf"
    suppression_key: str
    rationale: str

    # Private — stripped before judge sees them
    anchor: str = ""
    lever: str = ""
    prompt_version: str = ""
    fallback_used: bool = False
    skip_reason: str = ""                 # if body=="" via composer self-veto

    # Telemetry (private)
    model: str = ""
    cache_hit: bool = False
    latency_ms: int = 0
    input_tokens_cached: int = 0
    input_tokens_uncached: int = 0
    output_tokens: int = 0
    validation_errors: list[str] = field(default_factory=list)
    validation_retried: bool = False

    def public(self) -> dict[str, Any]:
        """Strip private fields. Used by /v1/tick and make_submission.py."""
        return {
            "body": self.body,
            "cta": self.cta,
            "send_as": self.send_as,
            "suppression_key": self.suppression_key,
            "rationale": self.rationale,
        }

    def is_skip(self) -> bool:
        return self.body == ""


# ---- compose ---------------------------------------------------------------


async def compose(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None = None,
    *,
    conversation_history: list[dict[str, Any]] | None = None,
    playbook_override: str | None = None,
    prior_bot_hashes: set[str] | None = None,
    test_id: str | None = None,
) -> ComposedMessage:
    """Compose ONE message for (category, merchant, trigger, customer?).

    Pipeline:
      1. Pick skeleton (merchant-facing vs customer-facing).
      2. Resolve playbook by trigger.kind (or use playbook_override).
      3. Build cached prefix (skeleton + category) + dynamic suffix (merchant + trigger + customer + playbook + history).
      4. Call LLM. Parse JSON. Build ComposedMessage.
      5. Run validator. If errors AND attempt 0, retry with feedback.
      6. If still failing → deterministic safe fallback.

    Args (kw-only after customer):
        conversation_history: prior turns for multi-turn context (added by /v1/reply).
        playbook_override:    if provided, replaces the per-trigger-kind playbook
                              (used for ACTION_MODE / QA_MODE in handle_reply).
        prior_bot_hashes:     for anti-repetition validation in reply branches.
        test_id:              for log correlation.

    Returns: ComposedMessage. Body is "" + skip_reason populated for self-veto.
    """
    skeleton_id, skeleton_text = _pick_skeleton(customer)
    category_text = _serialize_category(category)
    if playbook_override is not None:
        playbook_text = playbook_override
    else:
        playbook_text = get_playbook(trigger.get("kind", ""))

    # log context propagated through every event for this compose
    base_log = {
        "merchant_id": merchant.get("merchant_id"),
        "trigger_id": trigger.get("id"),
        "trigger_kind": trigger.get("kind"),
        "skeleton": skeleton_id,
        "test_id": test_id,
        "prompt_version": PROMPT_VERSION,
    }

    cache_extra = {
        "category_id": category.get("slug"),
        "merchant_hash": hash_payload(merchant),
        "trigger_hash": hash_payload(trigger),
        "customer_hash": hash_payload(customer) if customer else "",
        "playbook_kind": trigger.get("kind", "default"),
        "history_hash": hash_payload(conversation_history or []),
    }

    last_errors: list[str] = []
    last_result = None

    for attempt in (0, 1):
        dynamic_text = _serialize_dynamic(
            merchant=merchant,
            trigger=trigger,
            customer=customer,
            playbook_text=playbook_text,
            conversation_history=conversation_history,
            retry_feedback=last_errors if attempt == 1 else None,
        )

        # NOTE: the cache key includes attempt 0 vs attempt 1 prompts because
        # dynamic_text differs. So a retry is a fresh call on its own key.
        attempt_cache_extra = {**cache_extra, "attempt": attempt}

        try:
            result = await compose_call(
                skeleton_text, category_text, dynamic_text,
                skeleton_id=skeleton_id,
                category_id=category.get("slug", ""),
                prompt_version=PROMPT_VERSION,
                cache_payload_extra=attempt_cache_extra,
                log_context={**base_log, "attempt": attempt},
            )
        except Exception as exc:
            log_event("compose_call_failed", error=str(exc),
                      error_type=type(exc).__name__, attempt=attempt, **base_log)
            break

        last_result = result
        composed = _parse_into_composed(result.json, trigger, customer, base_log)
        composed.model = result.model
        composed.cache_hit = result.cache_hit
        composed.latency_ms = result.latency_ms
        composed.input_tokens_cached = result.input_tokens_cached
        composed.input_tokens_uncached = result.input_tokens_uncached
        composed.output_tokens = result.output_tokens
        composed.fallback_used = result.fallback_used
        composed.prompt_version = PROMPT_VERSION

        errors = validator.validate(
            composed, category=category, merchant=merchant,
            trigger=trigger, customer=customer,
            anchor_required=is_anchor_mandatory(trigger.get("kind", "")),
            prior_bot_hashes=prior_bot_hashes,
        )
        composed.validation_errors = errors

        if not errors:
            # Pass — emit compose event and return
            _log_compose_event(composed, base_log, retried=(attempt == 1))
            composed.validation_retried = (attempt == 1)
            return composed

        # Errors — log and either retry once or fall through to fallback
        log_event("validator_fail", errors=errors, attempt=attempt, **base_log)
        last_errors = errors
        if attempt == 0:
            continue

    # Both attempts failed (or LLM call exception) → fallback
    log_event("fallback_used", reason="validator_exhausted_or_call_failed",
              last_errors=last_errors, **base_log)
    fb = validator.fallback(trigger, merchant, customer)
    fb.fallback_used = True
    fb.prompt_version = PROMPT_VERSION
    fb.validation_retried = True
    fb.validation_errors = last_errors
    if last_result is not None:
        fb.model = last_result.model
        fb.latency_ms = last_result.latency_ms
    _log_compose_event(fb, base_log, retried=True)
    return fb


# ---- helpers: skeleton picker ---------------------------------------------


def _pick_skeleton(customer: dict[str, Any] | None) -> tuple[str, str]:
    if customer is None:
        return "merchant_facing", MERCHANT_FACING_SYSTEM
    return "customer_facing", CUSTOMER_FACING_SYSTEM


# ---- helpers: prompt serialization ----------------------------------------


def _serialize_category(category: dict[str, Any]) -> str:
    """Compact, JSON-style render of CategoryContext for the prompt-cache prefix.

    We feed it as a labelled blob rather than raw JSON so the model reads it
    as a knowledge pack, not a request.
    """
    voice = category.get("voice", {})
    peer = category.get("peer_stats", {})
    digest = category.get("digest", []) or []
    catalog = category.get("offer_catalog", []) or []
    seasonal = category.get("seasonal_beats", []) or []
    trends = category.get("trend_signals", []) or []

    lines = [
        f"[CATEGORY: {category.get('slug', '?')}] ({category.get('display_name', '')})",
        "",
        "VOICE:",
        f"  tone: {voice.get('tone', '?')}",
        f"  register: {voice.get('register', '?')}",
        f"  code_mix: {voice.get('code_mix', '?')}",
        f"  vocab_allowed: {voice.get('vocab_allowed', [])}",
        f"  vocab_taboo (NEVER USE): {voice.get('vocab_taboo', [])}",
        f"  salutation_examples: {voice.get('salutation_examples', [])}",
        f"  tone_examples: {voice.get('tone_examples', [])}",
        "",
        "PEER STATS (city/segment averages — use as anchors when comparing this merchant):",
        f"  scope: {peer.get('scope', '?')}",
        f"  avg_rating={peer.get('avg_rating')}, avg_review_count={peer.get('avg_review_count')}",
        f"  avg_views_30d={peer.get('avg_views_30d')}, avg_calls_30d={peer.get('avg_calls_30d')}",
        f"  avg_directions_30d={peer.get('avg_directions_30d')}, avg_ctr={peer.get('avg_ctr')}",
        f"  avg_photos={peer.get('avg_photos')}, avg_post_freq_days={peer.get('avg_post_freq_days')}",
        f"  retention_6mo_pct={peer.get('retention_6mo_pct')}",
        "",
        "OFFER CATALOG (canonical service@price patterns for this category):",
    ]
    for o in catalog[:12]:
        lines.append(f"  - {o.get('title', '?')}  (audience={o.get('audience', '?')}, type={o.get('type', '?')})")

    lines.extend(["", "DIGEST (this week's curated items — research/compliance/CDE/trend):"])
    for d in digest[:8]:
        lines.append(f"  - id={d.get('id')}  kind={d.get('kind')}")
        if d.get("title"):       lines.append(f"      title: {d['title']}")
        if d.get("source"):      lines.append(f"      source: {d['source']}")
        if d.get("trial_n"):     lines.append(f"      trial_n: {d['trial_n']}")
        if d.get("patient_segment"): lines.append(f"      segment: {d['patient_segment']}")
        if d.get("summary"):     lines.append(f"      summary: {d['summary']}")
        if d.get("actionable"):  lines.append(f"      actionable: {d['actionable']}")
        if d.get("date"):        lines.append(f"      date: {d['date']}")
        if d.get("credits"):     lines.append(f"      credits: {d['credits']}")

    if seasonal:
        lines.extend(["", "SEASONAL BEATS:"])
        for s in seasonal[:6]:
            lines.append(f"  - {s.get('month_range', '?')}: {s.get('note', '')}")
    if trends:
        lines.extend(["", "TREND SIGNALS:"])
        for t in trends[:6]:
            lines.append(f"  - query='{t.get('query', '?')}'  delta_yoy={t.get('delta_yoy')}  segment={t.get('segment_age', '?')}")

    return "\n".join(lines)


def _serialize_dynamic(
    *,
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None,
    playbook_text: str,
    conversation_history: list[dict[str, Any]] | None,
    retry_feedback: list[str] | None,
) -> str:
    """Compact render of the per-call dynamic block (merchant + trigger + customer + playbook + history)."""
    lines: list[str] = []

    # ---- merchant ----
    ident = merchant.get("identity", {})
    sub = merchant.get("subscription", {})
    perf = merchant.get("performance", {})
    delta = perf.get("delta_7d", {}) or {}
    offers = merchant.get("offers", []) or []
    cust_agg = merchant.get("customer_aggregate", {}) or {}
    signals = merchant.get("signals", []) or []
    review_themes = merchant.get("review_themes", []) or []
    history = merchant.get("conversation_history", []) or []

    lines.extend([
        "[MERCHANT]",
        f"merchant_id: {merchant.get('merchant_id')}",
        f"name: {ident.get('name')!r}",
        f"city/locality: {ident.get('city')} / {ident.get('locality')}",
        f"languages: {ident.get('languages', [])}",
        f"owner_first_name: {ident.get('owner_first_name')!r}",
        f"verified: {ident.get('verified')}",
        f"established_year: {ident.get('established_year')}",
        f"subscription: {sub.get('status')} / {sub.get('plan')} / days_remaining={sub.get('days_remaining')}",
    ])

    if perf:
        lines.append(
            f"performance ({perf.get('window_days', 30)}d): "
            f"views={perf.get('views')} calls={perf.get('calls')} directions={perf.get('directions')} "
            f"ctr={perf.get('ctr')} leads={perf.get('leads')}"
        )
        if delta:
            d_views = _pct(delta.get("views_pct"))
            d_calls = _pct(delta.get("calls_pct"))
            d_ctr = _pct(delta.get("ctr_pct"))
            lines.append(f"  delta_7d: views={d_views}, calls={d_calls}, ctr={d_ctr}")

    if offers:
        lines.append("offers:")
        for o in offers[:8]:
            lines.append(f"  - [{o.get('status', '?')}] {o.get('title', '?')}  (id={o.get('id', '?')})")

    if cust_agg:
        agg_bits = ", ".join(f"{k}={v}" for k, v in cust_agg.items())
        lines.append(f"customer_aggregate: {agg_bits}")

    if signals:
        lines.append(f"signals: {signals}")

    if review_themes:
        lines.append("review_themes:")
        for r in review_themes[:5]:
            lines.append(f"  - {r.get('theme', '?')} ({r.get('sentiment', '?')}, {r.get('occurrences_30d', '?')}× last 30d): {r.get('common_quote', '')!r}")

    if history:
        lines.append("recent conversation_history (newest last):")
        for h in history[-5:]:
            lines.append(f"  {h.get('ts', '')} {h.get('from', '?')}: {h.get('body', '')!r}  [{h.get('engagement', '')}]")

    # ---- trigger ----
    lines.extend([
        "",
        "[TRIGGER]",
        f"id: {trigger.get('id')}",
        f"kind: {trigger.get('kind')}",
        f"scope: {trigger.get('scope')}",
        f"source: {trigger.get('source')}",
        f"urgency: {trigger.get('urgency')}",
        f"suppression_key: {trigger.get('suppression_key')}",
        f"expires_at: {trigger.get('expires_at')}",
        f"payload: {json.dumps(trigger.get('payload', {}), ensure_ascii=False)}",
    ])

    # ---- customer (if present) ----
    if customer is not None:
        c_ident = customer.get("identity", {}) or {}
        rel = customer.get("relationship", {}) or {}
        prefs = customer.get("preferences", {}) or {}
        consent = customer.get("consent", {}) or {}
        lines.extend([
            "",
            "[CUSTOMER]",
            f"customer_id: {customer.get('customer_id')}",
            f"name: {c_ident.get('name')!r}",
            f"language_pref: {c_ident.get('language_pref')!r}",
            f"age_band: {c_ident.get('age_band')}",
            f"state: {customer.get('state')}",
            f"relationship: first_visit={rel.get('first_visit')}, last_visit={rel.get('last_visit')}, "
            f"visits_total={rel.get('visits_total')}, services={rel.get('services_received', [])}, "
            f"lifetime_value={rel.get('lifetime_value')}",
            f"preferences: {prefs}",
            f"consent: opted_in_at={consent.get('opted_in_at')}, scope={consent.get('scope', [])}",
        ])

    # ---- playbook ----
    lines.extend(["", playbook_text])

    # ---- conversation history (multi-turn — passed in by /v1/reply path) ----
    if conversation_history:
        lines.extend(["", "[CONVERSATION SO FAR — do not repeat anything you've already said]"])
        for i, turn in enumerate(conversation_history, 1):
            lines.append(f"  turn {i} {turn.get('from', '?')}: {turn.get('body', '')!r}")

    # ---- retry feedback ----
    if retry_feedback:
        lines.extend([
            "",
            "[VALIDATOR FEEDBACK ON YOUR PRIOR ATTEMPT — fix these issues]",
            *[f"  - {e}" for e in retry_feedback],
            "Use ONLY facts from the contexts above. Compose again.",
        ])

    lines.extend(["", "Now compose. Output JSON only — no fences, no prose."])
    return "\n".join(lines)


def _pct(v: Any) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v) * 100:+.1f}%"
    except (TypeError, ValueError):
        return str(v)


# ---- helpers: parsing the LLM JSON into ComposedMessage --------------------


def _parse_into_composed(
    payload: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None,
    base_log: dict[str, Any],
) -> ComposedMessage:
    """Tolerantly parse the LLM JSON. Missing optional fields default to empty.

    Be lenient on field absence (validator catches structural issues); be
    strict on field types (cast/clean).
    """
    body = str(payload.get("body", "") or "").strip()
    cta = str(payload.get("cta", "open_ended") or "open_ended").strip()
    if cta not in {"open_ended", "binary", "none"}:
        cta = "open_ended"

    # send_as: derived from customer presence; trust composer if it agrees, else override
    expected_send_as = "merchant_on_behalf" if customer is not None else "vera"
    send_as = str(payload.get("send_as", expected_send_as) or expected_send_as).strip()
    if send_as not in {"vera", "merchant_on_behalf"}:
        send_as = expected_send_as

    suppression_key = str(payload.get("suppression_key", "") or trigger.get("suppression_key", "") or "").strip()
    rationale = str(payload.get("rationale", "") or "").strip()
    anchor = str(payload.get("anchor", "") or "").strip()
    lever = str(payload.get("lever", "") or "").strip()

    # skip detection
    skip_reason = ""
    if not body:
        rl = rationale.lower()
        if rl.startswith("skip:") or rl.startswith("skip ") or "skip:" in rl[:30]:
            # Extract reason after "skip:"
            try:
                skip_reason = rationale.split(":", 1)[1].strip()
            except IndexError:
                skip_reason = "unspecified"

    return ComposedMessage(
        body=body, cta=cta, send_as=send_as,
        suppression_key=suppression_key,
        rationale=rationale, anchor=anchor, lever=lever,
        skip_reason=skip_reason,
    )


# ---- helpers: structured compose log ---------------------------------------


def _log_compose_event(c: ComposedMessage, base: dict[str, Any], retried: bool) -> None:
    log_event(
        "compose",
        model=c.model,
        cache_hit=c.cache_hit,
        latency_ms=c.latency_ms,
        input_tokens={"cached": c.input_tokens_cached, "uncached": c.input_tokens_uncached},
        output_tokens=c.output_tokens,
        validation={"errors": c.validation_errors, "retried": retried, "fallback": c.fallback_used},
        anchor=c.anchor, lever=c.lever,
        body_hash="sha256:" + hashlib.sha256(c.body.encode("utf-8")).hexdigest()[:16] if c.body else "",
        body_chars=len(c.body),
        cta=c.cta, send_as=c.send_as,
        skip_reason=c.skip_reason,
        rationale=c.rationale,
        **base,
    )


# ============================================================================
# REPLY HANDLER (S15 + S16)
# ============================================================================


@dataclass
class ReplyAction:
    """Output of handle_reply(). Mapped to the /v1/reply response by server.py."""

    action: str                   # "send" | "wait" | "end"
    body: str = ""
    cta: str = "open_ended"
    wait_seconds: int = 0
    rationale: str = ""

    # Telemetry — server.py uses these to update conversation phase
    label: str = "unclear"        # the classifier's verdict
    source: str = "regex"         # "regex" | "haiku" | "haiku_failed"
    confidence: float = 0.0
    keyphrase: str = ""

    # Set when the LLM was called (engaged / question / intent_action branches)
    composed: "ComposedMessage | None" = None


async def handle_reply(
    *,
    conv_state: Any,                       # state.ConversationState (typed loosely to avoid cycle)
    message: str,
    category: dict[str, Any] | None = None,
    merchant: dict[str, Any] | None = None,
    trigger: dict[str, Any] | None = None,
    customer: dict[str, Any] | None = None,
) -> ReplyAction:
    """Classify the merchant/customer reply and produce the next bot action.

    PURE w.r.t. conversation state — does not mutate `conv_state`. The caller
    (server.py /v1/reply) updates conv_state.phase, .auto_reply_count,
    .turns, .prior_bot_hashes based on the returned ReplyAction.

    Pipeline:
      1. classify_reply(message, conv_state.turns)  # 8 labels
      2. Templated branches (no LLM):
         auto_reply (1st)  → polite probe
         auto_reply (≥2nd) → graceful exit
         hostile           → action: end + apology body
         not_interested    → action: end
         defer             → action: wait (wait_seconds from regex)
         unclear           → templated binary clarifier
      3. LLM branches (call compose()):
         intent_action → ACTION_MODE_PLAYBOOK
         question      → QA_MODE_PLAYBOOK
         engaged       → main composer (default playbook for trigger.kind)
                         + conversation_history + prior_bot_hashes for anti-repeat
    """
    # Build conv-history for the classifier in the format it expects
    conv_history = list(getattr(conv_state, "turns", []) or [])
    classification = await classifiers.classify_reply(
        message, conv_history, conversation_id=getattr(conv_state, "conversation_id", None),
    )
    label = classification["label"]
    source = classification.get("source", "regex")
    conf = float(classification.get("confidence", 0.0) or 0.0)
    keyphrase = classification.get("keyphrase", "") or ""

    # ---- Templated branches (no LLM) -----------------------------------
    if label == "auto_reply":
        if int(getattr(conv_state, "auto_reply_count", 0)) >= 1:
            body, rationale = auto_reply_exit(merchant or {}, customer)
            log_event(
                "auto_reply_exit",
                conversation_id=getattr(conv_state, "conversation_id", None),
                count=int(getattr(conv_state, "auto_reply_count", 0)) + 1,
                last_label=label,
            )
            return ReplyAction(action="end", body=body, rationale=rationale,
                               label=label, source=source, confidence=conf, keyphrase=keyphrase)
        body, rationale = auto_reply_probe(trigger or {}, merchant or {}, customer)
        return ReplyAction(action="send", body=body, cta="binary", rationale=rationale,
                           label=label, source=source, confidence=conf, keyphrase=keyphrase)

    if label == "hostile":
        body, rationale = hostile_exit(merchant or {}, customer)
        return ReplyAction(action="end", body=body, rationale=rationale,
                           label=label, source=source, confidence=conf, keyphrase=keyphrase)

    if label == "not_interested":
        body, rationale = not_interested_exit(merchant or {}, customer)
        return ReplyAction(action="end", body=body, rationale=rationale,
                           label=label, source=source, confidence=conf, keyphrase=keyphrase)

    if label == "defer":
        wait = int(classification.get("wait_seconds", 1800) or 1800)
        return ReplyAction(action="wait", wait_seconds=wait, rationale=defer_rationale(wait),
                           label=label, source=source, confidence=conf, keyphrase=keyphrase)

    if label == "unclear":
        body, rationale = unclear_clarifier(trigger or {}, merchant or {}, customer)
        return ReplyAction(action="send", body=body, cta="binary", rationale=rationale,
                           label=label, source=source, confidence=conf, keyphrase=keyphrase)

    # ---- LLM branches (intent_action | question | engaged) -------------
    if not (category and merchant and trigger):
        # Defensive: if the server can't resolve the contexts, fall back to clarifier.
        body, rationale = unclear_clarifier(trigger or {}, merchant or {}, customer)
        return ReplyAction(
            action="send", body=body, cta="binary",
            rationale=f"Missing contexts; falling back to clarifier. {rationale}",
            label=label, source=source, confidence=conf, keyphrase=keyphrase,
        )

    if label == "intent_action":
        playbook_override = ACTION_MODE_PLAYBOOK
    elif label == "question":
        playbook_override = QA_MODE_PLAYBOOK
    else:
        playbook_override = None  # "engaged" → use default per-kind playbook

    composed = await compose(
        category, merchant, trigger, customer,
        conversation_history=conv_history,
        playbook_override=playbook_override,
        prior_bot_hashes=set(getattr(conv_state, "prior_bot_hashes", set()) or set()),
        test_id=getattr(conv_state, "conversation_id", None),
    )

    if composed.is_skip():
        # Composer self-vetoed in reply path — fall back to clarifier (we still owe a reply)
        body, rationale = unclear_clarifier(trigger, merchant, customer)
        return ReplyAction(
            action="send", body=body, cta="binary",
            rationale=f"Composer skipped reply; falling back to clarifier. {rationale}",
            label=label, source=source, confidence=conf, keyphrase=keyphrase,
        )

    return ReplyAction(
        action="send", body=composed.body, cta=composed.cta,
        rationale=composed.rationale,
        label=label, source=source, confidence=conf, keyphrase=keyphrase,
        composed=composed,
    )


# Re-exported so other modules can compute the same hash key for anti-repetition
def hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
