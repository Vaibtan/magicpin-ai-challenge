"""LLM client: selectable provider (Anthropic | OpenAI | Gemini) + two-cache strategy.

Public surface
--------------
    compose_call(skeleton_text, category_text, dynamic_text, *, ...) -> ComposeCallResult
    classify_call(prompt, *, ...) -> dict

Provider selection
------------------
- `LLM_PROVIDER`           env var, default "anthropic". Values: anthropic | openai | gemini.
- `LLM_FALLBACK_PROVIDER`  env var, default "openai".    Values: anthropic | openai | gemini | none.
  Set to "none" if you only have one provider's key.
- Each provider uses its own compose-model + classify-model pair (see constants below).

Caching (design-decisions.md §9)
--------------------------------
1. Anthropic prompt-cache — 2 ephemeral breakpoints (skeleton + category).
   Provider-side, ~5-min TTL, ~90% read discount on cached prefix.
   Active only when Anthropic is in the chain; Gemini relies on its own
   implicit prompt caching (provider-side, no API surface).
2. Local response-cache  — full-input-hash key, gitignored JSONL.
   Hit returns the parsed JSON with zero LLM call. Byte-identical reruns.
   Cache key embeds the model name, so different providers never collide.

Determinism
-----------
- temperature=0 everywhere.
- All identifying inputs (prompt_version, model, skeleton, category, merchant,
  trigger, customer, playbook, conv_state) feed the cache key. Any mutation
  busts the cache automatically — no stale messages.

Fallback
--------
- Each call walks the chain (primary → fallback). Each provider has its own
  cache key, so a fallback hit doesn't poison the primary's cache.
- All calls wrapped in asyncio.wait_for with a per-call ceiling that stays
  inside the tick wall-clock budget.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
import anthropic
import openai
from dotenv import load_dotenv

from obs import log_event

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ---- model + budget config -------------------------------------------------

SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
OPENAI_COMPOSE_MODEL = "gpt-4o"
OPENAI_CLASSIFY_MODEL = "gpt-4o-mini"
GEMINI_COMPOSE_MODEL = os.getenv("GEMINI_COMPOSE_MODEL") or "gemini-2.5-pro"
GEMINI_CLASSIFY_MODEL = os.getenv("GEMINI_CLASSIFY_MODEL") or "gemini-2.5-flash"

# Keep one primary call plus one fallback inside the tick envelope.
# Bumped from 10s → 14s: concurrent tick calls (3-way asyncio.gather) get
# queued by Gemini's free-tier preview quotas. 14s gives slack while staying
# under the simulator's 15s tick deadline.
LLM_CALL_TIMEOUT_S = float(os.getenv("LLM_CALL_TIMEOUT_S", "14.0"))
# Gemini 3 preview family does silent "thinking" by default and may ignore
# thinking_budget=0 — the hidden CoT then consumes most of max_output_tokens,
# truncating the visible JSON. 12000 absorbs that overhead so even a verbose
# Hinglish reply has room. Cost stays low (we set thinking_budget=0; only the
# unused-but-allocated ceiling rises).
COMPOSE_MAX_TOKENS = int(os.getenv("COMPOSE_MAX_TOKENS", "12000"))
CLASSIFY_MAX_TOKENS = int(os.getenv("CLASSIFY_MAX_TOKENS", "2000"))

CACHE_DIR = ROOT / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESPONSE_CACHE_FILE = CACHE_DIR / "llm_responses.jsonl"


# ---- response cache --------------------------------------------------------


class ResponseCache:
    """Append-only JSONL cache; full-input-hash key. Lazy-loaded on first hit.

    Append-only is intentional: cache writes avoid rewriting prior lines, and
    the file is greppable post-run. A threading lock is used because the public
    sync compose() wrapper may run this client from short-lived event loops.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mem: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = threading.RLock()

    def _load_sync(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self._mem[rec["key"]] = rec["response"]
                except Exception:
                    continue

    async def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            self._load_sync()
            return self._mem.get(key)

    async def put(self, key: str, response: dict[str, Any]) -> None:
        with self._lock:
            self._load_sync()
            self._mem[key] = response
            line = json.dumps({"key": key, "response": response}, ensure_ascii=False) + "\n"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)


_cache = ResponseCache(RESPONSE_CACHE_FILE)


# ---- key helpers -----------------------------------------------------------


def hash_payload(obj: Any) -> str:
    """Stable hash for a JSON-serializable payload. Used in cache keys."""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _compose_cache_key(
    *,
    prompt_version: str,
    model: str,
    skeleton_id: str,
    category_id: str,
    skeleton_text: str,
    category_text: str,
    dynamic_text: str,
    extra: dict[str, Any] | None,
) -> str:
    h = hashlib.sha256()
    for part in (
        prompt_version, model, skeleton_id, category_id,
        hash_payload(skeleton_text), hash_payload(category_text),
        hash_payload(dynamic_text), hash_payload(extra or {}),
    ):
        h.update(part.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


# ---- client factories (lazy) ----------------------------------------------

_anthropic_async: anthropic.AsyncAnthropic | None = None
_openai_async: openai.AsyncOpenAI | None = None
_gemini_async: Any | None = None


def _anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic_async
    if _anthropic_async is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _anthropic_async = anthropic.AsyncAnthropic(api_key=key)
    return _anthropic_async


def _openai() -> openai.AsyncOpenAI:
    global _openai_async
    if _openai_async is None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        _openai_async = openai.AsyncOpenAI(api_key=key)
    return _openai_async


def _gemini() -> Any:
    global _gemini_async
    if _gemini_async is None:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set")
        from google import genai
        _gemini_async = genai.Client(api_key=key)
    return _gemini_async


# ---- JSON extraction (defensive) ------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerate fenced code blocks + leading/trailing prose around the JSON."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").lstrip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    a = s.find("{")
    b = s.rfind("}")
    if a == -1 or b == -1 or b <= a:
        raise ValueError(f"No JSON object found in LLM response: {text[:200]!r}")
    return json.loads(s[a:b + 1])


# ---- provider chain --------------------------------------------------------

_VALID_PROVIDERS = ("anthropic", "openai", "gemini")


def _resolve_chain() -> list[str]:
    """Ordered list of providers to try (primary first, fallback second).

    Driven by env: LLM_PROVIDER (default "anthropic") + LLM_FALLBACK_PROVIDER
    (default "openai"). "none" disables the fallback. Unknown values are
    ignored so a typo can't silently swap providers.
    """
    primary = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
    fallback = (os.getenv("LLM_FALLBACK_PROVIDER") or "openai").strip().lower()
    chain: list[str] = []
    if primary in _VALID_PROVIDERS:
        chain.append(primary)
    if fallback in _VALID_PROVIDERS and fallback not in chain:
        chain.append(fallback)
    if not chain:
        # Defensive: if both env values are bogus, fall back to historical default
        chain = ["anthropic", "openai"]
    return chain


def _compose_model_for(provider: str) -> str:
    return {
        "anthropic": SONNET_MODEL,
        "openai": OPENAI_COMPOSE_MODEL,
        "gemini": GEMINI_COMPOSE_MODEL,
    }[provider]


def _classify_model_for(provider: str) -> str:
    return {
        "anthropic": HAIKU_MODEL,
        "openai": OPENAI_CLASSIFY_MODEL,
        "gemini": GEMINI_CLASSIFY_MODEL,
    }[provider]


# ---- compose_call ---------------------------------------------------------


@dataclass
class ComposeCallResult:
    """Compose-call return type. Hides the provider behind a uniform shape."""

    json: dict[str, Any]
    model: str
    cache_hit: bool
    latency_ms: int
    input_tokens_cached: int
    input_tokens_uncached: int
    output_tokens: int
    fallback_used: bool


async def compose_call(
    skeleton_text: str,
    category_text: str,
    dynamic_text: str,
    *,
    skeleton_id: str,
    category_id: str,
    prompt_version: str,
    cache_payload_extra: dict[str, Any] | None = None,
    log_context: dict[str, Any] | None = None,
) -> ComposeCallResult:
    """Compose-path LLM call with two-cache strategy + provider chain.

    Args:
        skeleton_text:        cached prefix (system prompt skeleton).
        category_text:        cached prefix (CategoryContext serialization).
        dynamic_text:         uncached suffix (merchant + trigger + customer + playbook).
        skeleton_id:          short identifier, e.g. "merchant_facing".
        category_id:          slug of the category (e.g. "dentists").
        prompt_version:       from prompts.PROMPT_VERSION; busts cache on bump.
        cache_payload_extra:  extra dict folded into the cache key (typically the
                              hashes of merchant + trigger + customer payloads).
        log_context:          extra fields propagated into structured logs.

    Returns: ComposeCallResult with parsed JSON + telemetry.
    """
    log_ctx = log_context or {}
    chain = _resolve_chain()
    last_exc: Exception | None = None

    for idx, provider in enumerate(chain):
        is_fallback = idx > 0
        model_id = _compose_model_for(provider)
        cache_key = _compose_cache_key(
            prompt_version=prompt_version,
            model=model_id,
            skeleton_id=skeleton_id,
            category_id=category_id,
            skeleton_text=skeleton_text,
            category_text=category_text,
            dynamic_text=dynamic_text,
            extra=cache_payload_extra,
        )

        cached = await _cache.get(cache_key)
        if cached is not None:
            log_event("cache_hit", path="compose", cache_key=cache_key,
                      model=model_id, provider=provider, fallback=is_fallback, **log_ctx)
            return ComposeCallResult(
                json=cached["json"],
                model=cached.get("model", model_id),
                cache_hit=True,
                latency_ms=0,
                input_tokens_cached=0,
                input_tokens_uncached=0,
                output_tokens=0,
                fallback_used=is_fallback or cached.get("fallback_used", False),
            )

        log_event("cache_miss", path="compose", cache_key=cache_key,
                  model=model_id, provider=provider, fallback=is_fallback, **log_ctx)

        start = time.monotonic()
        try:
            outcome = await _provider_compose(provider, skeleton_text, category_text, dynamic_text)
            latency_ms = int((time.monotonic() - start) * 1000)
            await _cache.put(cache_key, {
                "json": outcome["json"],
                "model": outcome["model"],
                "fallback_used": is_fallback,
            })
            return ComposeCallResult(
                json=outcome["json"],
                model=outcome["model"],
                cache_hit=False,
                latency_ms=latency_ms,
                input_tokens_cached=outcome.get("input_cached", 0),
                input_tokens_uncached=outcome.get("input_uncached", outcome.get("input", 0)),
                output_tokens=outcome["output"],
                fallback_used=is_fallback,
            )
        except Exception as exc:
            last_exc = exc
            log_event(f"{provider}_error_falling_back", path="compose", error=str(exc),
                      error_type=type(exc).__name__, **log_ctx)
            continue

    raise RuntimeError(
        f"all compose providers failed (chain={chain}): {last_exc}"
    ) from last_exc


async def _provider_compose(
    provider: str, skeleton: str, category: str, dynamic: str,
) -> dict[str, Any]:
    if provider == "anthropic":
        return await _anthropic_compose(skeleton, category, dynamic)
    if provider == "openai":
        return await _openai_compose(skeleton, category, dynamic)
    if provider == "gemini":
        return await _gemini_compose(skeleton, category, dynamic)
    raise ValueError(f"unknown provider: {provider}")


async def _anthropic_compose(skeleton: str, category: str, dynamic: str) -> dict[str, Any]:
    """Single Anthropic call with two ephemeral cache breakpoints."""
    client = _anthropic()
    resp = await asyncio.wait_for(
        client.messages.create(
            model=SONNET_MODEL,
            max_tokens=COMPOSE_MAX_TOKENS,
            temperature=0,
            system=[
                {"type": "text", "text": skeleton, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": category, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": dynamic},
                ],
            }],
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    parsed = _extract_json(text)
    usage = resp.usage
    return {
        "json": parsed,
        "model": SONNET_MODEL,
        "input_cached": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "input_uncached": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
    }


async def _openai_compose(skeleton: str, category: str, dynamic: str) -> dict[str, Any]:
    client = _openai()
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=OPENAI_COMPOSE_MODEL,
            messages=[
                {"role": "system", "content": skeleton},
                {"role": "user", "content": category + "\n\n" + dynamic},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=COMPOSE_MAX_TOKENS,
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )
    parsed = _extract_json(resp.choices[0].message.content or "")
    return {
        "json": parsed,
        "model": OPENAI_COMPOSE_MODEL,
        "input": resp.usage.prompt_tokens,
        "output": resp.usage.completion_tokens,
    }


async def _gemini_compose(skeleton: str, category: str, dynamic: str) -> dict[str, Any]:
    """Gemini compose call. Skeleton goes in `system_instruction`; category +
    dynamic concatenated as user content. Implicit prompt caching (no API
    surface) handles the cached-prefix discount on repeated runs.

    Thinking disabled (thinking_budget=0): Gemini 3 family does hidden CoT by
    default that consumes output tokens, causing JSON truncation on longer
    Hinglish bodies. We don't need reasoning for short message composition.
    """
    from google.genai import types  # noqa: WPS433 — defer SDK import until needed
    client = _gemini()
    resp = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=GEMINI_COMPOSE_MODEL,
            contents=category + "\n\n" + dynamic,
            config=types.GenerateContentConfig(
                system_instruction=skeleton,
                temperature=0,
                response_mime_type="application/json",
                max_output_tokens=COMPOSE_MAX_TOKENS,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        # Some safety blocks return empty .text — surface as error so chain falls through.
        raise RuntimeError(f"gemini returned empty text (finish_reason={_gemini_finish_reason(resp)})")
    cached, total, out = _gemini_usage(resp)
    thoughts = _gemini_thoughts(resp)
    finish = _gemini_finish_reason(resp)
    try:
        parsed = _extract_json(text)
    except ValueError as exc:
        # Surface why the JSON didn't parse — almost always max_tokens truncation
        # caused by hidden thinking tokens. Caller logs this through obs.log_event.
        raise RuntimeError(
            f"gemini truncated/invalid JSON: finish={finish} "
            f"prompt_tok={total} thoughts_tok={thoughts} output_tok={out}: {exc}"
        ) from exc
    return {
        "json": parsed,
        "model": GEMINI_COMPOSE_MODEL,
        "input_cached": cached,
        "input_uncached": max(total - cached, 0),
        "output": out,
    }


def _gemini_usage(resp: Any) -> tuple[int, int, int]:
    """Best-effort token-usage extraction from a google-genai response."""
    usage = getattr(resp, "usage_metadata", None)
    if usage is None:
        return 0, 0, 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    total = getattr(usage, "prompt_token_count", 0) or 0
    out = getattr(usage, "candidates_token_count", 0) or 0
    return cached, total, out


def _gemini_thoughts(resp: Any) -> int:
    """Hidden-thinking token count, if Gemini reports it. Useful for diagnosing
    JSON truncation under preview models that ignore thinking_budget=0."""
    usage = getattr(resp, "usage_metadata", None)
    if usage is None:
        return 0
    return getattr(usage, "thoughts_token_count", 0) or 0


def _gemini_finish_reason(resp: Any) -> str:
    cands = getattr(resp, "candidates", None) or []
    if not cands:
        return "no_candidates"
    return str(getattr(cands[0], "finish_reason", "unknown"))


# ---- classify_call --------------------------------------------------------


async def classify_call(
    prompt: str,
    *,
    prompt_version: str,
    cache_key_extra: str = "",
    log_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cheap classification call — walks the same provider chain as compose.

    Each provider gets its own cheap classifier model:
      anthropic → Haiku 4.5
      openai    → gpt-4o-mini
      gemini    → Flash 2.5
    """
    log_ctx = log_context or {}
    chain = _resolve_chain()
    last_exc: Exception | None = None

    def _cache_key(model: str) -> str:
        parts = (prompt_version, model, "classify", hash_payload(prompt), cache_key_extra)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    for idx, provider in enumerate(chain):
        is_fallback = idx > 0
        model_id = _classify_model_for(provider)
        key = _cache_key(model_id)

        cached = await _cache.get(key)
        if cached is not None:
            log_event("cache_hit", path="classify", cache_key=key,
                      model=model_id, provider=provider, fallback=is_fallback, **log_ctx)
            return cached["json"]

        log_event("cache_miss", path="classify", cache_key=key,
                  model=model_id, provider=provider, fallback=is_fallback, **log_ctx)

        try:
            outcome = await _provider_classify(provider, prompt)
            await _cache.put(key, {
                "json": outcome["json"],
                "model": outcome["model"],
                "fallback_used": is_fallback,
            })
            return outcome["json"]
        except Exception as exc:
            last_exc = exc
            log_event(f"{provider}_error_falling_back", path="classify", error=str(exc),
                      error_type=type(exc).__name__, **log_ctx)
            continue

    raise RuntimeError(
        f"all classify providers failed (chain={chain}): {last_exc}"
    ) from last_exc


async def _provider_classify(provider: str, prompt: str) -> dict[str, Any]:
    if provider == "anthropic":
        return await _anthropic_classify(prompt)
    if provider == "openai":
        return await _openai_classify(prompt)
    if provider == "gemini":
        return await _gemini_classify(prompt)
    raise ValueError(f"unknown provider: {provider}")


async def _anthropic_classify(prompt: str) -> dict[str, Any]:
    client = _anthropic()
    resp = await asyncio.wait_for(
        client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=CLASSIFY_MAX_TOKENS,
            temperature=0,
            messages=[{
                "role": "user",
                "content": prompt + "\n\nRespond with valid JSON only, no prose.",
            }],
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return {"json": _extract_json(text), "model": HAIKU_MODEL}


async def _openai_classify(prompt: str) -> dict[str, Any]:
    client = _openai()
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=OPENAI_CLASSIFY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=CLASSIFY_MAX_TOKENS,
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )
    return {
        "json": _extract_json(resp.choices[0].message.content or ""),
        "model": OPENAI_CLASSIFY_MODEL,
    }


async def _gemini_classify(prompt: str) -> dict[str, Any]:
    from google.genai import types  # noqa: WPS433
    client = _gemini()
    resp = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=GEMINI_CLASSIFY_MODEL,
            contents=prompt + "\n\nRespond with valid JSON only, no prose.",
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                max_output_tokens=CLASSIFY_MAX_TOKENS,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        ),
        timeout=LLM_CALL_TIMEOUT_S,
    )
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        raise RuntimeError(f"gemini returned empty text (finish_reason={_gemini_finish_reason(resp)})")
    return {"json": _extract_json(text), "model": GEMINI_CLASSIFY_MODEL}
