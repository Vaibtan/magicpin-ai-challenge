"""Deterministic tests for state-store semantics and tick reservations.

Run:
    python scripts/test_state_policy.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import (  # noqa: E402
    ContextStore,
    ConversationState,
    ConversationStore,
    ConvPhase,
    SuppressionStore,
)


def expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"  [FAIL] {message}")
        sys.exit(1)
    print(f"  [OK]   {message}")


async def main() -> int:
    print("state policy tests")
    print("------------------")

    contexts = ContextStore()
    accepted, _, reason = await contexts.push("category", "dentists", 1, {"slug": "dentists", "x": 1})
    expect(accepted and reason is None, "initial context push accepted")

    accepted, _, reason = await contexts.push("category", "dentists", 1, {"slug": "dentists", "x": 999})
    expect(accepted and reason is None, "same-version context push is accepted")
    expect(contexts.get("category", "dentists")["x"] == 1, "same-version context push is a true no-op")

    accepted, current, reason = await contexts.push("category", "dentists", 0, {"slug": "dentists"})
    expect((not accepted) and current is None and reason == "invalid_version", "invalid context version rejected")

    conversations = ConversationStore()
    state = ConversationState(
        conversation_id="conv_1",
        merchant_id="m_1",
        trigger_id="trg_1",
        send_as="vera",
        phase=ConvPhase.INITIATED,
        turns=[{"from": "bot", "body": "hello"}],
    )
    await conversations.upsert(state)
    fetched = conversations.get("conv_1")
    assert fetched is not None
    fetched.turns.append({"from": "merchant", "body": "mutated outside store"})
    expect(len(conversations.get("conv_1").turns) == 1, "conversation get() returns an isolated copy")

    open_convs = conversations.open_conversations_for_merchant("m_1")
    open_convs[0].phase = ConvPhase.EXITED
    expect(
        conversations.open_conversations_for_merchant("m_1")[0].phase == ConvPhase.INITIATED,
        "open conversation query returns isolated copies",
    )

    suppression = SuppressionStore()
    ok = await suppression.reserve_for_compose("sup:1", "m_1")
    expect(ok, "first reservation succeeds")
    ok = await suppression.reserve_for_compose("sup:1", "m_1")
    expect(not ok, "duplicate reservation is blocked while in flight")
    expect(suppression.is_suppressed_or_reserved("sup:1"), "reserved key is visible to the gate")
    expect(suppression.merchant_reserved("m_1"), "reserved merchant is visible to the gate")

    await suppression.release_reservation("sup:1", "m_1")
    ok = await suppression.reserve_for_compose("sup:1", "m_1")
    expect(ok, "released reservation can be retried")

    await suppression.commit_emit("sup:1", "sup:1", "m_1", 1_777_654_400.0)
    expect(suppression.is_suppressed("sup:1"), "committed emit records suppression key")
    expect(not suppression.merchant_reserved("m_1"), "committed emit clears in-flight merchant reservation")
    expect(suppression.daily_count("m_1", when=1_777_654_400.0) == 1, "committed emit increments daily cap")

    print()
    print("ALL STATE POLICY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
