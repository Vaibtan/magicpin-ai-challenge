"""Provider overrides for the local judge wrapper.

The bundled judge_simulator.py predates Gemini 2.5/3 thinking models. Its
Gemini REST adapter allocates only 1500 output tokens, which those models can
spend entirely on hidden thinking before producing visible text. This module
keeps the supplied simulator file untouched while replacing that adapter for
our wrapper/offline scoring entry points.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


def configure_utf8_stdio() -> None:
    """Make script output safe for Hindi text and score bars on Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def configure_judge_from_env(
    js: Any,
    *,
    default_provider: str = "anthropic",
    default_gemini_model: str = "gemini-2.5-pro",
) -> None:
    """Apply judge_simulator module-level config from environment variables."""
    provider = (os.getenv("JUDGE_LLM_PROVIDER", default_provider) or default_provider).strip().lower()
    js.LLM_PROVIDER = provider

    if provider == "anthropic":
        js.LLM_API_KEY = os.getenv("ANTHROPIC_API_KEY", "") or js.LLM_API_KEY
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "claude-sonnet-4-6") or js.LLM_MODEL
    elif provider == "openai":
        js.LLM_API_KEY = os.getenv("OPENAI_API_KEY", "") or js.LLM_API_KEY
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "gpt-4o") or js.LLM_MODEL
    elif provider == "gemini":
        js.LLM_API_KEY = (
            os.getenv("GEMINI_API_KEY", "")
            or os.getenv("GOOGLE_API_KEY", "")
            or os.getenv("LLM_API_KEY", "")
            or js.LLM_API_KEY
        )
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", default_gemini_model) or js.LLM_MODEL
    elif provider == "deepseek":
        js.LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "") or js.LLM_API_KEY
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", js.LLM_MODEL) or js.LLM_MODEL
    elif provider == "groq":
        js.LLM_API_KEY = os.getenv("GROQ_API_KEY", "") or js.LLM_API_KEY
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", js.LLM_MODEL) or js.LLM_MODEL
    elif provider == "openrouter":
        js.LLM_API_KEY = os.getenv("OPENROUTER_API_KEY", "") or js.LLM_API_KEY
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", js.LLM_MODEL) or js.LLM_MODEL
    else:
        js.LLM_API_KEY = os.getenv("LLM_API_KEY", "") or js.LLM_API_KEY
        js.LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", js.LLM_MODEL) or js.LLM_MODEL

    js.BOT_URL = os.getenv("BOT_URL", js.BOT_URL)


def patch_judge_simulator(js: Any) -> None:
    """Patch judge_simulator's Gemini provider and scoring prompt.

    The stock simulator is useful, but it has two local-dev gaps:
    - Gemini thinking models need a larger output budget than 1500 tokens.
    - LLMScorer.score only shows the scorer a narrow context summary and omits
      rationale, while the challenge brief says the judge receives the full
      context artifacts and considers rationale quality.
    """

    class PatchedGeminiProvider(js.LLMProvider):
        def __init__(self, api_key: str, model: str = ""):
            self.api_key = api_key
            self.model = model or "gemini-2.5-flash"
            self.max_output_tokens = int(os.getenv("JUDGE_MAX_OUTPUT_TOKENS", "12000"))
            self.temperature = float(os.getenv("JUDGE_TEMPERATURE", "0.2"))

        def name(self) -> str:
            return f"Gemini ({self.model})"

        def complete(self, prompt: str, system: str = None) -> str:
            full_prompt = f"{system}\n\n{prompt}" if system else prompt
            generation_config: dict[str, Any] = {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_output_tokens,
            }
            if _expects_json(prompt, system):
                generation_config["responseMimeType"] = "application/json"

            thinking_budget = os.getenv("JUDGE_GEMINI_THINKING_BUDGET")
            if thinking_budget not in (None, ""):
                generation_config["thinkingConfig"] = {
                    "thinkingBudget": int(thinking_budget),
                }

            body = json.dumps({
                "contents": [{"parts": [{"text": full_prompt}]}],
                "generationConfig": generation_config,
            }).encode("utf-8")

            req = urlrequest.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent?key={self.api_key}",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                resp = urlrequest.urlopen(req, timeout=js.TIMEOUT_LLM)
                data = json.loads(resp.read().decode("utf-8"))
            except urlerror.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc

            candidate = (data.get("candidates") or [{}])[0]
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
            if not text:
                finish = candidate.get("finishReason", "unknown")
                usage = data.get("usageMetadata", {})
                raise RuntimeError(
                    "Gemini returned no visible text "
                    f"(finishReason={finish}, usage={_compact_usage(usage)})"
                )
            return text

    js.GeminiProvider = PatchedGeminiProvider
    _patch_full_context_scorer(js)


def _patch_full_context_scorer(js: Any) -> None:
    if getattr(js.LLMScorer, "_vera_full_context_patch", False):
        return

    def score(self: Any, action: dict[str, Any], category: dict[str, Any],
              merchant: dict[str, Any], trigger: dict[str, Any],
              customer: dict[str, Any] | None = None) -> Any:
        body = action.get("body", "")
        prompt = f"""SCORE THIS MESSAGE:

Judge against ONLY the contexts below. They are the full contexts pushed to the
bot. A fact is verifiable if it appears in these JSON contexts or is a natural
formatting of a value in them (for example, 0.30 may be written as 30%).

=== FULL CATEGORY CONTEXT ===
{_json_for_scoring(_category_for_scoring(category))}

=== FULL MERCHANT CONTEXT ===
{_json_for_scoring(_merchant_for_scoring(merchant))}

=== FULL TRIGGER CONTEXT ===
{_json_for_scoring(trigger)}

=== FULL CUSTOMER CONTEXT ===
{_json_for_scoring(customer) if customer else "None (merchant-facing)"}

=== BOT ACTION ===
Body ({len(body)} chars): "{body}"
CTA: {action.get('cta', 'none')}
Send As: {action.get('send_as', 'vera')}
Suppression Key: {action.get('suppression_key', '')}
Rationale: {action.get('rationale', '')}

Score each dimension 0-10 with clear reasoning. Be STRICT."""
        try:
            js.print_llm("Analyzing message...")
            response = self.llm.complete(prompt, self.SYSTEM)
            return self._parse_response(response, action)
        except Exception as exc:
            js.print_warn(f"LLM error: {exc}")
            return self._fallback_score(action)

    js.LLMScorer.score = score
    js.LLMScorer._vera_full_context_patch = True


def _category_for_scoring(category: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": category.get("slug"),
        "display_name": category.get("display_name"),
        "voice": category.get("voice"),
        "offer_catalog": category.get("offer_catalog"),
        "peer_stats": category.get("peer_stats"),
        "digest": category.get("digest"),
        "seasonal_beats": category.get("seasonal_beats"),
        "trend_signals": category.get("trend_signals"),
    }


def _merchant_for_scoring(merchant: dict[str, Any]) -> dict[str, Any]:
    return {
        "merchant_id": merchant.get("merchant_id"),
        "category_slug": merchant.get("category_slug"),
        "identity": merchant.get("identity"),
        "subscription": merchant.get("subscription"),
        "performance": merchant.get("performance"),
        "offers": merchant.get("offers"),
        "conversation_history": merchant.get("conversation_history"),
        "customer_aggregate": merchant.get("customer_aggregate"),
        "signals": merchant.get("signals"),
        "review_themes": merchant.get("review_themes"),
    }


def _json_for_scoring(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _expects_json(prompt: str, system: str | None) -> bool:
    text = f"{system or ''}\n{prompt}"
    return (
        "RESPOND ONLY WITH THIS EXACT JSON FORMAT" in text
        or "Respond with valid JSON only" in text
    )


def _compact_usage(usage: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "promptTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
    )
    return {k: usage[k] for k in keys if k in usage}
