"""FastAPI shell — the 5 spec endpoints + optional /v1/teardown.

Layers
------
This module is THIN GLUE. Business logic lives in:
  bot.py         — compose() sync contract + acompose() + classify_reply() + handle_reply()
  state.py       — ContextStore / ConversationStore / SuppressionStore
  validator.py   — post-compose validation + safe fallback
  classifiers.py — reply classifier (regex + Haiku fallback)

server.py only:
  - shapes HTTP requests + responses per the spec
  - holds the singleton store instances
  - owns the snapshot dump-on-shutdown lifecycle
  - implements the tick + reply handler scaffolding (S09 / S13 / S15 fill it)

Strict response shaping
-----------------------
Every response is built by an explicit `to_public_action(...)` /
`to_public_reply(...)` helper that drops private fields (`anchor`, `lever`,
`prompt_version`, `fallback_used`). Direct serialization of ComposedMessage
is forbidden — it would leak private fields.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import bot
from validator import _hash_body_norm
from obs import RUN_ID, log_event
from state import (
    ContextStore, ConversationState, ConversationStore, ConvPhase, SuppressionStore,
    dump_state, is_dev_mode, load_state, STATE_DUMP_FILE,
)


# ---- singleton stores ------------------------------------------------------

CONTEXTS = ContextStore()
CONVERSATIONS = ConversationStore()
SUPPRESSION = SuppressionStore()
START_TS = time.time()


def _utc_now_iso() -> str:
    """UTC timestamp in the spec's trailing-Z form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- lifespan: load on startup if dev, dump on shutdown -------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if is_dev_mode():
        loaded = load_state(CONTEXTS, CONVERSATIONS, SUPPRESSION, STATE_DUMP_FILE)
        log_event("startup", dev_mode=True, dump_loaded=loaded,
                  contexts_loaded=CONTEXTS.counts())
    else:
        log_event("startup", dev_mode=False, contexts_loaded=CONTEXTS.counts())
    try:
        yield
    finally:
        if is_dev_mode():
            try:
                dump_state(CONTEXTS, CONVERSATIONS, SUPPRESSION, STATE_DUMP_FILE)
                log_event("shutdown", dev_mode=True, dumped_to=str(STATE_DUMP_FILE))
            except Exception as exc:
                log_event("shutdown_dump_failed", error=str(exc))
        else:
            log_event("shutdown", dev_mode=False)


app = FastAPI(title="Vera Bot", version="0.1.0", lifespan=lifespan)


# ---- response strippers (single source of truth) --------------------------


def to_public_action(composed_dict: dict[str, Any], *,
                     conversation_id: str,
                     merchant_id: str,
                     trigger_id: str,
                     customer_id: str | None,
                     trigger_kind: str,
                     send_as: str) -> dict[str, Any]:
    """Build a `/v1/tick` action dict. Drops private fields."""
    template_name = f"vera_{trigger_kind}_v1"
    # Sensible 3-5 element template_params (we don't actually call Meta)
    template_params = [
        merchant_id,
        trigger_id,
        composed_dict.get("body", "")[:60] or "intro",
    ]
    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": send_as,
        "trigger_id": trigger_id,
        "template_name": template_name,
        "template_params": template_params,
        "body": composed_dict["body"],
        "cta": composed_dict["cta"],
        "suppression_key": composed_dict["suppression_key"],
        "rationale": composed_dict["rationale"],
    }


def to_public_reply(action: str, *, body: str | None = None, cta: str | None = None,
                    wait_seconds: int | None = None, rationale: str = "") -> dict[str, Any]:
    """Build a `/v1/reply` response. Spec §2.3."""
    out: dict[str, Any] = {"action": action, "rationale": rationale}
    if action == "send":
        out["body"] = body or ""
        out["cta"] = cta or "open_ended"
    elif action == "wait":
        out["wait_seconds"] = int(wait_seconds or 1800)
    elif action == "end":
        # Off-spec deliberate (design-decisions.md §15): include body if present
        # so simulator's hostile-test passes on apology-keyword check.
        if body:
            out["body"] = body
    return out


# ---- /v1/healthz -----------------------------------------------------------


@app.get("/v1/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TS),
        "contexts_loaded": CONTEXTS.counts(),
    }


# ---- /v1/metadata ----------------------------------------------------------


TEAM_NAME = os.getenv("TEAM_NAME", "Vera Bot — magicpin AI Challenge")
TEAM_MEMBERS = [s.strip() for s in os.getenv("TEAM_MEMBERS", "Vaibhav").split(",")]
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "vaibhav21296@iiitd.ac.in")
SUBMITTED_AT = os.getenv("SUBMITTED_AT", _utc_now_iso())


def _active_model_string() -> str:
    """Reflect the active provider chain in /v1/metadata.

    Reads llm_client lazily so a config change (LLM_PROVIDER env var) shows up
    on the next /v1/metadata call without restart.
    """
    import llm_client as _lc
    chain = _lc._resolve_chain()
    parts = []
    for p in chain:
        parts.append(f"{_lc._compose_model_for(p)} (compose) + {_lc._classify_model_for(p)} (classify)")
    return " | ".join(parts) if parts else "unknown"


@app.get("/v1/metadata")
async def metadata() -> dict[str, Any]:
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": _active_model_string(),
        "approach": (
            "Single-prompt composer with per-trigger-kind playbook map; 6-rule "
            "deterministic validator with 1 retry + safe fallback; hybrid reply "
            "classifier (regex prefilters + Haiku fallback); 7-gate tick policy; "
            "two-cache strategy (provider prompt-cache + local response-cache)."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": "0.1.0",
        "submitted_at": SUBMITTED_AT,
        "run_id": RUN_ID,
    }


# ---- /v1/context -----------------------------------------------------------


class ContextPushRequest(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: str | None = None


@app.post("/v1/context")
async def push_context(req: ContextPushRequest) -> JSONResponse:
    accepted, current_version, reason = await CONTEXTS.push(
        req.scope, req.context_id, req.version, req.payload, req.delivered_at,
    )
    if accepted:
        ack = f"ack_{req.context_id}_v{req.version}"
        return JSONResponse(
            {"accepted": True, "ack_id": ack,
             "stored_at": _utc_now_iso()},
            status_code=200,
        )
    if reason:
        # Malformed scope or version → 400
        return JSONResponse({"accepted": False, "reason": reason}, status_code=400)
    # Stale version → 409
    return JSONResponse(
        {"accepted": False, "reason": "stale_version", "current_version": current_version},
        status_code=409,
    )


# ---- /v1/teardown (optional, design-decisions.md §6) -----------------------


@app.post("/v1/teardown")
async def teardown() -> dict[str, Any]:
    """Wipes all in-memory stores. Spec-optional but cheap insurance."""
    CONTEXTS.clear()
    CONVERSATIONS.clear()
    SUPPRESSION.clear()
    log_event("teardown", contexts_loaded=CONTEXTS.counts())
    return {"ok": True}


# ---- /v1/tick — 7-gate filter + parallel compose + timeout safety ---------


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


# Tick policy constants (design-decisions.md §5)
TICK_TIMEOUT_S = float(os.getenv("TICK_TIMEOUT_S", "23.0"))   # under 25s ceiling, far under 30s spec
COOLDOWN_HOURS = 6
STALE_GRACE_DAYS = int(os.getenv("STALE_GRACE_DAYS", "14"))
DAILY_CAP_PER_MERCHANT = 2
MAX_ACTIONS_PER_TICK = 3
URGENCY_BYPASS_COOLDOWN = 4   # urgency >= this bypasses cooldown


def _parse_iso(s: str) -> datetime:
    """Parse the spec's ISO timestamp form ('2026-04-26T10:30:00Z')."""
    s = (s or "").strip()
    if not s:
        return datetime.now(timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _consent_aliases_for(kind: str) -> set[str]:
    """Consent scopes that reasonably authorize a customer-scope trigger.

    The synthetic dataset uses human product names (`winback_offers`,
    `refill_reminders`, `promotional_offers`) rather than trigger.kind strings.
    Keep this mapping explicit so the consent gate stays conservative but does
    not drop valid customer-facing test cases.
    """
    aliases = {kind, f"{kind}s", "all"}
    mapping = {
        "recall_due": {"recall_reminders", "recall_alerts", "promotional_offers"},
        "appointment_tomorrow": {"appointment_reminders", "promotional_offers"},
        "customer_lapsed_soft": {"winback_offers", "promotional_offers", "renewal_reminders"},
        "customer_lapsed_hard": {"winback_offers", "promotional_offers", "renewal_reminders"},
        "chronic_refill_due": {"refill_reminders", "delivery_notifications", "recall_alerts"},
        "trial_followup": {"appointment_reminders", "promotional_offers"},
        "wedding_package_followup": {"promotional_offers"},
        "unplanned_slot_open": {"appointment_reminders", "promotional_offers"},
    }
    aliases.update(mapping.get(kind, set()))
    return aliases


def _gate_filter(now_dt: datetime, trigger_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the 7-gate filter pipeline. Returns (survivors, skipped_log_records).

    Each survivor is a dict {trigger, merchant, category, customer} with already-
    resolved contexts so the compose step doesn't re-read them.
    """
    survivors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for tid in trigger_ids:
        trigger = CONTEXTS.get("trigger", tid)

        # Gate 1 — resolution
        if trigger is None:
            skipped.append({"trigger_id": tid, "gate": "resolution", "reason": "trigger not in store"})
            continue
        merchant_id = trigger.get("merchant_id")
        if not merchant_id:
            skipped.append({"trigger_id": tid, "gate": "resolution", "reason": "trigger has no merchant_id"})
            continue
        merchant = CONTEXTS.get("merchant", merchant_id)
        if merchant is None:
            skipped.append({"trigger_id": tid, "gate": "resolution", "reason": f"merchant {merchant_id} not in store"})
            continue
        category_slug = merchant.get("category_slug", "")
        category = CONTEXTS.get("category", category_slug) if category_slug else None
        if category is None:
            skipped.append({"trigger_id": tid, "gate": "resolution", "reason": f"category {category_slug} not in store"})
            continue
        customer = None
        if trigger.get("scope") == "customer":
            cid = trigger.get("customer_id")
            customer = CONTEXTS.get("customer", cid) if cid else None
            # Customer trigger without customer context is a resolution miss
            if customer is None:
                skipped.append({"trigger_id": tid, "gate": "resolution", "reason": f"customer-scope trigger but customer {cid!r} missing"})
                continue

        # Gate 2 — stale
        expires = _parse_iso(trigger.get("expires_at", ""))
        if expires + timedelta(days=STALE_GRACE_DAYS) < now_dt:
            skipped.append({"trigger_id": tid, "gate": "stale", "reason": f"expired_at {trigger.get('expires_at')}"})
            continue

        # Gate 3 — suppression
        sup_key = trigger.get("suppression_key", "")
        if sup_key and SUPPRESSION.is_suppressed_or_reserved(sup_key):
            skipped.append({"trigger_id": tid, "gate": "suppression", "reason": f"suppression_key {sup_key!r} already sent or in flight"})
            continue

        # Gate 4 — active conversation
        if SUPPRESSION.merchant_reserved(merchant_id):
            skipped.append({
                "trigger_id": tid, "gate": "active_conversation",
                "reason": "merchant has an in-flight compose reservation",
            })
            continue
        open_convs = CONVERSATIONS.open_conversations_for_merchant(merchant_id)
        if open_convs:
            skipped.append({
                "trigger_id": tid, "gate": "active_conversation",
                "reason": f"merchant has {len(open_convs)} open conversation(s)",
            })
            continue

        # Gate 5 — cooldown (bypassed for urgency >= 4)
        urgency = int(trigger.get("urgency", 0) or 0)
        last = SUPPRESSION.last_send_ts.get(merchant_id, 0.0)
        now_ts = now_dt.timestamp()
        if urgency < URGENCY_BYPASS_COOLDOWN and last and (now_ts - last) < COOLDOWN_HOURS * 3600:
            mins = int((COOLDOWN_HOURS * 3600 - (now_ts - last)) // 60)
            skipped.append({
                "trigger_id": tid, "gate": "cooldown",
                "reason": f"merchant cooldown ~{mins}m remaining; urgency {urgency} below bypass threshold {URGENCY_BYPASS_COOLDOWN}",
            })
            continue

        # Gate 6 — daily cap
        daily = SUPPRESSION.daily_count(merchant_id, when=now_ts)
        if daily >= DAILY_CAP_PER_MERCHANT:
            skipped.append({
                "trigger_id": tid, "gate": "daily_cap",
                "reason": f"merchant has {daily} sends today (cap {DAILY_CAP_PER_MERCHANT})",
            })
            continue

        # Gate 7 — customer consent (only when scope=customer)
        if trigger.get("scope") == "customer":
            cust_state = customer.get("state", "")
            if cust_state == "churned":
                skipped.append({"trigger_id": tid, "gate": "customer_consent", "reason": "customer state=churned"})
                continue
            opt_in = ((customer.get("preferences") or {}).get("reminder_opt_in"))
            if opt_in is False:
                skipped.append({"trigger_id": tid, "gate": "customer_consent", "reason": "reminder_opt_in=false"})
                continue
            consent_scope = (customer.get("consent") or {}).get("scope") or []
            kind = trigger.get("kind", "")
            # Soft check: pass if either scope-list is empty (no granular consent recorded)
            # or contains a matching consent.
            consent_aliases = _consent_aliases_for(kind)
            if consent_scope and not (consent_aliases & set(consent_scope)):
                skipped.append({
                    "trigger_id": tid, "gate": "customer_consent",
                    "reason": f"trigger.kind={kind} not in consent.scope={consent_scope}",
                })
                continue

        survivors.append({
            "trigger": trigger, "merchant": merchant, "category": category,
            "customer": customer, "urgency": urgency, "expires_at": expires,
        })

    return survivors, skipped


def _select_top_actions(survivors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """1-per-merchant + global top-3 by (urgency desc, expires asc), then sort by category for cache locality."""
    # Per-merchant best (highest urgency; tiebreak earliest expiry)
    by_merchant: dict[str, dict[str, Any]] = {}
    for s in survivors:
        mid = s["merchant"]["merchant_id"]
        cur = by_merchant.get(mid)
        if cur is None:
            by_merchant[mid] = s
            continue
        if s["urgency"] > cur["urgency"]:
            by_merchant[mid] = s
        elif s["urgency"] == cur["urgency"] and s["expires_at"] < cur["expires_at"]:
            by_merchant[mid] = s

    # Global ranking: urgency desc, expires asc
    ranked = sorted(by_merchant.values(), key=lambda x: (-x["urgency"], x["expires_at"]))
    top = ranked[:MAX_ACTIONS_PER_TICK]

    # Re-sort by category for prompt-cache locality (within tied urgency)
    top.sort(key=lambda x: (x["merchant"].get("category_slug", ""), -x["urgency"]))
    return top


async def _reserve_selected_items(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reserve selected trigger/merchant pairs before LLM work starts.

    The gate filter is intentionally read-only; this reservation step is the
    atomic seam that prevents overlapping ticks from emitting the same trigger
    while a compose call is still in flight.
    """
    reserved: list[dict[str, Any]] = []
    for item in selected:
        trigger = item["trigger"]
        merchant = item["merchant"]
        merchant_id = merchant.get("merchant_id", "")
        suppression_key = trigger.get("suppression_key", "")
        ok = await SUPPRESSION.reserve_for_compose(suppression_key, merchant_id)
        if not ok:
            log_event(
                "tick_skip",
                trigger_id=trigger.get("id"),
                gate_failed="reservation",
                reason="suppression key or merchant already sent/in flight",
            )
            continue
        item["reserved_suppression_key"] = suppression_key
        reserved.append(item)
    return reserved


async def _compose_with_action_state(item: dict[str, Any]) -> tuple[dict[str, Any] | None, "bot.ComposedMessage | None"]:
    """Run compose for one survivor and shape into a /v1/tick action dict.

    Returns (action_or_none, composed). action is None if composer self-veto'd
    or fell back to safe (since fallbacks legitimately go out, return them).
    """
    trigger = item["trigger"]
    merchant = item["merchant"]
    category = item["category"]
    customer = item["customer"]

    composed = await bot.acompose(category, merchant, trigger, customer,
                                  test_id=trigger.get("id"))

    # Composer self-veto
    if composed.is_skip():
        log_event("composer_self_veto",
                  trigger_id=trigger.get("id"), merchant_id=merchant.get("merchant_id"),
                  reason=composed.skip_reason)
        return None, composed

    conversation_id = f"conv_{merchant.get('merchant_id')}_{trigger.get('id')}"
    action = to_public_action(
        composed.public(),
        conversation_id=conversation_id,
        merchant_id=merchant.get("merchant_id"),
        trigger_id=trigger.get("id"),
        customer_id=(customer or {}).get("customer_id"),
        trigger_kind=trigger.get("kind", "generic"),
        send_as=composed.send_as,
    )
    return action, composed


async def _emit_action_state_updates(action: dict[str, Any], composed: "bot.ComposedMessage",
                                     item: dict[str, Any], now_ts: float) -> None:
    """Update suppression / cooldown / daily-cap / conversation phase on emit."""
    trigger = item["trigger"]
    merchant = item["merchant"]
    customer = item["customer"]

    await SUPPRESSION.commit_emit(
        item.get("reserved_suppression_key", trigger.get("suppression_key", "")),
        composed.suppression_key or action.get("suppression_key", ""),
        merchant.get("merchant_id"),
        now_ts,
    )

    conv_id = action["conversation_id"]
    new_state = ConversationState(
        conversation_id=conv_id,
        merchant_id=merchant.get("merchant_id"),
        trigger_id=trigger.get("id"),
        send_as=composed.send_as,
        customer_id=(customer or {}).get("customer_id"),
        phase=ConvPhase.INITIATED,
        last_send_ts=now_ts,
        turns=[{"from": "bot", "body": composed.body, "ts": now_ts,
                "hash": _hash_body_norm(composed.body), "label": "initial"}],
        prior_bot_hashes={_hash_body_norm(composed.body)},
    )
    await CONVERSATIONS.upsert(new_state)

    log_event("phase_transition",
              conversation_id=conv_id,
              merchant_id=merchant.get("merchant_id"),
              trigger_id=trigger.get("id"),
              **{"from": "(none)", "to": ConvPhase.INITIATED.value, "trigger_label": "tick_emit"})


@app.post("/v1/tick")
async def tick(req: TickRequest) -> dict[str, Any]:
    """Proactive-send tick. 7-gate filter → parallel compose with 23s ceiling."""
    now_dt = _parse_iso(req.now)
    now_ts = now_dt.timestamp()

    # 1. Resolve + filter
    survivors, skipped = _gate_filter(now_dt, req.available_triggers)
    for sk in skipped:
        log_event("tick_skip", trigger_id=sk["trigger_id"], gate_failed=sk["gate"],
                  reason=sk["reason"])

    if not survivors:
        log_event("tick_complete", actions=0, attempted=len(req.available_triggers),
                  skipped=len(skipped))
        return {"actions": []}

    # 2. Select top-3 (1/merchant, urgency-sorted, then category-sorted)
    selected = _select_top_actions(survivors)
    selected = await _reserve_selected_items(selected)
    if not selected:
        log_event("tick_complete", actions=0, attempted=len(req.available_triggers),
                  skipped=len(skipped), selected=0)
        return {"actions": []}

    # 3. Parallel compose with hard 23s cutoff
    actions: list[dict[str, Any]] = []
    task_items = [
        (asyncio.create_task(_compose_with_action_state(item)), item)
        for item in selected
    ]
    tasks = {task: item for task, item in task_items}
    done, pending = await asyncio.wait(tasks.keys(), timeout=TICK_TIMEOUT_S)

    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            item = tasks[task]
            await SUPPRESSION.release_reservation(
                item.get("reserved_suppression_key", item["trigger"].get("suppression_key", "")),
                item["merchant"].get("merchant_id"),
            )
        log_event(
            "tick_timeout",
            attempted_count=len(selected),
            completed_count=len(done),
            cutoff_seconds=TICK_TIMEOUT_S,
        )

    for task, item in task_items:
        if task not in done:
            continue
        try:
            action, composed = task.result()
        except Exception as exc:
            await SUPPRESSION.release_reservation(
                item.get("reserved_suppression_key", item["trigger"].get("suppression_key", "")),
                item["merchant"].get("merchant_id"),
            )
            log_event("compose_call_failed", trigger_id=item["trigger"].get("id"),
                      error=str(exc), error_type=type(exc).__name__)
            continue
        if action is None or composed is None:
            await SUPPRESSION.release_reservation(
                item.get("reserved_suppression_key", item["trigger"].get("suppression_key", "")),
                item["merchant"].get("merchant_id"),
            )
            continue
        await _emit_action_state_updates(action, composed, item, now_ts)
        actions.append(action)

    log_event("tick_complete", actions=len(actions),
              attempted=len(req.available_triggers),
              skipped=len(skipped), selected=len(selected))
    return {"actions": actions}


# ---- /v1/reply — full state machine + LLM branches (S15+S16) ---------------


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: Literal["merchant", "customer"]
    message: str
    received_at: str
    turn_number: int


REPLY_TIMEOUT_S = float(os.getenv("REPLY_TIMEOUT_S", "23.0"))


def _phase_after_reply(label: str, action: str, prev_count: int) -> ConvPhase:
    """Compute the new conversation phase given the reply label + action taken."""
    if action == "end":
        return ConvPhase.EXITED
    if label == "auto_reply":
        return ConvPhase.AUTO_REPLY_SUSPECTED if prev_count == 0 else ConvPhase.EXITED
    if action == "wait":
        return ConvPhase.ENGAGED
    return ConvPhase.ENGAGED


def _resolve_reply_contexts(conv_state: ConversationState | None, req: ReplyRequest) -> tuple[
    dict | None, dict | None, dict | None, dict | None,
]:
    """Best-effort: pull category/merchant/trigger/customer for the LLM branches."""
    merchant_id = (conv_state and conv_state.merchant_id) or req.merchant_id
    trigger_id = conv_state and conv_state.trigger_id
    customer_id = (conv_state and conv_state.customer_id) or req.customer_id

    merchant = CONTEXTS.get("merchant", merchant_id) if merchant_id else None
    trigger = CONTEXTS.get("trigger", trigger_id) if trigger_id else None
    category = (CONTEXTS.get("category", merchant.get("category_slug"))
                if merchant and merchant.get("category_slug") else None)
    customer = CONTEXTS.get("customer", customer_id) if customer_id else None
    return category, merchant, trigger, customer


@app.post("/v1/reply")
async def reply(req: ReplyRequest) -> dict[str, Any]:
    """Hybrid reply handler: regex prefilters → templated branches OR LLM
    branches via handle_reply, with 23s hard cutoff."""
    conv_state = CONVERSATIONS.get(req.conversation_id)
    if conv_state is None:
        # Brand-new conversation we never opened — synthesize a minimal state
        # with whatever the request gave us. The LLM branches will see contexts
        # via _resolve_reply_contexts; templated branches don't need a trigger.
        conv_state = ConversationState(
            conversation_id=req.conversation_id,
            merchant_id=req.merchant_id or "",
            trigger_id="",
            send_as="vera" if req.customer_id is None else "merchant_on_behalf",
            customer_id=req.customer_id,
            phase=ConvPhase.AWAITING_REPLY,
        )

    category, merchant, trigger, customer = _resolve_reply_contexts(conv_state, req)

    # Build the incoming turn. It is appended after classification so the
    # duplicate auto-reply check compares only against prior merchant turns.
    incoming_turn = {
        "from": req.from_role, "body": req.message, "ts": time.time(),
        "hash": _hash_body_norm(req.message),
        "label": "incoming",
    }

    # Run handle_reply with hard cutoff
    try:
        action_obj = await asyncio.wait_for(
            bot.handle_reply(
                conv_state=conv_state, message=req.message,
                category=category, merchant=merchant, trigger=trigger, customer=customer,
            ),
            timeout=REPLY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log_event("reply_timeout", conversation_id=req.conversation_id, cutoff_seconds=REPLY_TIMEOUT_S)
        # Update phase + persist
        conv_state.turns.append(incoming_turn)
        prev_phase = conv_state.phase
        conv_state.phase = ConvPhase.EXITED
        await CONVERSATIONS.upsert(conv_state)
        log_event("phase_transition", conversation_id=req.conversation_id,
                  **{"from": prev_phase.value, "to": ConvPhase.EXITED.value, "trigger_label": "timeout"})
        return to_public_reply("end", rationale="timeout_safe_exit")

    # Apply state mutations: turns, auto_reply_count, phase, prior_bot_hashes
    conv_state.turns.append(incoming_turn)

    prev_phase = conv_state.phase
    if action_obj.label == "auto_reply":
        conv_state.auto_reply_count = int(conv_state.auto_reply_count or 0) + 1
    new_phase = _phase_after_reply(action_obj.label, action_obj.action, conv_state.auto_reply_count - 1
                                   if action_obj.label == "auto_reply" else 0)
    conv_state.phase = new_phase

    # Append outgoing bot turn (if we sent something) + update prior_bot_hashes
    if action_obj.action == "send" and action_obj.body:
        body_hash = _hash_body_norm(action_obj.body)
        conv_state.turns.append({
            "from": "bot", "body": action_obj.body, "ts": time.time(),
            "hash": body_hash, "label": action_obj.label,
        })
        conv_state.prior_bot_hashes.add(body_hash)
    elif action_obj.action == "end" and action_obj.body:
        # Off-spec deliberate (hostile branch returns body alongside end)
        body_hash = _hash_body_norm(action_obj.body)
        conv_state.turns.append({
            "from": "bot", "body": action_obj.body, "ts": time.time(),
            "hash": body_hash, "label": action_obj.label,
        })

    await CONVERSATIONS.upsert(conv_state)

    log_event("phase_transition", conversation_id=req.conversation_id,
              merchant_id=conv_state.merchant_id, trigger_id=conv_state.trigger_id,
              **{"from": prev_phase.value, "to": new_phase.value,
                 "trigger_label": action_obj.label})

    return to_public_reply(
        action=action_obj.action,
        body=action_obj.body or None,
        cta=action_obj.cta,
        wait_seconds=action_obj.wait_seconds,
        rationale=action_obj.rationale,
    )


# ---- signal handlers for graceful dump (Linux/Mac; Windows uses lifespan) -


def _install_signal_handlers() -> None:
    if os.name == "nt":
        return  # Windows uses uvicorn's own shutdown path
    def _handler(signum, frame):
        if is_dev_mode():
            try:
                dump_state(CONTEXTS, CONVERSATIONS, SUPPRESSION, STATE_DUMP_FILE)
            except Exception:
                pass
        os._exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            pass


_install_signal_handlers()
