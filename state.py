"""In-memory stores with idempotent push semantics + optional disk snapshot.

Stores
------
ContextStore       — (scope, context_id) -> {version, payload, delivered_at}
ConversationStore  — conversation_id -> ConversationState (phase machine)
SuppressionStore   — sent_keys, in-flight reservations, last_send_ts,
                     daily_send_count

Concurrency
-----------
Writes go through a per-store asyncio.Lock. Reads are lock-free (CPython
dict-get is atomic). The judge load is ~1 req/sec, so contention is
negligible; the locks exist to serialize writes against snapshots.
Conversation reads return isolated copies; suppression reservations are used
to block duplicate sends while compose calls are in flight.

Persistence
-----------
On graceful shutdown, dump all three stores to `state_dump.json`. On
startup, load if and only if BOT_DEV_MODE=1 — the judge run starts empty
per spec (testing brief §11).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).parent
STATE_DUMP_FILE = ROOT / "state_dump.json"

# Valid context scopes per challenge-testing-brief.md §3
_VALID_SCOPES: frozenset[str] = frozenset({"category", "merchant", "customer", "trigger"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- conversation phases (design-decisions.md §4) --------------------------


class ConvPhase(str, Enum):
    INITIATED = "INITIATED"
    AWAITING_REPLY = "AWAITING_REPLY"
    ENGAGED = "ENGAGED"
    AUTO_REPLY_SUSPECTED = "AUTO_REPLY_SUSPECTED"
    EXITED = "EXITED"


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    trigger_id: str
    send_as: str
    customer_id: str | None = None
    phase: ConvPhase = ConvPhase.INITIATED
    auto_reply_count: int = 0
    last_send_ts: float = 0.0
    turns: list[dict[str, Any]] = field(default_factory=list)        # [{from, body, hash, ts, label}]
    prior_bot_hashes: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "merchant_id": self.merchant_id,
            "trigger_id": self.trigger_id,
            "send_as": self.send_as,
            "customer_id": self.customer_id,
            "phase": self.phase.value,
            "auto_reply_count": self.auto_reply_count,
            "last_send_ts": self.last_send_ts,
            "turns": copy.deepcopy(self.turns),
            "prior_bot_hashes": sorted(self.prior_bot_hashes),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConversationState":
        return cls(
            conversation_id=d["conversation_id"],
            merchant_id=d["merchant_id"],
            trigger_id=d["trigger_id"],
            send_as=d.get("send_as", "vera"),
            customer_id=d.get("customer_id"),
            phase=ConvPhase(d.get("phase", "INITIATED")),
            auto_reply_count=int(d.get("auto_reply_count", 0)),
            last_send_ts=float(d.get("last_send_ts", 0.0)),
            turns=copy.deepcopy(d.get("turns", [])),
            prior_bot_hashes=set(d.get("prior_bot_hashes", [])),
        )


# ---- ContextStore ----------------------------------------------------------


class ContextStore:
    """Idempotent (scope, context_id) -> {version, payload, delivered_at}.

    Push semantics (challenge-testing-brief.md §2.1 + design-decisions.md §6):
      - Same version for same key   -> 200, accepted=True (no-op)
      - Higher version for same key -> 200, accepted=True (atomic replace)
      - Lower version for same key  -> 409, accepted=False, current_version=N
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def push(self, scope: str, context_id: str, version: int, payload: dict[str, Any],
                   delivered_at: str | None = None) -> tuple[bool, int | None, str | None]:
        """Returns (accepted, current_version_if_stale, error_reason_if_400).

        - (True, None, None)   for accept (200)
        - (False, N, None)     for stale_version (409)
        - (False, None, why)   for invalid scope (400)
        """
        if scope not in _VALID_SCOPES:
            return False, None, "invalid_scope"
        if not isinstance(version, int) or version < 1:
            return False, None, "invalid_version"

        async with self._lock:
            key = (scope, context_id)
            cur = self._data.get(key)
            if cur is not None and cur["version"] > version:
                return False, cur["version"], None
            if cur is not None and cur["version"] == version:
                # Idempotent no-op: exact version already accepted. Do not
                # replace payload with a divergent retry body.
                return True, None, None
            # Higher version → accept and atomically replace.
            self._data[key] = {
                "version": version,
                "payload": payload,
                "delivered_at": delivered_at or _utc_now_iso(),
            }
        return True, None, None

    def get(self, scope: str, context_id: str) -> dict[str, Any] | None:
        rec = self._data.get((scope, context_id))
        if rec is None:
            return None
        return rec["payload"]

    def get_with_version(self, scope: str, context_id: str) -> dict[str, Any] | None:
        return self._data.get((scope, context_id))

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in _VALID_SCOPES}
        for (scope, _), _ in self._data.items():
            out[scope] = out.get(scope, 0) + 1
        return out

    def all_of(self, scope: str) -> Iterable[tuple[str, dict[str, Any]]]:
        for (s, cid), rec in self._data.items():
            if s == scope:
                yield cid, rec["payload"]

    def to_dict(self) -> dict[str, Any]:
        return {
            f"{scope}|{cid}": rec
            for (scope, cid), rec in self._data.items()
        }

    def load_dict(self, blob: dict[str, Any]) -> None:
        self._data.clear()
        for k, rec in blob.items():
            scope, cid = k.split("|", 1)
            self._data[(scope, cid)] = rec

    def clear(self) -> None:
        self._data.clear()


# ---- ConversationStore -----------------------------------------------------


class ConversationStore:
    """conversation_id -> ConversationState. Async-locked writes."""

    def __init__(self) -> None:
        self._data: dict[str, ConversationState] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, state: ConversationState) -> None:
        async with self._lock:
            self._data[state.conversation_id] = ConversationState.from_dict(state.to_dict())

    def get(self, conversation_id: str) -> ConversationState | None:
        state = self._data.get(conversation_id)
        return ConversationState.from_dict(state.to_dict()) if state is not None else None

    def open_conversations_for_merchant(self, merchant_id: str) -> list[ConversationState]:
        """Return conversations for this merchant in non-terminal phases."""
        open_phases = {ConvPhase.INITIATED, ConvPhase.AWAITING_REPLY, ConvPhase.ENGAGED, ConvPhase.AUTO_REPLY_SUSPECTED}
        return [
            ConversationState.from_dict(s.to_dict())
            for s in self._data.values()
            if s.merchant_id == merchant_id and s.phase in open_phases
        ]

    def all(self) -> list[ConversationState]:
        return [ConversationState.from_dict(s.to_dict()) for s in self._data.values()]

    def to_dict(self) -> dict[str, Any]:
        return {cid: s.to_dict() for cid, s in self._data.items()}

    def load_dict(self, blob: dict[str, Any]) -> None:
        self._data.clear()
        for cid, d in blob.items():
            self._data[cid] = ConversationState.from_dict(d)

    def clear(self) -> None:
        self._data.clear()


# ---- SuppressionStore ------------------------------------------------------


class SuppressionStore:
    """sent_keys + last_send_ts[merchant_id] + daily_send_count[(mid, ymd)]."""

    def __init__(self) -> None:
        self.sent_keys: set[str] = set()
        self.reserved_keys: set[str] = set()
        self.reserved_merchants: set[str] = set()
        self.last_send_ts: dict[str, float] = {}
        self.daily_send_count: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    async def reserve_for_compose(self, suppression_key: str, merchant_id: str) -> bool:
        """Reserve a suppression key + merchant while compose is in flight.

        This closes the race where two overlapping /v1/tick requests both pass
        the gate filter before either one records the emitted action.
        """
        async with self._lock:
            if suppression_key and (suppression_key in self.sent_keys or suppression_key in self.reserved_keys):
                return False
            if merchant_id in self.reserved_merchants:
                return False
            if suppression_key:
                self.reserved_keys.add(suppression_key)
            self.reserved_merchants.add(merchant_id)
            return True

    async def release_reservation(self, suppression_key: str, merchant_id: str) -> None:
        async with self._lock:
            if suppression_key:
                self.reserved_keys.discard(suppression_key)
            self.reserved_merchants.discard(merchant_id)

    async def commit_emit(self, reserved_key: str, emitted_key: str, merchant_id: str, now: float) -> None:
        async with self._lock:
            for key in {reserved_key, emitted_key}:
                if key:
                    self.sent_keys.add(key)
                    self.reserved_keys.discard(key)
            self.reserved_merchants.discard(merchant_id)
            self.last_send_ts[merchant_id] = now
            ymd = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
            self.daily_send_count[(merchant_id, ymd)] = (
                self.daily_send_count.get((merchant_id, ymd), 0) + 1
            )

    def is_suppressed(self, suppression_key: str) -> bool:
        return suppression_key in self.sent_keys

    def is_suppressed_or_reserved(self, suppression_key: str) -> bool:
        return suppression_key in self.sent_keys or suppression_key in self.reserved_keys

    def merchant_reserved(self, merchant_id: str) -> bool:
        return merchant_id in self.reserved_merchants

    def cooldown_until(self, merchant_id: str, hours: int = 6) -> float:
        last = self.last_send_ts.get(merchant_id, 0.0)
        return last + (hours * 3600) if last else 0.0

    def daily_count(self, merchant_id: str, when: float | None = None) -> int:
        when = when if when is not None else time.time()
        ymd = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d")
        return self.daily_send_count.get((merchant_id, ymd), 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent_keys": sorted(self.sent_keys),
            "last_send_ts": dict(self.last_send_ts),
            "daily_send_count": [
                {"merchant_id": mid, "ymd": ymd, "count": c}
                for (mid, ymd), c in self.daily_send_count.items()
            ],
        }

    def load_dict(self, blob: dict[str, Any]) -> None:
        self.sent_keys = set(blob.get("sent_keys", []))
        self.last_send_ts = dict(blob.get("last_send_ts", {}))
        self.daily_send_count = {
            (e["merchant_id"], e["ymd"]): int(e["count"])
            for e in blob.get("daily_send_count", [])
        }

    def clear(self) -> None:
        self.sent_keys.clear()
        self.reserved_keys.clear()
        self.reserved_merchants.clear()
        self.last_send_ts.clear()
        self.daily_send_count.clear()


# ---- snapshot helpers (BOT_DEV_MODE only) ----------------------------------


def dump_state(contexts: ContextStore, conversations: ConversationStore,
               suppression: SuppressionStore, path: Path = STATE_DUMP_FILE) -> None:
    """Write all three stores to disk as JSON."""
    blob = {
        "contexts": contexts.to_dict(),
        "conversations": conversations.to_dict(),
        "suppression": suppression.to_dict(),
        "dumped_at": _utc_now_iso(),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False, default=str)


def load_state(contexts: ContextStore, conversations: ConversationStore,
               suppression: SuppressionStore, path: Path = STATE_DUMP_FILE) -> bool:
    """Load stores from disk. Returns True if a dump was found AND loaded."""
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as f:
            blob = json.load(f)
        contexts.load_dict(blob.get("contexts", {}))
        conversations.load_dict(blob.get("conversations", {}))
        suppression.load_dict(blob.get("suppression", {}))
        return True
    except Exception:
        return False


def is_dev_mode() -> bool:
    """Whether the snapshot should be loaded on startup. Judge run = False."""
    return os.getenv("BOT_DEV_MODE", "0") == "1"
