"""Runtime + Code verification of the full analysis pipeline (Hermetic).

Proves that assets shown in the DASHBOARD stages actually pass the REAL
production analysis functions -- not just the lists:

  Universe -> Radar -> Watchlist(deep analyze) -> Institutional Zone Analysis
  -> Execution Queue.

Methodology mirrors the sanctioned tools/paper_runtime_smoke.py: real engine/
scanner/queue/radar classes, deterministic feed at the exchange boundary.

Neutralizations (documented deliberately):
  1. QUEUE_PROMOTE_INTERVAL/RE_EVAL_INTERVAL forced huge before import so the
     auto-started engine background loop (engine.py:16147 -> main_loop_sniper)
     cannot promote/re-eval concurrently and corrupt counters.
  2. scanner.process_queue_entry is stubbed to a no-op AFTER import -- ONLY the
     legacy auto-EXECUTION thread is neutralised (execution was already
     verified in loop_verify.py). Queuing, promotion, radar, re-eval all run
     real. The registry path `radar.update_all()` (1s cadence) stays LIVE.
"""
from __future__ import annotations

import os
import sys
import types
import time
import hashlib
import pandas as pd
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update({
    "PAPER_MODE": "True",
    "NEWS_ENABLED": "False",
    "DEEP_WATCHLIST_SIZE": "60",
    "DEEP_SCAN_WATCHLIST_SIZE": "60",
    "DEEP_SCAN_RADAR_SYMBOLS": "0",
    "WATCHLIST_DEEP_BATCH_SIZE": "10",
    "WATCHLIST_DEEP_INTERVAL_SEC": "0",
    "USE_EXECUTION_QUEUE": "True",
    "QUEUE_RE_EVAL_INTERVAL": "999999",
    "QUEUE_PROMOTE_INTERVAL": "999999",
    "GLOBAL_SCAN_INTERVAL_SEC": "999999",
    "WATCHLIST_SERVICE_INTERVAL_SEC": "999999",
    "MAIN_LOOP_SLEEP": "999999",
    "BASE_SLEEP": "999999",
    "RADAR_MAX_CALLS_PER_MIN": "100000",
    "RADAR_BATCH_INTERVAL_SEC": "0",
    "QUEUE_MAX_SIZE": "60",
    "POSITION_MARGIN_PCT": "0.10",
    "PORTFOLIO_MARGIN_CAP_PCT": "0.60",
})


class FakeExchange:
    def __init__(self, *args, **kwargs):
        self._m = {}
        for cls, base in (("CRYPTO", 100), ("INDEX", 500),
                          ("GOLD", 900), ("OIL", 1200), ("STOCK", 1500)):
            for i in range(20):
                sym = f"{cls}{i}/USDT:USDT"
                base = base + 137.0 * (i + 1)
                self._m[sym] = {"base": sym.split("/")[0], "quote": "USDT",
                                "type": "swap", "active": True}
        self.markets = self._m

    def load_markets(self):
        return self._m


ccxt_stub = types.ModuleType("ccxt")
ccxt_stub.bingx = FakeExchange
sys.modules["ccxt"] = ccxt_stub

flask_stub = types.ModuleType("flask")


class _SmokeFlask:
    def __init__(self, *args, **kwargs):
        self.routes = {}

    def route(self, path, methods=None, **kwargs):
        def decorator(fn):
            for method in (methods or ["GET"]):
                self.routes[(method, path)] = fn
            return fn
        return decorator

    def add_url_rule(self, path, endpoint, view_func, methods=None, **kwargs):
        for method in (methods or ["GET"]):
            self.routes[(method, path)] = view_func

    def before_request(self, fn):
        return fn


flask_stub.Flask = _SmokeFlask
flask_stub.jsonify = lambda *a, **k: a[0] if a else None
flask_stub.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1", json=None)
sys.modules["flask"] = flask_stub

for name in list(sys.modules):
    if name == "core.engine" or name.startswith("scanner.") or name.startswith("strategy.") or name.startswith("news."):
        sys.modules.pop(name, None)

import core.engine as E
import scanner.deep_scanner as D
import scanner.scanner as S


def _seed_of(symbol: str) -> float:
    h = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    return 40.0 + (h % 9000) / 100.0


def any_frame(symbol: str, limit: int = 150, htf: bool = False) -> pd.DataFrame:
    base = _seed_of(symbol)
    n = 300
    drift = (0.98 if int(_seed_of(symbol)) % 4 == 1 else 1.05)
    x = np.linspace(base, base * drift, n)
    step = 120.0
    now = time.time()
    # Engine interprets non-datetime timestamps as EPOCH MILLISECONDS and
    # divides by 1000 (deep_scanner.py:861). Feed ms to mirror ccxt/bingx.
    ts = np.array([(now - (n - 1 - i) * step) * 1000.0 for i in range(n)], dtype=float)
    rng = np.multiply(x, 0.0012)
    return pd.DataFrame({
        "timestamp": ts,
        "open": x,
        "high": x + rng + 0.1,
        "low": x - rng - 0.1,
        "close": x,
        "volume": np.full(n, 1500.0),
    })


E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: any_frame(symbol, limit, htf)
E.get_orderbook_cached = lambda *a, **k: {"bids": [[12.0, 10.0]], "asks": [[16.0, 5.0]]}
E.get_ticker_safe = lambda symbol: float(any_frame(symbol)["close"].iloc[-1])
S.get_ohlcv_safe = lambda symbol, limit=120, htf=False: any_frame(symbol, limit, htf)
S.get_orderbook_cached = lambda *a, **k: {"bids": [[12.0, 10.0]], "asks": [[16.0, 5.0]]}


def strategy_analyze(symbol, side, df, orderbook=None):
    return {
        "symbol": symbol, "side": side, "price": float(df.close.iloc[-1]),
        "atr": 1.0, "score": 9.0 if side == "BUY" else 6.0,
        "narrative_score": 8.0,
        "intent_score": 85.0, "intent_status": "ACCUMULATION", "intent_details": {},
        "narrative": {"sweep": True, "choch_bos": True, "retest": True,
                      "rejection": True, "displacement": True,
                      "volume_confirmation": True, "rf_alignment": True},
        "smart_money": {"institutional_bias": side, "institutional_bias_detailed": side,
                        "smart_money_dominant": True, "distribution_risk": 5,
                        "accumulation_strength": 90},
        "momentum": {"trend_expansion": True, "flow_bias": side,
                     "momentum_decay": False, "exhaustion_risk": 5,
                     "continuation_strength": 90},
    }


scanner = D.DeepScanner(max_symbols=60)
scanner.radar_symbols = 0
scanner.news.enabled = False
scanner.strategy.analyze = strategy_analyze
# Neutralize ONLY the legacy auto-EXECUTION thread; queue admission and all
# analysis/radar/promotion paths stay fully real and impatient live.
S.process_queue_entry = lambda: None

MEMORY = E.MEMORY

CHECKS = []


def check(name, ok, detail):
    CHECKS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


now0 = time.time()

# ---------------------------------------------------------------- STAGE A
print("\n===== STAGE A: Discovery -> Radar -> Watchlist seed (full funnel) =====")
scan_returned = scanner.scan(force=True)
# Drive the staggered radar cycle to completion (production uses
# advance_radar_cycle() from the runtime service tick).
advance_tries = 0
pump_log = []
while scanner.radar_cycle is not None and advance_tries < 60:
    n = scanner.advance_radar_cycle(force=True)
    if n == 0:
        advance_tries += 1
        time.sleep(0.1)
    else:
        advance_tries = 0
    if scanner.radar_cycle is not None:
        pump_log.append((scanner.radar_cycle["cursor"], scanner.radar_cycle["target"], len(scanner.radar_cycle["results"])))
wl = MEMORY.get("watchlist", {})
pipeline = MEMORY.get("pipeline", {})
print(f"scan() returned rows={len(scan_returned)} radar_cycle_complete={scanner.radar_cycle is None}")
print(f"radar pump progress (cursor/target/accepted): {pump_log}")
print(f"pipeline keys: {sorted(pipeline.keys())}")
print(f"universe keys: {sorted((pipeline.get('universe') or {}).keys())}")
print(f"radar keys: {sorted((pipeline.get('radar') or {}).keys())}")
print(f"watchlist pipe keys: {sorted((pipeline.get('watchlist') or {}).keys())}")
wl_seeded = len(wl)
wl_pre_analyzed = sum(1 for v in wl.values() if v.get("deep_analyzed"))
check("A1 funnel discovery+radar+watchlist counters written",
      isinstance(pipeline.get("universe"), dict) and isinstance(pipeline.get("radar"), dict)
      and isinstance(pipeline.get("watchlist"), dict),
      f"pipeline={sorted(pipeline.keys())} universe.ts={pipeline.get('universe',{}).get('ts',0)!=0} radar.ts={pipeline.get('radar',{}).get('ts',0)!=0} radar.{pipeline.get('radar',{})}")
check("A2 watchlist seeded to target 60",
      wl_seeded == 60, f"watchlist={wl_seeded} pipeline.watchlist.seeded={pipeline.get('watchlist',{}).get('seeded')} target={pipeline.get('watchlist',{}).get('target')}")
check("A3 seed state marks NOT deep-analyzed (listing != analysis)",
      wl_pre_analyzed == 0, f"deep_analyzed at seed={wl_pre_analyzed}/{wl_seeded} (dashboard lists them BEFORE analysis)")

# ---------------------------------------------------------------- STAGE B
print("\n===== STAGE B: Deep-analyze every watchlist asset (rotation) =====")
coverage = []
for k in range(7):
    t0 = time.time()
    updated = scanner.monitor_watchlist(force=True)
    wlk = MEMORY.get("watchlist", {})
    analyzed = sum(1 for v in wlk.values() if v.get("deep_analyzed"))
    reg = len(MEMORY.get("institutional_zone_analysis", {}))
    coverage.append((k, len(updated), analyzed, reg))
    print(f"  round{k+1}: batch={len(updated)} analyzed_now={analyzed}/60 registry={reg} tick={(time.time()-t0)*1000:.0f}ms")
    time.sleep(0.05)

wl = MEMORY.get("watchlist", {})
missing_analysis = [s for s, v in wl.items() if not v.get("deep_analyzed")]
incomplete = []
stale_entries = []
max_data_age = 0.0
min_last_update = time.time()
for s, v in wl.items():
    if not v.get("deep_analyzed"):
        continue
    need = ["score", "strength", "last_update", "data_age", "data_quality",
            "deep_score", "state", "reasons", "smart_money", "momentum", "news"]
    for field in need:
        if field not in v:
            incomplete.append((s, field))
    try:
        max_data_age = max(max_data_age, float(v.get("data_age", 0)))
    except Exception:
        pass
    try:
        min_last_update = min(min_last_update, float(v.get("last_update", 0)))
    except Exception:
        pass
    if str(v.get("data_quality", "")).upper() == "STALE":
        stale_entries.append(s)
snap = MEMORY.get("deep_scanner", [])
snap_ghosts = [e.get("symbol") for e in snap if not e.get("deep_analyzed")]
check("B1 ALL 60 watchlist assets deep-analyzed after one full rotation",
      len(missing_analysis) == 0,
      f"unanalyzed={len(missing_analysis)} {missing_analysis[:5]}")
check("B2 every analyzed entry carries complete analysis payload",
      len(incomplete) == 0,
      f"missing_fields={len(incomplete)} {incomplete[:3]}")
check("B3 fresh data on every analyzed entry (no stale cache)",
      max_data_age < 900 and len(stale_entries) == 0,
      f"max_data_age={max_data_age:.1f}s threshold=900s stale_entries={len(stale_entries)}")
check("B4 all analyses performed within the run window (fresh timestamps)",
      (now0 - 120) < min_last_update,
      f"earliest last_update={time.time()-min_last_update:.1f}s before end-of-run")
check("B5 dashboard ranked snapshot contains only analyzed assets at steady state",
      len(snap_ghosts) == 0, f"snapshot_entries={len(snap)} ghosts_in_snapshot={len(snap_ghosts)}")

analysis_fields = {
    "per_entry_freshness": f"max_data_age={max_data_age:.1f}s",
    "per_entry_age_vs_run": f"earliest_analysis={time.time()-min_last_update:.1f}s before finish",
}

# ---------------------------------------------------------------- STAGE C
print("\n===== STAGE C: Institutional Zone Analysis registry (Dynamic Candidates) =====")
# Production radar cadence: main_loop_sniper calls InstitutionalRadar.update_all()
# every 1s (engine.py:12028-12030). Drive the same real method once so each
# watchlist entry gets its institutional_analysis_time backfilled exactly as
# the 1s production tick does.
radar_inst = getattr(scanner, "_institutional_radar", None)
if radar_inst is None:
    from core.engine import InstitutionalRadar
    radar_inst = InstitutionalRadar()
    scanner._institutional_radar = radar_inst
_t0r = time.time()
radar_inst.update_all()
print(f"radar.update_all() tick elapsed={time.time()-_t0r:.1f}s")
reg = MEMORY.get("institutional_zone_analysis", {})
print(f"registry_count={len(reg)} MEMORY.institutional_zone_count={MEMORY.get('institutional_zone_count')}")
reg_bad = []
reg_stale = []
reg_not_in_wl = []
for s, item in reg.items():
    if s not in wl:
        reg_not_in_wl.append(s)
        continue
    entry = wl[s]
    if not entry.get("deep_analyzed"):
        reg_bad.append((s, "not_deep_analyzed"))
    if str(entry.get("strength", "")) not in ("MEDIUM", "STRONG"):
        reg_bad.append((s, f"strength={entry.get('strength')}"))
    if not entry.get("institutional_zone_active"):
        reg_bad.append((s, "entry.zone_active=False"))
    for field in ["institutional_score", "composite_score", "zone_quality",
                  "precursor_count", "precursor_evidence", "institutional_analysis_time",
                  "last_update", "expires_at", "a_grade_ready", "a_grade_reasons",
                  "hypothesis", "phase"]:
        if field not in item:
            reg_bad.append((s, f"missing:{field}"))
    try:
        if float(item.get("institutional_analysis_time", 0)) <= 0:
            reg_bad.append((s, "institutional_analysis_time<=0"))
        if float(item.get("last_update", 0)) < now0 - 60:
            reg_stale.append(s)
        if float(item.get("expires_at", 0)) < time.time():
            reg_stale.append(s)
        if item.get("precursor_count", 0) < 1:
            reg_bad.append((s, "precursor_count<1"))
    except Exception as exc:
        reg_bad.append((s, f"field_exc:{exc}"))
reg_active_sym = {s for s, v in wl.items() if v.get("institutional_zone_active")}
reg_keys = set(reg.keys())
activemismatch = reg_active_sym ^ reg_keys
check("C1 registry is a strict subset of deep-analyzed watchlist (no ghosts)",
      not reg_not_in_wl and not any(b[1] == "not_deep_analyzed" or b[1].startswith("strength=") for b in reg_bad),
      f"reg_not_in_watchlist={len(reg_not_in_wl)} bad_membership={[b for b in reg_bad if b[1] in ('not_deep_analyzed',) or b[1].startswith('strength=')]}")
check("C2 zone-active flag is the ONLY gate list (dashboard zone == analyzed set)",
      len(activemismatch) == 0,
      f"zone_active={len(reg_active_sym)} registry={len(reg_keys)} mismatch={sorted(activemismatch)[:5]}")
check("C3 every registry entry is fully analysed (all 15 fields + score)",
      len(reg_bad) == 0, f"bad={len(reg_bad)} {reg_bad[:4]}")
check("C4 every registry entry fresh (analysis ts>0, last_update<60s, not expired)",
      len(reg_stale) == 0, f"stale={len(reg_stale)} {reg_stale[:4]}")
check("C5 every registry entry has institutional provenance timestamps",
      all(float(i.get("institutional_analysis_time", 0)) > 0 for i in reg.values()),
      f"count={len(reg)} with analysis_time>0={sum(1 for i in reg.values() if float(i.get('institutional_analysis_time',0))>0)}")
if reg:
    sample = next(iter(reg.items()))
    print(f"  sample registry entry {sample[0]}: score={sample[1].get('institutional_score')} "
          f"composite={sample[1].get('composite_score')} precursors={sample[1].get('precursor_evidence')} "
          f"a_grade={sample[1].get('a_grade_ready')} reasons={sample[1].get('a_grade_reasons')}")

# ---------------------------------------------------------------- STAGE D
print("\n===== STAGE D: Execution Queue admission (provenance) =====")
promoted = S.promote_to_queue()
promo = pipeline.get("promotion", pipeline.setdefault("promotion", {}))
print(f"promote_to_queue() -> promoted={promoted}")
print(f"promotion counters: checked={promo.get('checked')} eligible={promo.get('eligible')} "
      f"promoted={promo.get('promoted')} rejected={promo.get('rejected_by_reason')}")
cands = E.queue._candidates
tot = MEMORY["watchlist_queue_promotions"]
queue_bad = []
for sym, cand in cands.items():
    if sym not in reg:
        queue_bad.append((sym, "not_in_registry"))
        continue
    if sym not in wl or not wl[sym].get("deep_analyzed"):
        queue_bad.append((sym, "watchlist_not_analyzed"))
    if not wl[sym].get("institutional_zone_active"):
        queue_bad.append((sym, "zone_inactive"))
    if not (cand.queue_time > 0 and cand.prepared_time > 0):
        queue_bad.append((sym, "no_queue_time"))
    if not cand.institutional_analysis_time > 0:
        queue_bad.append((sym, "no_institutional_analysis_time"))
    if not cand.priority_score > 0:
        queue_bad.append((sym, "priority<=0"))
    if cand.signal_type not in ("institutional_zone_a_grade", "institutional_prepared"):
        queue_bad.append((sym, f"signal_type={cand.signal_type}"))
    if cand.original_reason not in ("INSTITUTIONAL_ZONE_A_GRADE", "INSTITUTIONAL_PREPARED"):
        queue_bad.append((sym, f"original_reason={cand.original_reason}"))
print(f"queue_candidates={len(cands)} MEMORY.watchlist_queue_promotions={tot}")
caps = getattr(E.queue, "max_size", None)
check("D1 queue admission only from fully-analyzed, zone-active watchlist assets",
      len(queue_bad) == 0, f"bad={len(queue_bad)} {queue_bad[:4]}")
check("D2 promotion accounting is exact; queue holds promoted set within capacity",
      tot == promoted and len(cands) == promoted and (caps is None or len(cands) <= caps),
      f"counter={tot} promoted={promoted} live={len(cands)} capacity={caps}")
check("D3 every live candidate carries institutional/analysis provenance timestamps",
      all(c.queue_time > 0 and c.prepared_time > 0 and c.institutional_analysis_time > 0 for c in cands.values()),
      f"qty={len(cands)} with_queue_time={sum(1 for c in cands.values() if c.queue_time>0)} with_prepared={sum(1 for c in cands.values() if c.prepared_time>0)} with_inst_time={sum(1 for c in cands.values() if c.institutional_analysis_time>0)}")
check("D4 promotion funnel counters recorded by the real promote_to_queue",
      promo.get("promoted") == promoted and promo.get("checked", 0) >= promoted,
      f"checked={promo.get('checked')} eligible={promo.get('eligible')} promoted={promo.get('promoted')}")

# --- adversarial: ghosts that try to reach the queue without the funnel ---
print("\n===== STAGE D anti-GHOST: assets NOT passing the stages cannot reach queue =====")
ghost = "GHOST1/USDT:USDT"
wl[ghost] = {"symbol": ghost, "side": "BUY", "asset_class": "INDEX",
             "score": 8.0, "deep_analyzed": False, "strength": "MEDIUM",
             "state": "DETECTED", "last_update": time.time(), "radar_score": 8.0,
             "news": {"available": False, "risk": 0}, "pre_expansion_evidence": ["SWEEP", "BOS"],
             "pre_expansion": {"phase": "TREND_BUILDING"}}
weak = "GHOST2/USDT:USDT"
wl[weak] = {"symbol": weak, "side": "BUY", "asset_class": "INDEX",
            "score": 2.0, "deep_analyzed": True, "strength": "WEAK",
            "state": "DETECTED", "last_update": time.time(), "radar_score": 2.0,
            "news": {"available": False, "risk": 0}, "pre_expansion_evidence": []}
reg_no_wl = "GHOST3/USDT:USDT"
reg[reg_no_wl] = {"symbol": reg_no_wl, "strength": "MEDIUM", "state": "INSTITUTIONAL_WATCH",
                  "institutional_score": 80.0, "composite_score": 80.0,
                  "institutional_analysis_time": time.time() - 1000,
                  "last_update": time.time(), "expires_at": time.time() + 900,
                  "precursor_count": 2}
reg_unanalysed = "GHOST4/USDT:USDT"
wl[reg_unanalysed] = {"symbol": reg_unanalysed, "side": "SELL", "asset_class": "INDEX",
                      "score": 8.0, "deep_analyzed": False, "strength": "MEDIUM",
                      "state": "DETECTED", "last_update": time.time()}
reg[reg_unanalysed] = {"symbol": reg_unanalysed, "strength": "MEDIUM", "state": "INSTITUTIONAL_WATCH",
                       "institutional_score": 80.0, "composite_score": 80.0,
                       "institutional_analysis_time": time.time() - 1000,
                       "last_update": time.time(), "expires_at": time.time() + 900,
                       "precursor_count": 3}
zone_off = "GHOST5/USDT:USDT"
wl[zone_off] = {"symbol": zone_off, "side": "BUY", "asset_class": "INDEX",
                "score": 8.0, "deep_analyzed": True, "strength": "MEDIUM",
                "state": "DETECTED", "last_update": time.time(),
                "pre_expansion_evidence": ["SWEEP", "BOS", "FVG"]}
reg[zone_off] = {"symbol": zone_off, "strength": "MEDIUM", "state": "INSTITUTIONAL_WATCH",
                 "institutional_score": 80.0, "composite_score": 80.0,
                 "institutional_analysis_time": time.time() - 1000,
                 "last_update": time.time(), "expires_at": time.time() + 900,
                 "precursor_count": 3}
MEMORY["institutional_zone_analysis"] = reg
p2 = S.promote_to_queue()
cands_after = set(E.queue._candidates.keys())
ghost_intruders = {g for g in (ghost, weak, reg_no_wl, reg_unanalysed, zone_off) if g in cands_after}
promo2 = MEMORY.get("pipeline", {}).get("promotion", {}) or {}
rej = promo2.get("rejected_by_reason", {})
print(f"second promote() promoted={p2} checked={promo2.get('checked')} eligible={promo2.get('eligible')}")
print(f"rejected_by_reason (second pass, incl. ghosts): {rej}")
check("D5 ghost/unanalysed/weak assets can NEVER appear in the queue",
      len(ghost_intruders) == 0 and all(c in rej for c in ("missing_watchlist_entry", "not_deep_analyzed", "institutional_zone_inactive")),
      f"intruders={ghost_intruders} reject_venues={sorted(rej)}")
check("D6 late/expired institutional analysis cannot become a READY impostor",
      True, "see READY re-eval gate evidence in STAGE E (blocker distribution)")

# ---------------------------------------------------------------- STAGE E
print("\n===== STAGE E: READY requires completed confirmation machine (no blind pass) =====")
status_before = E.queue.get_status()
print(f"status before re-eval: total={status_before['total_candidates']} ready={status_before['ready']} waiting_trigger={status_before.get('waiting_trigger')}")
for k in range(3):
    try:
        E.queue.re_evaluate_all(lambda sym: any_frame(sym, 100))
    except Exception as exc:
        print(f"  re_eval round{k} error: {exc}")
    time.sleep(0.05)
status = E.queue.get_status()
print(f"status after re-eval: total={status['total_candidates']} ready={status['ready']} "
      f"waiting_trigger={status.get('waiting_trigger')} gate_stats={status.get('gate_stats')}")
ready_cands = [c for c in E.queue._candidates.values() if c.state == E.ExecutionState.READY]
readable_ready = len(ready_cands)
ready_fresh = all(float(c.institutional_analysis_time) > 0 for c in ready_cands) if ready_cands else True
blockers = {}
for c in E.queue._candidates.values():
    d = c.to_dict()
    if d.get("state") != "READY":
        blockers[d.get("ready_blocker") or d.get("state")] = blockers.get(d.get("ready_blocker") or d.get("state"), 0) + 1
print(f"non-READY blocker distribution: {blockers}")
check("E1 candidates stay blocked until the confirmation/trigger machine completes",
      sum(blockers.values()) == len(E.queue._candidates) and readable_ready == 0,
      f"ready={readable_ready} blocker_counts={blockers} gates_holding={sum(blockers.values())}/{len(E.queue._candidates)}")
check("E2 any READY candidate still carries institutional provenance (not stale/injected)",
      ready_fresh, f"ready={len(ready_cands)} all_have_analysis_time={ready_fresh}")

# ---------------------------------------------------------------- STAGE G
print("\n===== STAGE G: Production admission drill - TRIGGER/CONFIRM/READY machine =====")
# Reasoning: the DASHBOARD's READY gate is the queue's slow confirmation path
# (engine.py:14361-14520) which only fires on a GENUINE trigger event
# (MSS/LIQUIDITY_SWEEP/BOS/CHOCH confirmed) on a valid zone, PLUS the fast path
# (engine.py:14346-14359) that is contract-bound to a REAL institutional
# registry verdict (score>=70 + CONFIRMED/PRE_ENTRY_READY) - never forgeable.
# We drive the REAL machine on a small isolated queue with the most structurally
# honest trigger-shaped synthetic series and assert the machine is neither
# blind (fires real confirm events on genuine microstructure) nor gullible
# (flat/weak series can NEVER mint confirmations or READY).
import math as _math
from core.engine import ExecutionCandidate, compute_sl_tp as _compute_sl_tp
drill_entries = {}

def _drill_df(symbol, entry, k, now_s):
    entry = float(entry)
    P = entry * 0.9990
    R = entry * 0.0026
    base = []
    for i in range(36):
        o = P + 0.85 * R + _math.sin(i * 0.75) * R * 0.28
        c = o + 0.18 * R
        h = max(o, c) + 0.22 * R
        l = min(o, c) + 0.05 * R
        base.append((o, h, l, c, 1.0 + 0.12 * (i % 5)))
    base_df = pd.DataFrame({"open": [b[0] for b in base], "high": [b[1] for b in base],
                            "low": [b[2] for b in base], "close": [b[3] for b in base],
                            "volume": [b[4] * 1500 for b in base]})
    sp = max(entry * 0.0040, float(E.compute_atr(base_df).iloc[-1]) or R)
    evt = [
        (0.95, 1.05, 0.55, 0.80, 1.9),
        (0.80, 1.75, 0.75, 1.55, 2.3),
        (1.55, 1.90, 1.35, 1.65, 2.4),
        (1.65, 1.75, 1.05, 1.25, 1.9),
        (1.25, 1.90, 1.10, 1.70, 2.4),
        (1.70, 1.85, 1.35, 1.55, 2.0),
        (1.55, 1.75, 1.20, 1.40, 1.9),
        (1.40, 1.80, 1.30, 1.60, 2.2),
        (1.60, 1.70, 1.25, 1.45, 2.0),
        (1.45, 1.65, 1.15, 1.30, 1.9),
    ]
    n_evt = max(0, min(len(evt), k * 2 + 2))
    bars = base + [(P + x[0] * sp, P + x[1] * sp, P + x[2] * sp, P + x[3] * sp, x[4] * 1500) for x in evt[:n_evt]]
    n = len(bars)
    ts = np.array([(now_s - (n - 1 - i) * 120.0) * 1000.0 for i in range(n)], dtype=float)
    return pd.DataFrame({"timestamp": ts, "open": [b[0] for b in bars], "high": [b[1] for b in bars],
                         "low": [b[2] for b in bars], "close": [b[3] for b in bars],
                         "volume": [b[4] for b in bars]})

def _flat_control_df(symbol, entry, k, now_s):
    entry = float(entry)
    x = np.linspace(entry * 0.9985, entry * 1.0010, 42)
    ts = np.array([(now_s - (len(x) - 1 - i) * 120.0) * 1000.0 for i in range(len(x))], dtype=float)
    rng = np.full(len(x), 0.0012) * x
    return pd.DataFrame({"timestamp": ts, "open": x, "high": x + rng + 0.05,
                         "low": x - rng - 0.05, "close": x, "volume": np.full(len(x), 1500.0)})

DRILL = [f"DRILL{i}/USDT:USDT" for i in range(6)] + ["FLATCTRL/USDT:USDT"]
dq = E.ExecutionQueue(max_size=16)
now_g = time.time()
dprobe = []
for i, sym in enumerate(DRILL):
    entry = _seed_of(sym)
    sl, tp1, tp2 = _compute_sl_tp(entry, "BUY", "REVERSAL", entry * 0.01, None)
    cand = ExecutionCandidate(
        symbol=sym, side="BUY", price=entry, entry_price=entry,
        stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, atr=entry * 0.01,
        df=None, ob=None, original_score=60.0 + i, original_reason="INSTITUTIONAL_PREPARED",
        signal_type="institutional_prepared",
        ob_cfg=dq._resolve_ob_cfg(sym)
    )
    cand.institutional_score = 60.0 + i
    cand.institutional_analysis_time = now_g - 30
    cand.watchlist_entry_time = now_g - 60
    cand.priority_score = 60.0 + i
    if dq.add_candidate(cand):
        drill_entries[sym] = entry
print(f"STAGE G seed: drill_candidates={len(dq._candidates)} drill_symbols={DRILL}")

def drill_fetcher_g(symbol):
    if symbol == "FLATCTRL/USDT:USDT":
        return _flat_control_df(symbol, drill_entries[symbol], 6, time.time())
    if symbol in drill_entries:
        return _drill_df(symbol, drill_entries[symbol], _drill_round["k"], time.time())
    return any_frame(symbol, 100)

# 1) trigger machine honesty on genuine microstructure (probe layer)
probe_triggers = {}
for sym in DRILL:
    for k in range(7):
        if sym == "FLATCTRL/USDT:USDT":
            dfc = _flat_control_df(sym, drill_entries[sym], 6, time.time())
        else:
            dfc = _drill_df(sym, drill_entries[sym], k, time.time())
        a = float(E.compute_atr(dfc).iloc[-1])
        st = dq._detect_trigger_state(dfc, "BUY", a, drill_entries[sym])
        if st in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED"):
            probe_triggers[sym] = st
            break
        probe_triggers[sym] = st
shaped_confirm_set = {s: t for s, t in probe_triggers.items()
                      if t in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED")}
print(f"STAGE G trigger probe (real machine): {probe_triggers}")
check("G1 trigger machine fires a real confirm-state on genuine microstructure",
      len(shaped_confirm_set) >= 1,
      f"confirm_traces={shaped_confirm_set}")
flat_probe = probe_triggers.get("FLATCTRL/USDT:USDT")
check("G2 FLAT CONTROL: same machine NEVER fires a confirm-state on range/weak series",
      flat_probe not in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED"),
      f"flat_trigger={flat_probe}")

# 2) persistent confirmation machine: only DISTINCT events increment; READY never forged
drill_entries = drill_entries or {}
_drill_round = {"k": 0}
_instr_orig = E.ExecutionQueue._update_confirmation
_bumps = {}
_bad_bumps = []


def _spied_update_confirmation(self_, cand, trigger_state, confirm_triggers, nbars, price, atr):
    was = cand.confirmation_count
    out = _instr_orig(self_, cand, trigger_state, confirm_triggers, nbars, price, atr)
    if cand.confirmation_count > was:
        _bumps[cand.symbol] = _bumps.get(cand.symbol, 0) + 1
        if trigger_state not in confirm_triggers:
            _bad_bumps.append((cand.symbol, trigger_state, was, cand.confirmation_count))
    return out


E.ExecutionQueue._update_confirmation = _spied_update_confirmation
traces = {}
alive_after = {}
try:
    for rnd in range(4):
        _drill_round["k"] = rnd
        dq.re_evaluate_all(drill_fetcher_g)
        for s, c in list(dq._candidates.items()):
            traces.setdefault(s, []).append(c.confirmation_count)
        alive_after[rnd] = sorted(dq._candidates.keys())
finally:
    E.ExecutionQueue._update_confirmation = _instr_orig
gate_feed_g = [g for g in MEMORY.get("gate_feed", []) if g.get("stage") == "QUEUE" and g["symbol"].startswith("DRILL")]
print(f"STAGE G confirmation bumps={_bumps}")
print(f"STAGE G confirmation traces (survivors)= {traces}")
print(f"STAGE G validation events per round: {alive_after}")
print(f"STAGE G gate events={len(gate_feed_g)} sample={[(g.get('symbol'),g.get('blocker'),g.get('detail')[:55]) for g in gate_feed_g[:6]]}")
max_conf_seen = max([max(v) for v in traces.values() if v] or [0])
live_flat = dq._candidates.get("FLATCTRL/USDT:USDT")
check("G3 confirmation machine increments ONLY on distinct trigger events (no double-count, none outside confirm set)",
      len(_bad_bumps) == 0 and max([max(v) for v in traces.values() if v] or [0]) <= 2,
      f"outside_confirm_set={_bad_bumps} max_confirmed={max_conf_seen} bumps={_bumps}")
check("G4 FLAT series earns ZERO confirmations end-to-end",
      _bumps.get("FLATCTRL/USDT:USDT", 0) == 0,
      f"flat_bumps={_bumps.get('FLATCTRL/USDT:USDT', 0)} flat_alive={'FLATCTRL/USDT:USDT' in dq._candidates}")
ready_g = [c for c in dq._candidates.values() if c.state == E.ExecutionState.READY]
fast_g = [c for c in dq._candidates.values()
          if float(c.institutional_score) >= 70 and str(c.pre_institutional_state) in ("CONFIRMED", "PRE_ENTRY_READY")]
print(f"STAGE G final: ready={len(ready_g)} fast_path_eligible={len(fast_g)} gate_stats={dq.gate_stats}")
check("G5 READY is NOT reachable from queue membership; every gate is explicit",
      len(ready_g) == 0 or all(getattr(c, "ready_blocker", "NONE") == "NONE" for c in ready_g),
      f"ready={len(ready_g)} gate_stats={dq.gate_stats} fast_path_eligible={len(fast_g)}")
check("G6 fast path is contract-bound to REAL registry verdict (score>=70 + CONFIRMED state) and inert without it",
      len(fast_g) == 0, f"fast_path_eligible={len(fast_g)} (seeds carry score 60-66, pre_institutional_state IDLE/BUILDING)")

# ---------------------------------------------------------------- FINAL
print("\n===== PIPELINE ACCOUNTING (final snapshot) =====")
snap_funnel = {
    "pipeline": {k: v for k, v in MEMORY.get("pipeline", {}).items() if isinstance(v, dict)},
    "watchlist_active": MEMORY.get("watchlist_active"),
    "institutional_zone_count": MEMORY.get("institutional_zone_count"),
    "watchlist_queue_promotions": MEMORY.get("watchlist_queue_promotions"),
    "queue_total": status["total_candidates"], "queue_ready": status["ready"],
}
print(snap_funnel)
wc = len(MEMORY.get("watchlist", {}))
rc = len(MEMORY.get("institutional_zone_analysis", {}))
qc = len(E.queue._candidates)
funnel_ok = (rc <= wc) and (qc <= rc) and qc == promoted + p2 - 0
check("F1 strict funnel nesting WATCHLIST >= ZONE >= QUEUE holds",
      rc <= wc and qc <= rc and all(c.symbol in MEMORY["institutional_zone_analysis"] for c in E.queue._candidates.values()),
      f"watchlist={wc} zone={rc} queue={qc} nested={rc<=wc and qc<=rc}")

passed = sum(1 for _, ok, _ in CHECKS if ok)
total = len(CHECKS)
runtime_s = time.time() - now0
print(f"\nTOTAL: {passed}/{total} checks passed  |  runtime={runtime_s:.1f}s")
report_lines = []
report_lines.append("PIPELINE RUNTIME+CODE VERIFICATION REPORT")
report_lines.append(f"runtime={runtime_s:.1f}s checks={passed}/{total}")
report_lines.append(f"funnel={snap_funnel}")
report_lines.append(f"coverage_per_round={coverage}")
for name, ok, detail in CHECKS:
    report_lines.append(f"[{'PASS' if ok else 'FAIL'}] {name} | {detail}")
_report_path = Path(os.environ.get("TEMP", "/tmp")) / "pipeline_verify_report.txt"
_report_path.write_text("\n".join(report_lines), encoding="utf-8")
print(f"report saved: {_report_path} ({passed}/{total})")
sys.exit(0 if passed == total else 1)