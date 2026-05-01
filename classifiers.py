"""Reply classifier — regex prefilters + Haiku fallback.

Public surface:
    async def classify_reply(message, conv_history) -> dict
        returns {label, source, confidence, keyphrase, wait_seconds?}

Classification labels (8 total):
    auto_reply      — canned WhatsApp Business auto-reply, or verbatim-dup
    engaged         — substantive reply that wants to continue the thread
    intent_action   — explicit commitment to act ("ok lets do it", "haan kar do")
    not_interested  — courteous decline
    hostile         — rude / spam / abuse
    question        — merchant asks for info
    unclear         — ambiguous, needs a clarifying turn
    defer           — "send later" / "tomorrow" — bot returns wait_seconds

Order of checks (first match wins):
    1. Verbatim-dup hash vs all prior merchant turns in conv_history → auto_reply
    2. AUTO_REPLY_PATTERNS regex → auto_reply
    3. HOSTILE_PATTERNS regex → hostile
    4. NOT_INTERESTED_PATTERNS regex → not_interested
    5. INTENT_ACTION_PATTERNS regex → intent_action
    6. DEFER_PATTERNS regex (with wait_seconds extractor) → defer
    7. No fast-match → Haiku call over remaining 7 labels (defer is regex-only)

Every classification emits one `event=reply_classify` JSONL log line.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from llm_client import classify_call
from obs import log_event
from prompts import PROMPT_VERSION


# ---- normalization for hash dedup -----------------------------------------


_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii", errors="ignore")
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def _hash(s: str) -> str:
    return hashlib.sha256(_normalize(s).encode("utf-8")).hexdigest()


# ---- regex pattern lists ---------------------------------------------------
# Each list is (compiled_pattern, label_or_None_for_metadata).
# Patterns are case-insensitive; matched against the raw message.

_AUTO_REPLY_PATTERNS = [
    re.compile(p, flags=re.I) for p in [
        r"\bthank(?:s| you)\b.*\b(?:contact(?:ing)?|reach(?:ing)?\s*out)\b",
        r"\bwe\s*(?:will|'ll)\s*(?:get back|respond|reply)\b",
        r"\bour\s*team\s*(?:will|'ll)\s*(?:respond|reply|reach\s*out|get\s*back)\b",
        r"\bi\s*am\s*an?\s*(?:automated|auto[- ]?reply)\b",
        r"\bautomated?\s*(?:reply|response|message|assistant)\b",
        r"\bthis\s*is\s*an?\s*auto(?:matic|mated)?[- ]?reply\b",
        r"\bcurrently\s*(?:away|unavailable|out\s*of\s*office)\b",
        r"\bout\s*of\s*office\b",
        r"\byour\s*message\s*has\s*been\s*received\b",
        r"\bbusiness\s*hours?\s*are\b",
        r"\bnamaste.*shukriya.*sandes",   # canonical Hindi auto-reply pattern
        r"\bjald\s*hi\s*sampark\b",       # "will contact soon" hi
        r"\bteam\s*tak\s*pahuncha\b",     # "will pass to team"
    ]
]

_HOSTILE_PATTERNS = [
    re.compile(p, flags=re.I) for p in [
        r"\bstop\s*(?:messag\w*|spam\w*|send\w*|contact\w*|text\w*|call\w*)\b",
        r"\bdon[' ]?t\s*(?:messag\w*|spam\w*|send\w*|contact\w*|text\w*|call\w*)\b",
        r"\b(?:this\s+is|its|it'?s)\s*(?:useless\s*)?spam\b",
        r"\bspam\s*(?:message|nonsense)\b",
        r"\b(?:fuck|shit|bullshit|bloody|stupid|garbage|crap|nonsense)\b",
        r"\bleave\s*me\s*alone\b",
        r"\bblock(?:ing)?\s*(?:you|this)\b",
        r"\breport(?:ing)?\s*(?:you|this|spam)\b",
        r"\bkick.*out\b",
        r"\bharass(?:ing|ment)?\b",
        r"\bbakwas\b",
        r"\bbakwaas\b",
        r"\bmurkha\b",
    ]
]

_NOT_INTERESTED_PATTERNS = [
    re.compile(p, flags=re.I) for p in [
        r"\bnot\s*interested\b",
        r"\bno\s*thanks?\b",
        r"\bnot?\s*requir(?:e|ed|ing)\b",
        r"\bnot?\s*(?:need|wanted)\b",
        r"\bremove\s*(?:me|my\s*number|from\s*list)\b",
        r"\bunsubscribe\b",
        r"\bplease\s*(?:remove|unsubscribe)\b",
        r"\bnahi(?:n)?\s*chahiye\b",
        r"\bzaroorat?\s*nahi(?:n)?\b",
        r"\bno\s*need\b",
    ]
]

_INTENT_ACTION_PATTERNS = [
    re.compile(p, flags=re.I) for p in [
        r"^\s*ok(?:ay)?\s*(?:lets?|let'?s)\s*(?:do|go|start|proceed)\s*(?:it|this)?\b",
        r"\bgo\s*ahead\b",
        r"\bplease\s*(?:do|proceed|send\s*it|start)\b",
        r"\b(?:yes|yeah|yep|sure)\s*(?:please|sir|madam|kindly)?\s*(?:do|send|proceed)\b",
        # "send it" / "send the X" / "send now" — but NOT "send me later" (defer)
        r"\bsend\s*(?:it|the\s+\w+|now)\b",
        r"\bproceed\s*(?:please|with\s*it)?\b",
        r"\b(?:lets?|let'?s)\s*(?:do|go|start)\s*(?:it|this|now)?\b",
        r"\bdraft\s*(?:it|the|please)\b",
        r"\bpull\s*(?:it|that|the)\b",
        # Hindi-English code-mix
        r"\bha+n+\s*(?:ji)?\s*(?:kar|karo|kar\s*do|kar\s*dijiye|kar\s*dena)\b",
        r"\bkar\s*(?:do|dijiye|dena)\b",
        r"\babhi\s*(?:karo|kar\s*do)\b",
        r"\bjoin\s*(?:karna|karenge|karwado|karwa\s*do)\b",
        r"\bmujhe\s*(?:join|jud|jude)\w*\b",   # "I want to join" — Pattern D from brief
        r"\bmagicpin\s*(?:join|jud)\w*\b",
        r"^\s*ok\s*$",                     # bare "ok"
        r"^\s*han\s*$",                    # bare "han"
        r"^\s*haan\s*$",                   # bare "haan"
    ]
]

# Defer regex with wait_seconds extraction.
# Each entry: (compiled_pattern, callable_or_int producing wait_seconds).
# IMPORTANT: more-specific patterns must come BEFORE less-specific ones (the
# first regex that matches wins). e.g. "day after tomorrow" must precede
# "tomorrow" or "tomorrow" would steal the match.
_DEFER_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Specific multi-word patterns first
    (re.compile(r"\bday\s*after\s*tomorrow\b", flags=re.I),      172800),
    (re.compile(r"\bin\s*half\s*(?:an?\s*)?hour\b", flags=re.I), 1800),
    (re.compile(r"\bin\s*an?\s*hour\b", flags=re.I),             3600),
    (re.compile(r"\bin\s*(\d+)\s*min(?:utes?)?\b", flags=re.I),  lambda m: int(m.group(1)) * 60),
    (re.compile(r"\bin\s*(\d+)\s*hour(?:s)?\b", flags=re.I),     lambda m: int(m.group(1)) * 3600),
    (re.compile(r"\bin\s*(\d+)\s*day(?:s)?\b", flags=re.I),      lambda m: int(m.group(1)) * 86400),
    (re.compile(r"\bnext\s*week\b", flags=re.I),                 604800),
    (re.compile(r"\bnext\s*month\b", flags=re.I),                2592000),
    (re.compile(r"\btomorrow\b", flags=re.I),                    86400),
    (re.compile(r"\b(?:send|message|ping|remind)\s*(?:me\s*)?(?:back\s*)?later\b", flags=re.I), 3600),
    (re.compile(r"\bthoda\s*ruko\b", flags=re.I),                1800),     # "wait a bit"
    (re.compile(r"\bbaad\s*me(?:n)?\b", flags=re.I),             3600),     # "later" hi
    (re.compile(r"\bkal\s*(?:karo|baat\s*kar(?:te|enge|na)?)?\b", flags=re.I), 86400),
    (re.compile(r"^\s*later\s*$", flags=re.I),                   3600),
]

_DEFAULT_DEFER_WAIT = 1800


# ---- public entrypoint -----------------------------------------------------


async def classify_reply(message: str, conv_history: list[dict[str, Any]] | None,
                         *, conversation_id: str | None = None) -> dict[str, Any]:
    """Classify one merchant/customer reply. Returns:

        {
          "label": <one of 8 labels>,
          "source": "regex" | "haiku",
          "confidence": 0.0-1.0,
          "keyphrase": <text fragment that drove the decision>,
          "wait_seconds": <int>   # only when label == "defer"
        }
    """
    msg = (message or "").strip()
    log_ctx = {"conversation_id": conversation_id}

    if not msg:
        result = {"label": "unclear", "source": "regex", "confidence": 0.99, "keyphrase": "(empty)"}
        log_event("reply_classify", **result, **log_ctx)
        return result

    # 1. Verbatim-dup hash vs prior merchant turns
    msg_hash = _hash(msg)
    for prior in (conv_history or []):
        if prior.get("from") in ("merchant", "customer") and _hash(prior.get("body", "") or prior.get("message", "")) == msg_hash:
            result = {"label": "auto_reply", "source": "regex", "confidence": 0.99,
                      "keyphrase": "verbatim_duplicate"}
            log_event("reply_classify", **result, **log_ctx)
            return result

    # 2. AUTO_REPLY phrase patterns
    for pat in _AUTO_REPLY_PATTERNS:
        m = pat.search(msg)
        if m:
            result = {"label": "auto_reply", "source": "regex", "confidence": 0.95,
                      "keyphrase": m.group(0)[:60]}
            log_event("reply_classify", **result, **log_ctx)
            return result

    # 3. HOSTILE
    for pat in _HOSTILE_PATTERNS:
        m = pat.search(msg)
        if m:
            result = {"label": "hostile", "source": "regex", "confidence": 0.95,
                      "keyphrase": m.group(0)[:60]}
            log_event("reply_classify", **result, **log_ctx)
            return result

    # 4. NOT_INTERESTED
    for pat in _NOT_INTERESTED_PATTERNS:
        m = pat.search(msg)
        if m:
            result = {"label": "not_interested", "source": "regex", "confidence": 0.95,
                      "keyphrase": m.group(0)[:60]}
            log_event("reply_classify", **result, **log_ctx)
            return result

    # 5. INTENT_ACTION
    for pat in _INTENT_ACTION_PATTERNS:
        m = pat.search(msg)
        if m:
            result = {"label": "intent_action", "source": "regex", "confidence": 0.95,
                      "keyphrase": m.group(0)[:60]}
            log_event("reply_classify", **result, **log_ctx)
            return result

    # 6. DEFER (with wait_seconds extraction)
    for pat, wait_spec in _DEFER_PATTERNS:
        m = pat.search(msg)
        if m:
            wait_seconds = wait_spec(m) if callable(wait_spec) else int(wait_spec)
            wait_seconds = max(60, min(wait_seconds, 7 * 86400))  # clamp 1m..7d
            result = {"label": "defer", "source": "regex", "confidence": 0.9,
                      "keyphrase": m.group(0)[:60], "wait_seconds": wait_seconds}
            log_event("reply_classify", **result, **log_ctx)
            return result

    # 7. Haiku fallback (engaged | intent_action | not_interested | hostile | question | unclear | auto_reply)
    try:
        result = await _haiku_classify(msg, conv_history or [], log_ctx=log_ctx)
        result["source"] = "haiku"
        log_event("reply_classify", **result, **log_ctx)
        return result
    except Exception as exc:
        # Hard fail-open to "unclear" with low confidence — at least we don't crash the reply path
        log_event("haiku_classify_failed", error=str(exc), error_type=type(exc).__name__, **log_ctx)
        result = {"label": "unclear", "source": "haiku_failed", "confidence": 0.3,
                  "keyphrase": "(haiku error)"}
        log_event("reply_classify", **result, **log_ctx)
        return result


# ---- Haiku fallback -------------------------------------------------------


_HAIKU_CLASSIFY_PROMPT = """You classify a single merchant/customer reply on a WhatsApp business thread.

Choose ONE label from this set:
- engaged          — wants to continue the thread; substantive but no commitment yet
- intent_action    — explicit commitment to act ("ok lets do it", "go ahead", "yes please")
- not_interested   — courteous decline; doesn't want to continue
- hostile          — rude / abusive / spam-flagging
- question         — asks for clarification or info
- unclear          — ambiguous, can't tell
- auto_reply       — canned WhatsApp Business auto-reply (thank-you-for-contacting style)

Output JSON only:
{"label": "<one of the labels above>", "confidence": 0.0-1.0, "keyphrase": "<brief excerpt that drove your decision>"}

CONTEXT (recent turns, oldest first):
{history}

REPLY TO CLASSIFY:
"{message}"
"""


async def _haiku_classify(message: str, conv_history: list[dict[str, Any]],
                          *, log_ctx: dict[str, Any]) -> dict[str, Any]:
    history_str = "(none)"
    if conv_history:
        history_str = "\n".join(
            f"  {turn.get('from', '?')}: {(turn.get('body') or turn.get('message') or '')[:160]!r}"
            for turn in conv_history[-6:]
        )
    prompt = _HAIKU_CLASSIFY_PROMPT.format(history=history_str, message=message[:1000])

    out = await classify_call(prompt, prompt_version=PROMPT_VERSION,
                              cache_key_extra="reply_classify",
                              log_context=log_ctx)
    label = str(out.get("label", "unclear")).strip().lower()
    valid = {"engaged", "intent_action", "not_interested", "hostile", "question", "unclear", "auto_reply"}
    if label not in valid:
        label = "unclear"
    try:
        confidence = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    keyphrase = str(out.get("keyphrase", "") or "")[:60]
    return {"label": label, "confidence": confidence, "keyphrase": keyphrase}
