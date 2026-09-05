"""Persistent, tamper-evident decision journal for the trading runtime."""
from __future__ import annotations
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_LAST_HASH = "0" * 64
_INITIALIZED = False


def _path() -> Path:
    return Path(os.getenv("DECISION_JOURNAL_PATH", "logs/decision_journal.jsonl"))


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _initialize_chain(path: Path) -> None:
    global _LAST_HASH, _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True
    try:
        if path.exists():
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                if line.strip():
                    _LAST_HASH = str(json.loads(line).get("hash") or ("0" * 64))
                    break
    except Exception:
        _LAST_HASH = "0" * 64


def append_event(*, symbol: str = "", side: str = "", stage: str = "",
                 decision: str = "", reason: str = "", detail: str = "",
                 score: float | None = None, metadata: dict[str, Any] | None = None) -> dict:
    global _LAST_HASH
    path = _path()
    with _LOCK:
        _initialize_chain(path)
    record = {
        "ts": round(time.time(), 6), "symbol": str(symbol or ""),
        "side": str(side or ""), "stage": str(stage or ""),
        "decision": str(decision or ""), "reason": str(reason or "")[:240],
        "detail": str(detail or "")[:500],
        "score": None if score is None else round(float(score), 6),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "prev_hash": _LAST_HASH,
    }
    digest = hashlib.sha256(_canonical(record)).hexdigest()
    record["hash"] = digest
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            _LAST_HASH = digest
        except Exception:
            pass
    return record


def verify_file(path: str | os.PathLike[str] | None = None) -> tuple[bool, int, str]:
    p = Path(path) if path else _path()
    if not p.exists():
        return True, 0, "journal_missing_or_empty"
    prev = "0" * 64
    count = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            stored = rec.pop("hash")
            if rec.get("prev_hash") != prev:
                return False, count, "prev_hash_mismatch"
            expected = hashlib.sha256(_canonical(rec)).hexdigest()
            if stored != expected:
                return False, count, "hash_mismatch"
            prev, count = stored, count + 1
        return True, count, "ok"
    except Exception as exc:
        return False, count, f"verification_error:{type(exc).__name__}"
