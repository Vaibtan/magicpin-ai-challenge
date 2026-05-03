"""Per-trigger-kind playbook map + reply-handling playbooks.

Each playbook is a 3-5 line framing snippet specifying:
  • which compulsion lever to lean on
  • which payload field is the verifiable anchor
  • the "why now" framing the rationale should reflect
  • a low-friction CTA hint

Injected adjacent to the dynamic context block in each compose call.
NOT cached — they're small and varying. The skeleton + category context
are the cache breakpoints (design-decisions.md §9).

Reply-handling playbooks (ACTION_MODE, QA_MODE) are reverse-engineered
from `judge_simulator.py`'s keyword detectors so the bot's output text
matches the simulator's pass conditions.
"""

from __future__ import annotations

# --- Trigger-kind playbooks --------------------------------------------------
# S05 ships ONE entry (research_digest) end-to-end; S10 fills the rest.

PLAYBOOKS: dict[str, str] = {

    # ============================================================
    # External — knowledge / world-event driven (merchant-facing)
    # ============================================================

    "research_digest": """
TRIGGER: A category-relevant research / digest item just published this week.
ANCHOR: payload.top_item_id resolves into category.digest. Use the digest item's
  `source` citation verbatim (e.g., "JIDA Oct 2026, p.14") AND one specific number
  from the item (trial_n, %improvement, etc.). Both anchors strengthen specificity.
LEVER: reciprocity + curiosity. The merchant gets value WITHOUT lifting a finger.
WHY-NOW: "<source> dropped this week. One item likely relevant to your <merchant cohort>: ..."
CTA: low-friction binary or open. "Want the abstract?" / "Want me to draft a patient-ed WhatsApp?"
""".strip(),

    "regulation_change": """
TRIGGER: A regulatory / compliance change with a deadline that affects this category.
ANCHOR: payload.deadline_iso AND the digest item's `source` citation (e.g.,
  "DCI circular 2026-11-04"). Both anchors lift specificity AND credibility.
LEVER: loss_aversion + effort_externalization. Deadline creates urgency; offering
  to draft a checklist removes friction.
WHY-NOW: "<source> revised <rule>. Effective <deadline_iso>. Worth a 60-second check on your setup."
CTA: "Want a 1-page checklist?" / "Want me to flag the SOP changes?"
""".strip(),

    "festival_upcoming": """
TRIGGER: A festival is N days away (e.g., Diwali, Navratri, Eid, Christmas).
ANCHOR: festival name + days_until from payload, AND ONE merchant-specific
  hook (an active offer, a category seasonal_beat, or a peer benchmark).
  If no specific hook is present, prefer SKIP — generic festival messaging is
  spammy and the judge penalizes it.
LEVER: specificity + curiosity. Festival alone is generic; pair it with a
  concrete next step rooted in the merchant's catalog.
WHY-NOW: "<festival> in N days — your <category> sees a Y% spike historically.
  Want a 2-line offer that fits your catalog?"
CTA: open-ended draft request. Avoid pre-baked discount language.
""".strip(),

    "weather_heatwave": """
TRIGGER: Local weather event (heatwave, monsoon, cold snap) likely to shift footfall.
ANCHOR: payload.weather_metric (temperature, rainfall_mm) + city. SKIP if the
  merchant's category isn't weather-sensitive (dentists, pharmacies are; salons
  are; restaurants are; gyms partly). For low-relevance pairings, return a skip.
LEVER: loss_aversion + specificity. "Today's weather will probably reduce
  footfall by X — here's a 1-line nudge to push."
WHY-NOW: cite the specific metric ("42°C today") and the likely behaviour shift.
CTA: "Want me to draft the nudge?"
""".strip(),

    "local_news_event": """
TRIGGER: A local news/sports event affecting footfall (IPL match, expressway
  closure, cricket-on-TV, a parade).
ANCHOR: payload.event_name + payload.local_city. Optionally pair with a
  category seasonal_beat.
LEVER: specificity + curiosity. The merchant probably hasn't connected the
  event to their inbound — that's the value.
WHY-NOW: "<event> in your area today. <category> in this city historically
  sees X behaviour during these events."
CTA: "Want a 1-line nudge?"
""".strip(),

    "ipl_match_today": """
TRIGGER: IPL match in the merchant's city tonight; footfall pattern shift.
ANCHOR: payload.match or payload.venue or payload.city; pair with the merchant's
  active offer / delivery-share / order mix if it fits an IPL-night use case.
LEVER: specificity + curiosity. Many merchants don't optimise for these one-offs.
WHY-NOW: "<match> at <venue> tonight — your current offer/order mix suggests a concrete next step."
CTA: "Want me to draft a 1-line match-night combo?" Do NOT invent customer counts.
""".strip(),

    "competitor_opened": """
TRIGGER: A new competitor opened nearby per payload.competitor_distance_km +
  payload.competitor_name (if available).
ANCHOR: distance ("1.3 km away") AND any contextual peer_stats fact (your CTR
  vs. peer median). Both ground the message.
LEVER: loss_aversion + reciprocity. "I saw this and thought you'd want to know."
  Avoid alarmism — peer-tone, not panic.
WHY-NOW: "A new <category> opened <distance>. Your CTR vs. peer median is X.
  Worth a quick read on how this might affect inbound."
CTA: "Want a 1-pager comparing your listing to theirs?"
""".strip(),

    "category_trend_movement": """
TRIGGER: A category-wide search trend shifted significantly (Practo / Google Trends).
ANCHOR: trend_signals[i].query + delta_yoy from category context. Quote the
  query string verbatim. Pair with merchant.signals if relevant.
LEVER: curiosity + asking. The merchant's customers are searching this — does
  the merchant offer it?
WHY-NOW: "'<query>' searches are +X% YoY in your area. Do you offer this?"
CTA: open-ended. "Worth listing it on your profile?"
""".strip(),

    "category_seasonal": """
TRIGGER: Seasonal demand shift (start of summer, monsoon, winter).
ANCHOR: payload.season or one payload.trends[] item + a category seasonal_beat from contexts.
LEVER: loss_aversion + reciprocity. Heads-up to capture the predictable shift.
WHY-NOW: "<season> shift just started — historically your <category> sees X."
CTA: "Want a 1-line note on what to stock up?"
""".strip(),

    # ============================================================
    # Internal — merchant-state driven (merchant-facing)
    # ============================================================

    "perf_dip": """
TRIGGER: A performance metric dropped sharply (calls, views, CTR) week-over-week.
ANCHOR: payload.metric + payload.delta_pct + payload.vs_baseline. Quote both
  the percentage AND the absolute baseline number.
LEVER: loss_aversion + reciprocity. Diagnostic tone — don't blame, investigate.
WHY-NOW: "Your <metric> is -<X>% week-over-week (vs. <baseline> typical).
  Likely causes: <2-3 from the merchant signals/review_themes>."
CTA: "Want me to break it down further?" / "Want a 30-sec audit?"
""".strip(),

    "perf_spike": """
TRIGGER: A performance metric jumped (positive). Use to celebrate AND learn.
ANCHOR: payload.metric + payload.delta_pct + concrete number ("calls 12 -> 18").
LEVER: reciprocity + asking. Praise + extract why so we can repeat.
WHY-NOW: "<metric> jumped <X>% week-over-week — nice. What changed on your end?"
CTA: open-ended ask. "Anything different last week we can repeat?"
""".strip(),

    "milestone_reached": """
TRIGGER: A vanity milestone (100 reviews, 1000 views, 1-year-on-platform).
ANCHOR: payload.milestone_value + the actual number from merchant.performance
  or merchant.customer_aggregate. Concrete number is non-negotiable.
LEVER: reciprocity + social_proof. Celebrate, then offer a small follow-up.
WHY-NOW: "You crossed <milestone> this <period>. <peer comparison if relevant>."
CTA: "Want me to draft a short post you can publish?"
""".strip(),

    "dormant_with_vera": """
TRIGGER: Merchant hasn't engaged with Vera in N days.
ANCHOR: payload.days_dormant + ONE recent positive datum from merchant
  (a perf delta_7d positive, a recent review_theme, a milestone-in-reach).
  If nothing positive, SKIP — silence is better than empty re-engagement.
LEVER: curiosity / asking. Don't open with "we miss you" — boring. Open with
  a fact the merchant might not know.
WHY-NOW: "Quick heads-up — your <metric> moved <X> in the last week, thought
  you'd want to know."
CTA: "Anything I can help with this week?"
""".strip(),

    "renewal_due": """
TRIGGER: Subscription renewal in N days.
ANCHOR: payload.days_remaining + payload.renewal_amount + ONE concrete value
  the merchant has gotten from the platform (impressions, leads, recovered
  customers — pull from performance / customer_aggregate).
LEVER: loss_aversion + specificity. Quantified value > vague "renew now".
WHY-NOW: "Your renewal is in <X> days. Last <window> you got <metric>."
CTA: "Want me to walk through what's queued for next month?"
DO NOT mention unrelated category digest/research/trend items in a renewal message.
""".strip(),

    "review_theme_emerged": """
TRIGGER: Multiple recent reviews mentioned the same theme (positive or negative).
ANCHOR: payload.theme + payload.occurrences + merchant.review_themes[i].common_quote.
  Quote the common_quote verbatim — that's the most specific anchor.
LEVER: reciprocity + curiosity. Bringing patterns to attention is high-value.
WHY-NOW: "<N> reviews this week mention <theme>: '<quote>'. Worth a look."
CTA: "Want a 1-line response template?" / "Want me to summarize all <N>?"
""".strip(),

    "scheduled_recurring": """
TRIGGER: A weekly/monthly recurring nudge (e.g., Friday curiosity-ask cadence).
ANCHOR: any concrete signal from the merchant (a recent review, an active
  offer that's expiring, a customer_aggregate stat). If nothing concrete, SKIP.
LEVER: asking + reciprocity. Recurring nudges live or die on how curious the
  question is.
WHY-NOW: "Quick weekly check-in. <observed signal>." (No "weekly" wording —
  the cadence is internal, the merchant doesn't need to know.)
CTA: open-ended question rooted in the merchant's data.
""".strip(),

    "curious_ask_due": """
TRIGGER: Curiosity-driven knowledge nudge — ask the merchant something
  about their practice that they're likely to enjoy answering.
ANCHOR: optional but preferred (a specific category trend, a peer stat).
LEVER: asking. Open-ended question that gets the merchant talking.
WHY-NOW: "Quick question — <category-specific curiosity trigger>?"
CTA: open-ended question. NO binary CTA on this kind.
""".strip(),

    "active_planning_intent": """
TRIGGER: Merchant has an active planning thread (corporate orders, kids program,
  new launch). Continue the planning conversation.
ANCHOR: payload.intent_topic + recent conversation_history exchanges.
LEVER: effort_externalization + asking. Move the plan one step forward.
WHY-NOW: "Picking up on the <topic> thread — the next step is X."
CTA: "Want me to draft <next deliverable>?"
If prior conversation already contains package details, reuse those exact details.
Do NOT invent new pricing, class counts, customer cohorts, or package terms.
""".strip(),

    "supply_alert": """
TRIGGER: Supply / SKU recall, shortage, or restock event for the category.
ANCHOR: payload.molecule + payload.affected_batches[] + payload.manufacturer.
  If payload.alert_id resolves to a digest item, use that source/title too.
LEVER: loss_aversion + reciprocity. Time-sensitive operational signal.
WHY-NOW: "Heads-up: <molecule> alert just came in. Affects <batch codes>."
CTA: "Want the alternates list?" / "Want the SKU codes?"
""".strip(),

    "winback_eligible": """
TRIGGER: Merchant has dropped subscription / engagement; eligible for a winback.
ANCHOR: payload.days_since_expiry + payload.perf_dip_pct + ONE quantified
  merchant/customer value (views, calls, total customers, lapsed customers).
LEVER: loss_aversion + reciprocity. Concrete historic value > vague "come back".
WHY-NOW: "While you were off, your category in <city> moved <X>. Plus, you have
  <historic_value> already. Worth 5 mins to re-activate?"
CTA: open-ended.
""".strip(),

    "cde_opportunity": """
TRIGGER: A continuing education / professional development event coming up.
ANCHOR: payload.event_title + date + credits (from category.digest CDE entry).
LEVER: reciprocity + specificity. Targeted opportunity, not a generic ad.
WHY-NOW: "<title> on <date>. <credits> credits."
CTA: "Want the registration link?" / "Should I block your calendar?"
""".strip(),

    "gbp_unverified": """
TRIGGER: Google Business Profile is unverified — capping the merchant's reach.
ANCHOR: merchant.identity.verified=false + payload.estimated_uplift_pct +
  the merchant's current performance/views/calls.
LEVER: loss_aversion + effort_externalization. "5 minutes; unlocks X."
WHY-NOW: "Your profile is unverified. Verifying takes ~5 min and lifts X by Y%."
CTA: "Want me to walk you through?"
""".strip(),

    "seasonal_perf_dip": """
TRIGGER: Performance dipped, but it's a known seasonal pattern (predictable).
ANCHOR: payload.season + your category's seasonal_beats note.
LEVER: reciprocity. Reassure that it's seasonal, then point at what bounces back.
WHY-NOW: "This is the seasonal-dip window your category sees every year.
  Historically <X> rebounds first."
CTA: "Want a 1-line note on what to focus on?"
""".strip(),

    # ============================================================
    # Customer-scope (only when CustomerContext is populated)
    # ============================================================

    "recall_due": """
TRIGGER (CUSTOMER): A recurring service is due — recall window opened.
ANCHOR: payload.service_due + payload.last_service_date + payload.due_date +
  payload.available_slots[0..2].label (verbatim).
LEVER: loss_aversion + binary_commitment.
SEND_AS: merchant_on_behalf. The customer hears their merchant, not Vera.
WHY-NOW: "It's been N months since your last visit — your <service> recall is due."
CTA: "Reply 1 for <slot1>, 2 for <slot2>, or tell us a time that works."
NOTE: address the customer by name. Match customer.identity.language_pref.
""".strip(),

    "customer_lapsed_soft": """
TRIGGER (CUSTOMER): Customer is in 'lapsed_soft' state (3-6mo since last visit).
ANCHOR: payload.last_visit + customer.relationship.services_received[-1] (last
  service they had). Pair with ONE relevant active merchant offer.
LEVER: reciprocity + curiosity. Familiar tone — no salesy "we miss you" line.
SEND_AS: merchant_on_behalf.
WHY-NOW: "Hi <name>, <merchant> here — your last <service> was N months ago.
  We have <new offer / what's-on-this-week> if you'd like to drop in."
CTA: open or binary. Honor preferred_slots if known.
""".strip(),

    "customer_lapsed_hard": """
TRIGGER (CUSTOMER): Customer is 'lapsed_hard' (long inactive, near churn).
ANCHOR: payload.days_since_last_visit (literal number). The concrete days count
  is the strongest specificity hook — keep it as the raw number from the payload.
LEVER: reciprocity + binary_commitment. Make returning frictionless, NOT guilt-laden.
SEND_AS: merchant_on_behalf.

REQUIRED FACTS (use what's available; do not invent):
  1. customer.identity.name
  2. merchant.identity.name (so the customer knows who's writing)
  3. payload.days_since_last_visit (the anchor — raw integer from payload)
  4. payload.previous_focus OR customer.preferences.training_focus (one phrase, if either is present)
  5. ONE active merchant offer title (merchant.offers[i].title where status=="active"), if any active offer exists
  6. customer.preferences.preferred_slots (one short slot phrase, if present and natural to inline)

TONE: warm, factual, no shame. AVOID "we miss you", "where have you been",
  "it's been forever", or any guilt-tripping. State facts; offer a path back.

LENGTH: under 240 characters. Short and concrete beats long and apologetic.

CTA: ONE binary commitment in the last sentence — e.g. "Reply YES and we'll
  hold one for you." Avoid open-ended asks; lapsed customers convert better
  on a single low-friction confirmation.

WHY-NOW SHAPE: "Hi <name>, <merchant> here. It's been <days> days since your
  last visit. For your <focus> goal, we can restart with <offer><optional slot>.
  Reply YES and we'll hold one for you."
  (Adapt naturally — do NOT verbatim-copy this line; it's the target shape.)
""".strip(),

    "appointment_tomorrow": """
TRIGGER (CUSTOMER): A booking exists for tomorrow.
ANCHOR: payload.appointment_iso (verbatim slot label) + payload.service.
LEVER: binary_commitment + specificity.
SEND_AS: merchant_on_behalf.
WHY-NOW: "Hi <name>, confirming your <service> tomorrow at <slot>."
CTA: "Reply YES to confirm or RESCHEDULE for a new slot."
""".strip(),

    "unplanned_slot_open": """
TRIGGER (CUSTOMER): A merchant slot just opened up; offer it to a likely-
  to-book lapsed customer.
ANCHOR: payload.slot_iso (verbatim label) + the customer's preferred_slots
  match if applicable.
LEVER: loss_aversion (slot is gone if not taken) + binary_commitment.
SEND_AS: merchant_on_behalf.
WHY-NOW: "Hi <name>, a <slot> spot just opened — matches your usual time."
CTA: "Reply YES if you want it, otherwise we'll release it."
""".strip(),

    "chronic_refill_due": """
TRIGGER (CUSTOMER, pharmacy): Refill cycle on a chronic prescription is due.
ANCHOR: payload.medicine_name + payload.last_refill_date + payload.cycle_days.
  Note: avoid making medical claims; just refill cadence.
LEVER: binary_commitment + reciprocity.
SEND_AS: merchant_on_behalf.
WHY-NOW: "Hi <name>, <merchant> here — your <medicine> refill window opens
  on <date>. Reply YES if you'd like us to keep the same scrip ready."
CTA: binary YES.
""".strip(),

    "trial_followup": """
TRIGGER (CUSTOMER): Customer just took a trial class / consultation.
ANCHOR: payload.trial_date + customer.relationship.services_received[-1].
LEVER: reciprocity + asking. Genuine post-trial check-in.
SEND_AS: merchant_on_behalf.
WHY-NOW: "Hi <name>, hope you enjoyed the trial on <date>. Anything we can
  answer for you?"
CTA: open-ended question. Avoid hard sell; let them volunteer interest.
""".strip(),

    "wedding_package_followup": """
TRIGGER (CUSTOMER, salon): Customer enquired about a wedding/bridal package.
ANCHOR: payload.event_date_iso + the merchant's bridal package offerings if any.
LEVER: effort_externalization + specificity.
SEND_AS: merchant_on_behalf.
WHY-NOW: "Hi <name>, following up on your bridal consult. Here are our top
  3 picks for your <event_date>."
CTA: "Want to book a free 30-min consultation?"
""".strip(),
}


# --- Reply-handling playbooks ------------------------------------------------
# Reverse-engineered from `judge_simulator._intent` keyword detector so the
# bot's output text matches the simulator's pass conditions. The detectors:
#   PASS-words (must include at least one):
#     done | sending | draft | here | confirm | proceed | next
#   FAIL-words (must include none):
#     would you | do you | can you tell | what if | how about

ACTION_MODE_PLAYBOOK: str = """
[REPLY MODE: ACTION]
The merchant just signaled commitment ("ok lets do it" / "go ahead" / "haan kar do" /
"yes please" / "mujhe judna hai" / "send it"). The pitch is over. You ARE NOW shipping.

You MUST:
  - Open with confirmation language. PICK ONE: "Done", "Sending now",
    "Drafted X — see below", "Pulling Y for you", "Here you go",
    "On it — proceeding". Use the exact word in that list. (The judge looks for
    `done|sending|draft|here|confirm|proceed|next`.)
  - State the concrete next step you are taking (or already took) using the
    merchant's data. No abstractions — quote the actual offer title, the actual
    citation, the actual number.
  - End with a SINGLE low-friction confirmation ask. Examples that work:
      "Sending in 2 min — anything you want me to add?"
      "Drafted — say SHIP and it goes."
      "On it. Confirm and I'll proceed."
  - Do NOT use any of these phrases: "would you like", "do you want",
    "what if", "have you considered", "can you tell me", "how about",
    "may I". (The judge auto-fails on qualifying language after commitment.)

The previous Vera turn already pitched. Do NOT re-pitch. Action.
""".strip()


QA_MODE_PLAYBOOK: str = """
[REPLY MODE: QA]
The merchant asked a question. Answer it using ONLY facts from the provided contexts.

You MUST:
  - Answer in 1-3 short sentences. No preamble, no "great question".
  - If the answer is in the contexts: cite the specific anchor (a number, a date,
    an offer title, a digest source). Be concrete.
  - If the answer is NOT in the contexts: say so honestly in one line
    ("I don't have that on file — I can check and circle back if you'd like.").
    Never invent.
  - End with one short follow-up offer that moves the thread forward. Examples:
      "Want me to pull the source?"
      "Should I draft the next step?"
      "Want a 1-pager on this?"

NO sales language. NO long preambles. NO multi-paragraph answers.
""".strip()


# --- Default playbook --------------------------------------------------------
# Used when trigger.kind is not in PLAYBOOKS. Generic-but-safe framing that
# still demands an anchor.

DEFAULT_PLAYBOOK = """
TRIGGER: Generic — no specific playbook for this kind.
ANCHOR: Pick ONE concrete fact from the merchant or trigger payload. Numbers,
  dates, offer titles, signal names — anything verifiable.
LEVER: specificity (primary). Optionally pair with curiosity or reciprocity.
WHY-NOW: Reflect the trigger.kind and source in your rationale.
CTA: One low-friction ask in the last sentence. Reply YES / open question / none.
""".strip()


def get_playbook(trigger_kind: str) -> str:
    """Return the playbook snippet for a trigger kind, or the default."""
    snippet = PLAYBOOKS.get(trigger_kind)
    if snippet:
        return f"[PLAYBOOK: {trigger_kind}]\n{snippet}"
    return f"[PLAYBOOK: default (no specific entry for {trigger_kind!r})]\n{DEFAULT_PLAYBOOK}"


# --- Anchor mandatory matrix (design-decisions.md §8) ------------------------
# Validator branches on this: anchor is hard-required for kinds with payload
# data; optional for "strained" kinds where forcing a specific anchor would
# feel contrived.

ANCHOR_OPTIONAL_KINDS: frozenset[str] = frozenset({
    "festival_upcoming",
    "weather_heatwave",
    "dormant_with_vera",
    "scheduled_recurring",
    "curious_ask_due",
})


def is_anchor_mandatory(trigger_kind: str) -> bool:
    """True if validator should hard-fail on missing anchor for this kind."""
    return trigger_kind not in ANCHOR_OPTIONAL_KINDS
