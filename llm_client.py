"""LLM client: Anthropic primary, OpenAI fallback, two-cache strategy.

Public surface
--------------
    compose_call(skeleton_text, category_text, dynamic_text, *, ...) -> ComposeCallResult
    classify_call(prompt, *, ...) -> dict

Caching (design-decisions.md §9)
--------------------------------
1. Anthropic prompt-cache — 2 ephemeral breakpoints (skeleton + category).
   Provider-side, ~5-min TTL, ~90% read discount on cached prefix.
2. Local response-cache  — full-input-hash key, gitignored JSONL.
   Hit returns the parsed JSON with zero LLM call. Byte-identical reruns.

Determinism
-----------
- temperature=0 everywhere.
- All identifying inputs (prompt_version, model, skeleton, category, merchant,
  trigger, customer, playbook, conv_state) feed the cache key. Any mutation
  busts the cache automatically — no stale messages.

Fallback
--------
- Single hop: Anthropic -> OpenAI on 5xx / 429 / timeout.
- All calls wrapped in asyncio.wait_for with a per-call ceiling that stays
  inside the tick wall-clock budget (25s).
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import anthropic
import openai
from dotenv import load_dotenv

from obs import log_event

load_dotenv()

# ---- model + budget config -------------------------------------------------

SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
OPENAI_COMPOSE_MODEL = "gpt-4o"
OPENAI_CLASSIFY_MODEL = "gpt-4o-mini"

LLM_CALL_TIMEOUT_S = float(os.getenv("LLM_CALL_TIMEOUT_S", "22.0"))
COMPOSE_MAX_TOKENS = 1500
CLASSIFY_MAX_TOKENS = 400

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESPONSE_CACHE_FILE = CACHE_DIR / "llm_responses.jsonl"


# ---- response cache --------------------------------------------------------


class ResponseCache:
    """Append-only JSONL cache; full-input-hash key. Lazy-loaded on first hit.

    Append-only is intentional: cache writes during a tick are non-blocking
    (no rewrite of prior lines), and the file is greppable post-run.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mem: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

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
        self._load_sync()
        return self._mem.get(key)

    async def put(self, key: str, response: dict[str, Any]) -> None:
        async with self._lock:
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
    """Compose-path LLM call with two-cache strategy + fallback.

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

    # 1. Local response-cache lookup (zero LLM call on hit)
    primary_model = SONNET_MODEL
    cache_key = _compose_cache_key(
        prompt_version=prompt_version,
        model=primary_model,
        skeleton_id=skeleton_id,
        category_id=category_id,
        skeleton_text=skeleton_text,
        category_text=category_text,
        dynamic_text=dynamic_text,
        extra=cache_payload_extra,
    )
    cached = await _cache.get(cache_key)
    if cached is not None:
        log_event("cache_hit", path="compose", cache_key=cache_key, **log_ctx)
        return ComposeCallResult(
            json=cached["json"],
            model=cached.get("model", primary_model),
            cache_hit=True,
            latency_ms=0,
            input_tokens_cached=0,
            input_tokens_uncached=0,
            output_tokens=0,
            fallback_used=cached.get("fallback_used", False),
        )

    log_event("cache_miss", path="compose", cache_key=cache_key, **log_ctx)

    # 2. Anthropic primary
    start = time.monotonic()
    try:
        outcome = await _anthropic_compose(skeleton_text, category_text, dynamic_text)
        latency_ms = int((time.monotonic() - start) * 1000)
        await _cache.put(cache_key, {
            "json": outcome["json"],
            "model": outcome["model"],
            "fallback_used": False,
        })
        return ComposeCallResult(
            json=outcome["json"],
            model=outcome["model"],
            cache_hit=False,
            latency_ms=latency_ms,
            input_tokens_cached=outcome["input_cached"],
            input_tokens_uncached=outcome["input_uncached"],
            output_tokens=outcome["output"],
            fallback_used=False,
        )
    except Exception as exc:
        log_event("anthropic_error_falling_back", path="compose", error=str(exc),
                  error_type=type(exc).__name__, **log_ctx)

    # 3. OpenAI fallback
    start = time.monotonic()
    outcome = await _openai_compose(skeleton_text, category_text, dynamic_text)
    latency_ms = int((time.monotonic() - start) * 1000)
    await _cache.put(cache_key, {
        "json": outcome["json"],
        "model": outcome["model"],
        "fallback_used": True,
    })
    return ComposeCallResult(
        json=outcome["json"],
        model=outcome["model"],
        cache_hit=False,
        latency_ms=latency_ms,
        input_tokens_cached=0,
        input_tokens_uncached=outcome["input"],
        output_tokens=outcome["output"],
        fallback_used=True,
    )


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


# ---- classify_call --------------------------------------------------------


async def classify_call(
    prompt: str,
    *,
    prompt_version: str,
    cache_key_extra: str = "",
    log_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cheap classification call (Haiku primary, gpt-4o-mini fallback)."""
    log_ctx = log_context or {}

    # Cache key — different namespace from compose to avoid collisions
    parts = (prompt_version, HAIKU_MODEL, "classify",
             hash_payload(prompt), cache_key_extra)
    cache_key = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    cached = await _cache.get(cache_key)
    if cached is not None:
        log_event("cache_hit", path="classify", cache_key=cache_key, **log_ctx)
        return cached["json"]

    log_event("cache_miss", path="classify", cache_key=cache_key, **log_ctx)

    try:
        outcome = await _anthropic_classify(prompt)
        await _cache.put(cache_key, {
            "json": outcome["json"], "model": outcome["model"], "fallback_used": False,
        })
        return outcome["json"]
    except Exception as exc:
        log_event("anthropic_error_falling_back", path="classify", error=str(exc),
                  error_type=type(exc).__name__, **log_ctx)

    outcome = await _openai_classify(prompt)
    await _cache.put(cache_key, {
        "json": outcome["json"], "model": outcome["model"], "fallback_used": True,
    })
    return outcome["json"]


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
