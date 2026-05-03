"""Evidence hint construction for compose prompts.

The raw contexts are still authoritative. This module builds a compact fact
menu that reduces provider-specific hallucination without turning trigger
composition into deterministic templates.
"""

from __future__ import annotations

import json
from typing import Any


def build_evidence_hints(
    *,
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: dict[str, Any] | None,
) -> list[str]:
    """Return body-safe facts the composer may use as concrete claims."""
    ident = merchant.get("identity", {}) or {}
    perf = merchant.get("performance", {}) or {}
    delta = perf.get("delta_7d", {}) or {}
    offers = [o for o in (merchant.get("offers", []) or []) if o.get("status") == "active"]
    cust_agg = merchant.get("customer_aggregate", {}) or {}
    payload = trigger.get("payload", {}) or {}
    kind = trigger.get("kind", "")
    source = trigger.get("source", "")

    lines: list[str] = [
        "Priority: trigger payload/resolved digest > merchant/customer facts > active offer > category facts only if directly relevant.",
        "Do not mention facts outside this list unless they appear verbatim in the raw contexts above.",
        f"- Merchant identity: {ident.get('name')} in {ident.get('locality')}, {ident.get('city')}; owner={ident.get('owner_first_name')}; verified={ident.get('verified')}.",
    ]

    if perf:
        lines.append(
            "- Merchant 30d performance: "
            f"views={perf.get('views')}, calls={perf.get('calls')}, "
            f"directions={perf.get('directions')}, leads={perf.get('leads')}, ctr={perf.get('ctr')}."
        )
    if delta:
        lines.append(
            "- Merchant 7d deltas: "
            f"views={_pct(delta.get('views_pct'))}, calls={_pct(delta.get('calls_pct'))}, ctr={_pct(delta.get('ctr_pct'))}."
        )
    if offers:
        offer_titles = "; ".join(o.get("title", "?") for o in offers[:4])
        lines.append(f"- Active offers: {offer_titles}.")
    elif "no_active_offers" in (merchant.get("signals", []) or []):
        lines.append("- Active offers: none listed.")
    if cust_agg:
        lines.append("- Merchant customer aggregate: " + ", ".join(f"{k}={v}" for k, v in cust_agg.items()) + ".")

    review_themes = merchant.get("review_themes", []) or []
    if review_themes:
        rendered = []
        for r in review_themes[:3]:
            bit = f"{r.get('theme')} {r.get('sentiment')} {r.get('occurrences_30d')}x/30d"
            if r.get("common_quote"):
                bit += f" quote={r.get('common_quote')!r}"
            rendered.append(bit)
        lines.append("- Review themes: " + "; ".join(rendered) + ".")

    if payload:
        lines.append("- Trigger payload facts: " + _render_payload_facts(payload) + ".")

    digest_item = _resolve_digest_item(category, payload)
    if digest_item:
        digest_bits = [
            f"id={digest_item.get('id')}",
            f"title={digest_item.get('title')}",
            f"source={digest_item.get('source')}",
        ]
        for key in ("date", "credits", "trial_n", "summary", "actionable"):
            if digest_item.get(key) is not None:
                digest_bits.append(f"{key}={digest_item.get(key)}")
        lines.append("- Resolved category digest item: " + "; ".join(digest_bits) + ".")

    if customer is not None:
        c_ident = customer.get("identity", {}) or {}
        rel = customer.get("relationship", {}) or {}
        lines.append(
            "- Customer facts: "
            f"name={c_ident.get('name')}, language={c_ident.get('language_pref')}, "
            f"state={customer.get('state')}, last_visit={rel.get('last_visit')}, "
            f"visits_total={rel.get('visits_total')}, services={rel.get('services_received')}."
        )

    if _include_category_hints(kind, source):
        _add_category_hints(lines, category, kind)

    return lines


def _add_category_hints(lines: list[str], category: dict[str, Any], kind: str) -> None:
    peer = category.get("peer_stats", {}) or {}
    if peer:
        peer_bits = []
        for key in (
            "avg_views_30d",
            "avg_calls_30d",
            "avg_ctr",
            "retention_6mo_pct",
            "retention_3mo_pct",
            "repeat_customer_pct",
            "delivery_share_pct",
            "trial_to_paid_pct",
        ):
            if peer.get(key) is not None:
                peer_bits.append(f"{key}={peer.get(key)}")
        if peer_bits:
            lines.append("- Category peer benchmarks: " + ", ".join(peer_bits) + ".")

    seasonal = category.get("seasonal_beats", []) or []
    if seasonal and kind in {
        "festival_upcoming",
        "category_seasonal",
        "seasonal_perf_dip",
        "winback_eligible",
        "weather_heatwave",
        "ipl_match_today",
    }:
        lines.append(
            "- Relevant seasonal beats: "
            + "; ".join(f"{s.get('month_range')}: {s.get('note')}" for s in seasonal[:4])
            + "."
        )

    trends = category.get("trend_signals", []) or []
    if trends and kind in {"curious_ask_due", "category_trend_movement", "ipl_match_today", "gbp_unverified"}:
        lines.append(
            "- Category trend signals: "
            + "; ".join(f"{t.get('query')} {_pct(t.get('delta_yoy'))} YoY" for t in trends[:5])
            + "."
        )


def _pct(v: Any) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v) * 100:+.1f}%"
    except (TypeError, ValueError):
        return str(v)


def _render_payload_facts(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            short = ", ".join(json.dumps(v, ensure_ascii=False) for v in value[:4])
            parts.append(f"{key}=[{short}]")
        elif isinstance(value, dict):
            parts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _resolve_digest_item(category: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    digest_id = (
        payload.get("top_item_id")
        or payload.get("digest_item_id")
        or payload.get("alert_id")
        or payload.get("event_id")
    )
    if not digest_id:
        return None
    for item in category.get("digest", []) or []:
        if item.get("id") == digest_id:
            return item
    return None


def _include_category_hints(kind: str, source: str) -> bool:
    if source == "external":
        return True
    return kind in {
        "curious_ask_due",
        "gbp_unverified",
        "category_seasonal",
        "seasonal_perf_dip",
        "festival_upcoming",
        "winback_eligible",
        "dormant_with_vera",
        "scheduled_recurring",
    }
