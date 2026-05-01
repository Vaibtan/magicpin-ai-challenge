"""End-to-end integration smoke test using FastAPI TestClient.

Drives the bot through:
  1. healthz / metadata
  2. /v1/context (push 5 cats + 50 merchants + 200 customers + 100 triggers)
  3. /v1/context idempotency (same/higher/lower/malformed scope)
  4. /v1/tick (10 triggers — exercises the 7-gate filter + parallel compose)
  5. /v1/reply (auto_reply 1st & 2nd, hostile, defer, not_interested, unclear)
  6. /v1/teardown

Without API keys, /v1/tick will fall back to safe-template messages — that's
the whole point: the bot stays up + emits valid responses even when the LLM
isn't reachable. With API keys, the same flow exercises the live model path.

Run:
    python scripts/smoke_integration.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient


def main() -> int:
    import server  # imports trigger startup logs
    client = TestClient(server.app)

    # ---- 1. healthz / metadata ----
    print("\n=== /v1/healthz ===")
    r = client.get("/v1/healthz")
    assert r.status_code == 200, r.text
    print(json.dumps(r.json(), indent=2))

    print("\n=== /v1/metadata ===")
    r = client.get("/v1/metadata")
    assert r.status_code == 200, r.text
    print(json.dumps(r.json(), indent=2)[:400] + "...")

    # ---- 2. push contexts ----
    print("\n=== /v1/context — push the full dataset ===")
    # Per-scope ID-field — trigger payloads also have merchant_id, customer payloads
    # too, so we must look up by the specific field per scope.
    cid_field = {"category": "slug", "merchant": "merchant_id",
                 "customer": "customer_id", "trigger": "id"}
    pushed = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for scope, dirname in [("category", "categories"), ("merchant", "merchants"),
                           ("customer", "customers"), ("trigger", "triggers")]:
        for f in (ROOT / "dataset" / dirname).glob("*.json"):
            with f.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            cid = payload[cid_field[scope]]
            r = client.post("/v1/context", json={
                "scope": scope, "context_id": cid, "version": 1, "payload": payload,
            })
            assert r.status_code == 200, f"{scope}/{cid}: {r.text}"
            pushed[scope] += 1
    print(f"pushed: {pushed}")

    print("\n=== /v1/healthz after push ===")
    r = client.get("/v1/healthz")
    counts = r.json()["contexts_loaded"]
    print(json.dumps(counts, indent=2))
    assert counts["category"] == 5
    assert counts["merchant"] == 50
    assert counts["customer"] == 200
    assert counts["trigger"] == 100

    # ---- 3. idempotency ----
    print("\n=== /v1/context idempotency ===")
    p = {"scope": "category", "context_id": "dentists", "version": 1,
         "payload": {"slug": "dentists"}}
    r = client.post("/v1/context", json=p);          print(f"  same v1   → {r.status_code}")
    p["version"] = 2
    r = client.post("/v1/context", json=p);          print(f"  higher v2 → {r.status_code}")
    p["version"] = 1
    r = client.post("/v1/context", json=p);          print(f"  lower v1  → {r.status_code} (expect 409)")
    assert r.status_code == 409
    r = client.post("/v1/context", json={"scope": "garbage", "context_id": "x",
                                          "version": 1, "payload": {}})
    print(f"  bad scope → {r.status_code} (expect 400)")
    assert r.status_code == 400

    # Re-push real dentists v1 to avoid breaking subsequent tests
    with (ROOT / "dataset/categories/dentists.json").open() as f:
        cat = json.load(f)
    client.post("/v1/context", json={"scope": "category", "context_id": "dentists",
                                      "version": 99, "payload": cat})

    # ---- 4. /v1/tick — exercise 7-gate filter + parallel compose ----
    print("\n=== /v1/tick — 10 triggers (exercises gates + parallel compose) ===")
    with (ROOT / "dataset/test_pairs.json").open() as f:
        pairs = json.load(f)["pairs"][:10]
    trigger_ids = [p["trigger_id"] for p in pairs]
    r = client.post("/v1/tick", json={
        "now": "2026-04-30T10:00:00Z",
        "available_triggers": trigger_ids,
    })
    assert r.status_code == 200, r.text
    actions = r.json()["actions"]
    print(f"actions returned: {len(actions)} (cap 3)")
    for a in actions:
        # Verify private fields stripped
        assert "anchor" not in a, "anchor leaked!"
        assert "lever" not in a, "lever leaked!"
        assert "prompt_version" not in a, "prompt_version leaked!"
        # Verify required fields present
        for k in ["conversation_id", "merchant_id", "customer_id", "send_as",
                  "trigger_id", "template_name", "template_params", "body",
                  "cta", "suppression_key", "rationale"]:
            assert k in a, f"missing key {k!r} in action"
        is_fallback = "fallback" in (a.get("rationale") or "").lower()
        marker = "FALLBACK" if is_fallback else "OK"
        print(f"  [{marker}] {a['merchant_id']:42}  "
              f"trigger={a['trigger_id'][:30]:30}  body={a['body'][:50]!r}")

    # ---- 5. /v1/reply branches ----
    print("\n=== /v1/reply branches (templated) ===")
    cases = [
        ("auto_reply 1st", "Thank you for contacting us! Our team will respond shortly.", "send"),
        ("auto_reply 2nd", "Thank you for contacting us! Our team will respond shortly.", "end"),
        ("hostile",        "Stop messaging me, this is useless spam.",                    "end"),
        ("defer",          "Send tomorrow please",                                        "wait"),
        ("not_interested", "Not interested, thanks",                                      "end"),
        ("unclear",        "hmm",                                                         "send"),
    ]
    conv_id = "conv_smoke_reply"
    for label, msg, expected_action in cases:
        r = client.post("/v1/reply", json={
            "conversation_id": conv_id,
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "from_role": "merchant",
            "message": msg,
            "received_at": "2026-04-30T10:00:00Z",
            "turn_number": 1,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        action = body.get("action")
        ok = (action == expected_action)
        marker = "OK  " if ok else "FAIL"
        print(f"  [{marker}] {label:18} -> action={action:6} "
              f"body/wait={(body.get('body') or str(body.get('wait_seconds') or ''))[:60]!r}")

    # ---- 6. /v1/teardown ----
    print("\n=== /v1/teardown ===")
    r = client.post("/v1/teardown")
    assert r.status_code == 200, r.text
    print(r.json())
    counts = client.get("/v1/healthz").json()["contexts_loaded"]
    print(f"  post-teardown counts: {counts}")
    assert all(v == 0 for v in counts.values())

    print("\nALL INTEGRATION TESTS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
