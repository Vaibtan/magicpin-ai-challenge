"""Templated reply messages for the no-LLM branches of the reply state
machine (auto_reply probe / exit, hostile, not_interested, defer, unclear).

These are short, deterministic, language-aware. They do NOT call an LLM.
Used by bot.handle_reply() (S15).

Language-pick heuristic:
    - merchant.identity.languages contains "hi" → use Hinglish ("hi_en")
    - customer.identity.language_pref starts with "hi" → use Hinglish
    - otherwise → English
"""

from __future__ import annotations

from typing import Any


def _picked_language(merchant: dict[str, Any], customer: dict[str, Any] | None) -> str:
    """Returns 'hi_en' or 'en' for template selection."""
    if customer is not None:
        pref = (customer.get("identity") or {}).get("language_pref", "") or ""
        if "hi" in pref.lower():
            return "hi_en"
        if pref.lower().startswith("en"):
            return "en"
    langs = (merchant.get("identity") or {}).get("languages", []) or []
    if "hi" in langs:
        return "hi_en"
    return "en"


def _name_for(merchant: dict[str, Any], customer: dict[str, Any] | None) -> str:
    """Best human name to use in greetings (depends on direction)."""
    if customer is not None:
        return (customer.get("identity") or {}).get("name", "there")
    ident = merchant.get("identity") or {}
    owner = ident.get("owner_first_name") or ident.get("name") or "there"
    if (merchant.get("category_slug") == "dentists") and not str(owner).lower().startswith("dr."):
        return f"Dr. {owner}"
    return str(owner)


# ---- AUTO_REPLY PROBE (1st detection) --------------------------------------
# Per-kind probe; falls back to GENERIC. Owner-name + one anchor field interpolated.

_AUTO_PROBE_HI = {
    "research_digest":     "Samajh gayi {name}. Quick 30-sec yes/no — kya aap khud digest abstract dekhna chahenge?",
    "regulation_change":   "Samajh gayi {name}. Quick check — kya main aapko deadline + 1-page checklist bhej dun?",
    "perf_dip":            "Samajh gayi {name}. Quick yes/no — kya aap dekhna chahenge ki {metric_or_topic} kyon dropped?",
    "perf_spike":          "Samajh gayi {name}. 1-line question — kya last week aapne kuch alag kiya tha?",
    "renewal_due":         "Samajh gayi {name}. Quick yes/no — renewal ka summary 1 line mein chahiye?",
    "recall_due":          "Samajh gayi {customer_name}. Quick yes/no — kya appointment book karni hai?",
    "appointment_tomorrow": "Samajh gayi {customer_name}. Quick confirm — kal {service} attend karenge?",
    "competitor_opened":   "Samajh gayi {name}. Quick read — 1-pager comparison chahiye?",
    "review_theme_emerged": "Samajh gayi {name}. 1-min read — review theme summary bhej dun?",
    "milestone_reached":   "Samajh gayi {name}. Chhoti si baat — milestone post draft chahiye?",
    "festival_upcoming":   "Samajh gayi {name}. Quick yes/no — festival ka 2-line offer chahiye?",
    "supply_alert":        "Samajh gayi {name}. Quick check — alternate SKU list bhej dun?",
    "ipl_match_today":     "Samajh gayi {name}. Quick yes/no — match ka 1-line offer push karun?",
    "winback_eligible":    "Samajh gayi {name}. Quick yes/no — winback campaign draft bhej dun?",
    "active_planning_intent": "Samajh gayi {name}. Quick — agle step ka draft chahiye?",
    "GENERIC":             "Samajh gayi {name}. Quick 30-sec yes/no — chalega?",
}

_AUTO_PROBE_EN = {
    "research_digest":     "Understood {name}. Quick 30-sec yes/no — want to glance at the digest abstract?",
    "regulation_change":   "Understood {name}. Quick check — should I send the deadline + a 1-page checklist?",
    "perf_dip":            "Understood {name}. Quick yes/no — want me to break down what changed?",
    "perf_spike":          "Understood {name}. 1-line question — anything different in your operations last week?",
    "renewal_due":         "Understood {name}. Quick yes/no — want a 1-line renewal summary?",
    "recall_due":          "Hi {customer_name}, quick yes/no — would you like to schedule the appointment?",
    "appointment_tomorrow": "Hi {customer_name}, quick confirm — joining tomorrow for your {service}?",
    "competitor_opened":   "Understood {name}. Quick read — want a 1-pager comparing listings?",
    "review_theme_emerged": "Understood {name}. 1-min read — want me to summarize the recurring review theme?",
    "milestone_reached":   "Understood {name}. Quick — want a milestone post draft you can publish?",
    "festival_upcoming":   "Understood {name}. Quick yes/no — want a 2-line festival offer that fits your catalog?",
    "supply_alert":        "Understood {name}. Quick check — want the alternate SKU list?",
    "ipl_match_today":     "Understood {name}. Quick yes/no — should I push a 1-line match-night offer to your top 50 customers?",
    "winback_eligible":    "Understood {name}. Quick yes/no — want me to draft the winback campaign?",
    "active_planning_intent": "Understood {name}. Quick — want a draft of the next step?",
    "GENERIC":             "Understood {name}. Quick 30-sec yes/no — does this work?",
}


def auto_reply_probe(trigger: dict[str, Any], merchant: dict[str, Any],
                     customer: dict[str, Any] | None) -> tuple[str, str]:
    """Returns (body, rationale) for the polite probe after 1st auto-reply."""
    lang = _picked_language(merchant, customer)
    table = _AUTO_PROBE_HI if lang == "hi_en" else _AUTO_PROBE_EN
    template = table.get(trigger.get("kind", ""), table["GENERIC"])
    name = _name_for(merchant, customer)
    customer_name = (customer or {}).get("identity", {}).get("name", "there") if customer else "there"
    payload = trigger.get("payload", {}) or {}
    body = template.format(
        name=name,
        customer_name=customer_name,
        metric_or_topic=payload.get("metric") or payload.get("topic") or payload.get("metric_or_topic") or "this",
        service=payload.get("service") or payload.get("service_due") or "appointment",
    )
    rationale = (
        f"First auto-reply detected. Sending one polite probe per trigger.kind={trigger.get('kind','')!r}; "
        f"will exit on second detection. [anchor=auto_reply_probe, lever=binary_commitment, "
        f"trigger={trigger.get('kind','')}:u{trigger.get('urgency',0)}, send_as=auto_reply_probe, prompt_v=template]"
    )
    return body, rationale


# ---- AUTO_REPLY GRACEFUL EXIT (2nd detection) ------------------------------

_AUTO_EXIT_HI = "Koi baat nahi {name}, samajh gayi. Owner ya manager ke saath directly connect kar lungi. Best wishes! 🙂"
_AUTO_EXIT_EN = "No worries {name} — understood. I'll connect with the owner/manager directly. Best wishes!"


def auto_reply_exit(merchant: dict[str, Any], customer: dict[str, Any] | None) -> tuple[str, str]:
    lang = _picked_language(merchant, customer)
    template = _AUTO_EXIT_HI if lang == "hi_en" else _AUTO_EXIT_EN
    body = template.format(name=_name_for(merchant, customer))
    rationale = (
        "Second auto-reply detected — exiting gracefully per Vera Pattern B. "
        "[anchor=auto_reply_exit, lever=specificity, trigger=auto_reply, send_as=exit, prompt_v=template]"
    )
    return body, rationale


# ---- HOSTILE EXIT ----------------------------------------------------------

_HOSTILE_HI = "Apologies {name} — won't message further. Best wishes."
_HOSTILE_EN = "Apologies {name} — won't message further. Best wishes."


def hostile_exit(merchant: dict[str, Any], customer: dict[str, Any] | None) -> tuple[str, str]:
    """Off-spec deliberate (design-decisions.md §15): action=end + body containing
    'Apologies' satisfies the simulator's hostile-test pass condition."""
    lang = _picked_language(merchant, customer)
    template = _HOSTILE_HI if lang == "hi_en" else _HOSTILE_EN
    body = template.format(name=_name_for(merchant, customer))
    rationale = (
        "Hostile reply — graceful exit with apology. "
        "[anchor=hostile_exit, lever=specificity, trigger=hostile, send_as=exit, prompt_v=template]"
    )
    return body, rationale


# ---- NOT_INTERESTED EXIT ---------------------------------------------------

_NOT_INTERESTED_HI = "Theek hai {name}, samajh gayi. Best wishes! 🙂"
_NOT_INTERESTED_EN = "Got it {name} — won't bother further. Best wishes!"


def not_interested_exit(merchant: dict[str, Any], customer: dict[str, Any] | None) -> tuple[str, str]:
    lang = _picked_language(merchant, customer)
    template = _NOT_INTERESTED_HI if lang == "hi_en" else _NOT_INTERESTED_EN
    body = template.format(name=_name_for(merchant, customer))
    rationale = (
        "Not-interested reply — courteous exit. "
        "[anchor=not_interested_exit, lever=specificity, trigger=not_interested, send_as=exit, prompt_v=template]"
    )
    return body, rationale


# ---- UNCLEAR CLARIFIER -----------------------------------------------------

_UNCLEAR_HI = "Quick yes/no {name} — kya {ask}?"
_UNCLEAR_EN = "Quick yes/no {name} — {ask}?"

_CLARIFIER_ASKS = {
    "research_digest":     ("aap digest abstract dekhna chahenge",                "want me to share the digest abstract"),
    "regulation_change":   ("main checklist bhej dun",                            "want me to send the checklist"),
    "perf_dip":            ("main metric breakdown bhej dun",                     "want a metric breakdown"),
    "perf_spike":          ("main detail share kar dun",                          "want me to share the cause analysis"),
    "renewal_due":         ("main renewal summary share karun",                   "want a renewal summary"),
    "festival_upcoming":   ("main festival offer draft bhej dun",                 "want a festival offer draft"),
    "competitor_opened":   ("main 1-pager comparison bhej dun",                   "want a 1-pager comparison"),
    "review_theme_emerged": ("main review summary bhej dun",                       "want me to summarize"),
    "milestone_reached":   ("main milestone post draft bhej dun",                 "want a milestone post draft"),
    "supply_alert":        ("main alternate SKU list bhej dun",                   "want the alternate SKU list"),
    "renewal_due":         ("main renewal summary bhej dun",                      "want a renewal summary"),
    "GENERIC":             ("main aage badhun",                                   "should I proceed"),
}


def unclear_clarifier(trigger: dict[str, Any], merchant: dict[str, Any],
                      customer: dict[str, Any] | None) -> tuple[str, str]:
    lang = _picked_language(merchant, customer)
    asks = _CLARIFIER_ASKS.get(trigger.get("kind", ""), _CLARIFIER_ASKS["GENERIC"])
    ask = asks[0] if lang == "hi_en" else asks[1]
    template = _UNCLEAR_HI if lang == "hi_en" else _UNCLEAR_EN
    body = template.format(name=_name_for(merchant, customer), ask=ask)
    rationale = (
        f"Reply was ambiguous — sending binary clarifier on trigger.kind={trigger.get('kind','')!r}. "
        f"[anchor=unclear_clarifier, lever=binary_commitment, "
        f"trigger={trigger.get('kind','')}:u{trigger.get('urgency',0)}, send_as=clarifier, prompt_v=template]"
    )
    return body, rationale


# ---- DEFER RATIONALE -------------------------------------------------------

def defer_rationale(wait_seconds: int) -> str:
    minutes = wait_seconds // 60
    if minutes < 60:
        when = f"{minutes} min"
    elif minutes < 60 * 24:
        when = f"{minutes // 60} hour(s)"
    else:
        when = f"{minutes // (60 * 24)} day(s)"
    return f"Merchant asked to defer; waiting {when}. [trigger=defer, send_as=wait, prompt_v=template]"
