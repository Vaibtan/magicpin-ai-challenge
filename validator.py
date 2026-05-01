"""6-rule deterministic post-compose validator + safe fallback templates.

Public surface:
    validate(composed, *, category, merchant, trigger, customer, anchor_required) -> list[str]
    fallback(trigger, merchant, customer) -> ComposedMessage

Rules (design-decisions.md §8):
  1. Structural        — body length, cta, suppression_key, send_as integrity
  2. Anchor verifiable — anchor must appear (normalized) in stringified contexts
  3. Vocab taboo       — no category.voice.vocab_taboo words in body
  4. Language match    — regex/devanagari/Hinglish heuristic vs merchant.languages
  5. Anti-repetition   — body hash collision against prior bot turns (reply branch)
  6. Send-as integrity — customer is None ↔ send_as=vera; customer present ↔ merchant_on_behalf

Skip-veto handling: an empty body with rationale="skip: ..." is a legitimate
composer self-veto and returns NO errors. The tick handler drops empty bodies.

Fallback: deterministic safe template keyed on trigger.kind, using guaranteed-
present merchant identity fields. Logs `event=fallback_used` at the call site.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot import ComposedMessage


# ---- normalization helpers -------------------------------------------------


_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, NFKD-fold accents."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii", errors="ignore")
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def _stringify_context_for_anchor_search(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None,
) -> str:
    """Concatenate every string value across the four contexts (recursively)
    so we can substring-search for the anchor. Numbers are also stringified
    (the model sometimes anchors on prices like 299, percentages like 38, etc.).
    """
    bits: list[str] = []

    def walk(obj: Any) -> None:
        if obj is None:
            return
        if isinstance(obj, (str, int, float, bool)):
            bits.append(str(obj))
            return
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
            return
        if isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v)
            return

    walk(category)
    walk(merchant)
    walk(trigger)
    if customer is not None:
        walk(customer)
    return _normalize(" ".join(bits))


# ---- language detection (cheap heuristic) ----------------------------------

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_HINGLISH_TOKENS = {
    "aap", "aapka", "aapke", "aapko", "aapki", "hai", "haan", "nahin", "kya",
    "kaise", "karen", "karein", "karo", "karte", "kar", "main", "mera", "meri",
    "lekin", "abhi", "phir", "wahan", "yahan", "ka", "ki", "ke", "ko", "se",
    "bhi", "agar", "lagta", "lagti", "rahi", "hoon", "ho", "ji", "thik", "theek",
    "samajh", "kyun", "kyunki", "naam", "achchha", "accha", "haan", "shukriya",
    "namaste", "namaskar", "dhanyawad", "chalega", "chahenge", "chahiye",
}


def _detect_language(body: str) -> dict[str, Any]:
    """Heuristic: returns {"langs": set, "confidence": 0.0-1.0}.

    "hi" is detected if devanagari OR ≥1 Hinglish token is present.
    "en" is detected if any ASCII alpha token is present (always true for non-empty bodies).
    """
    if not body:
        return {"langs": set(), "confidence": 1.0}

    has_devanagari = bool(_DEVANAGARI_RE.search(body))
    tokens = re.findall(r"[A-Za-z]+", body.lower())
    hinglish_hits = sum(1 for t in tokens if t in _HINGLISH_TOKENS)
    has_english_words = any(re.match(r"^[a-z]+$", t) for t in tokens)

    langs: set[str] = set()
    if has_english_words:
        langs.add("en")
    if has_devanagari or hinglish_hits >= 1:
        langs.add("hi")

    # Confidence is high when we have positive devanagari OR multiple hinglish tokens
    # OR plain English without any Hindi marker. Low otherwise.
    if has_devanagari or hinglish_hits >= 2:
        confidence = 0.95
    elif hinglish_hits == 1 or len(tokens) >= 5:
        confidence = 0.85
    else:
        confidence = 0.6

    return {"langs": langs, "confidence": confidence}


# ---- main validator --------------------------------------------------------


def validate(
    composed: "ComposedMessage",
    *,
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None,
    anchor_required: bool = True,
    prior_bot_hashes: set[str] | None = None,
) -> list[str]:
    """Returns a list of human-readable error strings. Empty list = pass.

    A composer self-veto (body=="" + skip_reason set) is a PASS.
    """
    # Skip-veto: empty body with rationale starting "skip:" is legitimate
    if composed.body == "" and composed.skip_reason:
        return []

    errors: list[str] = []

    # --- Rule 1: structural ----
    if not composed.body or len(composed.body) < 20:
        errors.append("structural: body too short (need >= 20 chars)")
    elif len(composed.body) > 1000:
        errors.append(f"structural: body too long ({len(composed.body)} > 1000 chars)")

    if composed.cta not in {"open_ended", "binary", "none"}:
        errors.append(f"structural: invalid cta {composed.cta!r}")

    if not composed.suppression_key:
        errors.append("structural: missing suppression_key")

    # --- Rule 6: send_as integrity (run early; cheap deterministic check) ---
    expected_send_as = "merchant_on_behalf" if customer is not None else "vera"
    if composed.send_as != expected_send_as:
        errors.append(f"send_as: expected {expected_send_as!r}, got {composed.send_as!r}")

    # --- Rule 2: anchor verifiable ---
    if not composed.anchor:
        if anchor_required:
            errors.append("anchor: missing (mandatory for this trigger kind)")
    else:
        haystack = _stringify_context_for_anchor_search(category, merchant, trigger, customer)
        anchor_norm = _normalize(composed.anchor)
        # Allow short fragment match: anchors are often phrases. We require the
        # full normalized anchor to appear as a substring; if the LLM cited an
        # excerpt, this catches it. We do NOT do fuzzy matching — fabrications
        # often include subtle distortions.
        if anchor_norm and anchor_norm not in haystack:
            errors.append(f"anchor_fabricated: {composed.anchor!r} not in contexts")

    # --- Rule 3: vocab taboo ---
    voice = (category.get("voice") or {})
    taboo = voice.get("vocab_taboo") or []
    body_lower = composed.body.lower()
    for word in taboo:
        if not isinstance(word, str) or not word:
            continue
        if word.lower() in body_lower:
            errors.append(f"taboo_used: {word!r}")

    # --- Rule 4: language match ---
    expected_langs = set(((merchant.get("identity") or {}).get("languages") or []))
    if expected_langs:
        detected = _detect_language(composed.body)
        if detected["confidence"] >= 0.8:
            # If "hi" expected but body is purely English (no hinglish, no devanagari),
            # flag a soft mismatch ONLY when the merchant has just "hi" in languages.
            # Most merchants have ["en", "hi", ...] so en alone is fine.
            if detected["langs"] and not (detected["langs"] & expected_langs):
                errors.append(
                    f"lang_mismatch: detected={sorted(detected['langs'])} "
                    f"expected={sorted(expected_langs)}"
                )

    # --- Rule 5: anti-repetition (only when prior_bot_hashes given by /v1/reply) ---
    if prior_bot_hashes:
        body_norm_hash = _hash_body_norm(composed.body)
        if body_norm_hash in prior_bot_hashes:
            errors.append("repeats_prior_message")

    return errors


def _hash_body_norm(body: str) -> str:
    import hashlib
    return hashlib.sha256(_normalize(body).encode("utf-8")).hexdigest()


# ---- fallback templates ----------------------------------------------------


_FALLBACK_TEMPLATES: dict[str, str] = {
    "research_digest":     "Hi {salutation}, this week's {category} digest landed — one item likely relevant to your practice. Want me to pull the abstract?",
    "regulation_change":   "Hi {salutation}, heads-up: a {category} regulation change is coming up. Want me to share the deadline + a 2-min checklist?",
    "perf_dip":            "Hi {salutation}, quick note — your performance dipped this week. Want me to break down what changed?",
    "perf_spike":          "Hi {salutation}, your numbers jumped this week — nice. Want me to share what likely drove the lift so we can repeat it?",
    "milestone_reached":   "Hi {salutation}, you crossed a milestone this week. Want me to share a short post draft you can publish?",
    "dormant_with_vera":   "Hi {salutation}, haven't heard from you in a bit. Anything I can help with this week?",
    "renewal_due":         "Hi {salutation}, your subscription is up for renewal. Want me to walk through what's queued for you next month?",
    "review_theme_emerged": "Hi {salutation}, a recurring theme showed up in your reviews this week. Want me to summarize + suggest a 1-line response template?",
    "competitor_opened":   "Hi {salutation}, a new {category} opened nearby. Want a quick read on how it might affect your inbound?",
    "festival_upcoming":   "Hi {salutation}, festival's around the corner. Want a 2-line offer suggestion that fits your catalog?",
    "festival":            "Hi {salutation}, festival's around the corner. Want a 2-line offer suggestion?",
    "weather_heatwave":    "Hi {salutation}, today's weather is unusual — quick relevance check for your offers. Want a 1-line nudge to share?",
    "ipl_match_today":     "Hi {salutation}, IPL match in the city today — tonight's footfall could shift. Want a 1-line offer to push?",
    "active_planning_intent": "Hi {salutation}, picking up on a planning thread from earlier — want me to draft the next step?",
    "supply_alert":        "Hi {salutation}, a supply alert just came in for your category — want the SKU list + alternatives?",
    "category_seasonal":   "Hi {salutation}, seasonal demand is shifting. Want a 2-line note on what to stock up?",
    "winback_eligible":    "Hi {salutation}, you have a winback opportunity from a recent dip. Want me to draft the campaign?",
    "cde_opportunity":     "Hi {salutation}, a CDE event is coming up that might interest you. Want details?",
    "curious_ask_due":     "Hi {salutation}, quick question — what's been your most-asked service this week?",
    "gbp_unverified":      "Hi {salutation}, your Google profile is unverified — verifying takes ~5 min and unlocks the full toolkit. Shall I walk you through?",
    "seasonal_perf_dip":   "Hi {salutation}, this is a seasonal-dip period. Want a 1-line note on what usually rebounds first?",
    # Customer-scope fallbacks
    "recall_due":          "Hi {customer_name}, {merchant_name} here — your recall is due. Reply YES to see open slots.",
    "customer_lapsed_soft": "Hi {customer_name}, {merchant_name} here — it's been a while. Want me to share what's on this week?",
    "customer_lapsed_hard": "Hi {customer_name}, {merchant_name} here — we'd love to have you back. Want a quick note on what's new?",
    "appointment_tomorrow": "Hi {customer_name}, {merchant_name} here — confirming your appointment tomorrow. Reply YES to confirm or RESCHEDULE.",
    "unplanned_slot_open": "Hi {customer_name}, {merchant_name} here — a slot just opened up. Want me to hold it for you?",
    "chronic_refill_due":  "Hi {customer_name}, {merchant_name} here — your refill is due. Reply YES if you'd like us to keep the same scrip ready.",
    "trial_followup":      "Hi {customer_name}, {merchant_name} here — checking in after your trial. Anything we can answer for you?",
    "wedding_package_followup": "Hi {customer_name}, {merchant_name} here — following up on your wedding consult. Want our top picks?",
}

_GENERIC_FALLBACK = "Hi {salutation}, quick check-in from your magicpin team. Anything I can help you with this week?"


def fallback(trigger: dict[str, Any], merchant: dict[str, Any], customer: dict[str, Any] | None) -> "ComposedMessage":
    """Deterministic safe message keyed on trigger.kind. Used only when validate
    fails twice in a row. Always passes its own validator (anchor optional in
    fallback because the body uses only guaranteed-present identity fields)."""
    from bot import ComposedMessage  # local import to avoid cycle

    kind = trigger.get("kind", "")
    template = _FALLBACK_TEMPLATES.get(kind, _GENERIC_FALLBACK)

    ident = merchant.get("identity", {}) or {}
    owner = ident.get("owner_first_name") or ident.get("name") or "there"
    salutation = owner if not str(owner).lower().startswith("dr.") else owner  # already prefixed
    if (merchant.get("category_slug") == "dentists") and not str(owner).lower().startswith("dr."):
        salutation = f"Dr. {owner}"

    body = template.format(
        salutation=salutation,
        category=merchant.get("category_slug", "your category"),
        customer_name=(customer or {}).get("identity", {}).get("name", "there") if customer else "there",
        merchant_name=ident.get("name", "your clinic"),
    )

    send_as = "merchant_on_behalf" if customer is not None else "vera"
    rationale = (
        f"Fallback used after validator failures. Generic safe template for trigger.kind={kind}. "
        f"[anchor=fallback, lever=specificity, trigger={kind}:u{trigger.get('urgency', 0)}, "
        f"send_as={send_as}, prompt_v=fallback]"
    )

    return ComposedMessage(
        body=body,
        cta="open_ended",
        send_as=send_as,
        suppression_key=trigger.get("suppression_key", f"fallback:{trigger.get('id', '')}"),
        rationale=rationale,
        anchor="fallback",
        lever="specificity",
        prompt_version="fallback",
        fallback_used=True,
    )
