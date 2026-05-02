"""Regression test for overlapping /v1/tick reservations.

This monkey-patches bot.acompose with a slow deterministic composer, then runs
two ticks for the same trigger concurrently. Exactly one action should emit.

Run:
    python scripts/test_tick_reservations.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import server  # noqa: E402


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


async def main() -> int:
    print("tick reservation race test")
    print("--------------------------")

    server.CONTEXTS.clear()
    server.CONVERSATIONS.clear()
    server.SUPPRESSION.clear()

    category = _load_json(ROOT / "dataset" / "categories" / "dentists.json")
    merchant = _load_json(ROOT / "dataset" / "merchants" / "m_001_drmeera_dentist_delhi.json")
    trigger = _load_json(ROOT / "dataset" / "triggers" / "trg_001_research_digest_dentists.json")

    await server.CONTEXTS.push("category", category["slug"], 1, category)
    await server.CONTEXTS.push("merchant", merchant["merchant_id"], 1, merchant)
    await server.CONTEXTS.push("trigger", trigger["id"], 1, trigger)

    original_acompose = bot.acompose

    async def fake_acompose(*args, **kwargs):
        await asyncio.sleep(0.05)
        return bot.ComposedMessage(
            body="Dr. Meera, JIDA Oct 2026, p.14 is relevant to your high-risk adults. Want the abstract?",
            cta="open_ended",
            send_as="vera",
            suppression_key=trigger["suppression_key"],
            rationale="Race-test compose. [anchor=JIDA Oct 2026, p.14, lever=specificity, trigger=research_digest:u2, send_as=vera, prompt_v=test]",
            anchor="JIDA Oct 2026, p.14",
            lever="specificity",
        )

    bot.acompose = fake_acompose
    try:
        req = server.TickRequest(
            now="2026-05-02T00:00:00Z",
            available_triggers=[trigger["id"]],
        )
        first, second = await asyncio.gather(server.tick(req), server.tick(req))
    finally:
        bot.acompose = original_acompose
        server.CONTEXTS.clear()
        server.CONVERSATIONS.clear()
        server.SUPPRESSION.clear()

    action_count = len(first["actions"]) + len(second["actions"])
    if action_count != 1:
        print(f"  [FAIL] expected exactly one action across concurrent ticks, got {action_count}")
        print(f"  first={first}")
        print(f"  second={second}")
        return 1

    print("  [OK] exactly one action emitted across concurrent ticks")
    print("ALL TICK RESERVATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
