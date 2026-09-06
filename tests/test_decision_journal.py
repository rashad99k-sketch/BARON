import json
from pathlib import Path

import core.decision_journal as _dj


def test_decision_journal_hash_chain(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    monkeypatch.setenv("DECISION_JOURNAL_PATH", str(path))
    from core.decision_journal import append_event, verify_file
    orig_last, orig_init = _dj._LAST_HASH, _dj._INITIALIZED
    try:
        # The journal chain state is process-global; other tests that appended
        # earlier in the run would otherwise taint -> reset to a fresh chain.
        _dj._LAST_HASH = "0" * 64
        _dj._INITIALIZED = False
        append_event(symbol="BTC/USDT:USDT", stage="RISK", decision="VETO",
                     reason="GLOBAL_COOLDOWN")
        append_event(symbol="ETH/USDT:USDT", stage="QUEUE", decision="OBSERVE",
                     reason="WAITING_TRIGGER")
        assert verify_file() == (True, 2, "ok")
        rows = [json.loads(x) for x in path.read_text().splitlines()]
        assert rows[1]["prev_hash"] == rows[0]["hash"]
    finally:
        _dj._LAST_HASH = orig_last
        _dj._INITIALIZED = orig_init

def test_decision_journal_detects_tamper(tmp_path, monkeypatch):
    path=tmp_path/"journal.jsonl"; monkeypatch.setenv("DECISION_JOURNAL_PATH",str(path))
    from core.decision_journal import append_event, verify_file
    append_event(symbol="BTC/USDT:USDT",stage="EXECUTION",decision="VETO",reason="TEST")
    path.write_text(path.read_text().replace('"TEST"','"TAMPERED"'))
    ok,count,msg=verify_file(); assert not ok and count==0 and msg in {"hash_mismatch","prev_hash_mismatch"}
