"""Structured event logging — one JSON line per significant event.

Every module that needs to log calls `log_event(name, **fields)`. Output
goes to `logs/run_{RUN_ID}.jsonl` so a full run can be reconstructed
post-hoc and grepped/filtered with jq.

Event types used across the codebase (design-decisions.md §10):
    compose, cache_hit, cache_miss, validator_fail, fallback_used,
    tick_skip, tick_timeout, composer_self_veto, reply_classify,
    auto_reply_exit, phase_transition, anthropic_error_falling_back, ...
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

RUN_ID = os.getenv("RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
LOG_FILE = LOGS_DIR / f"run_{RUN_ID}.jsonl"

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"


def log_event(event: str, **fields: Any) -> None:
    """Append one JSON line to the current run's log file."""
    record = {"ts": _now_iso(), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _lock:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
