#!/usr/bin/env python3
# ====================================================================
# RF LIQUIDITY ENGINE v28 – INSTITUTIONAL INTENT EDITION
# [PRODUCTION READY] Institutional Discovery + Dynamic Execution Queue
# ====================================================================
# ENHANCEMENTS (2026-07-29):
# - Institutional Intent Engine (9-layer gatekeeper)
# - Dynamic Trade Management (adaptive SL, multi-stage TP, runner)
# - Watchlist Priority Manager
# - Dashboard extensions (Intent, Lifecycle, Probability)
# - FIXES applied (2026-08-14): Dual authority, partial close, regime-aware TP1,
#   pre-TP1 BE, healthy pullback, trigger confirmation, PAPER_MODE, MEMORY init,
#   duplicate log, startup path validation.
# ====================================================================
# SURGICAL ENTRY-QUALITY UPGRADE (2026-08-16):
# - Liquidity Authenticity Engine (post-sweep response, trap detection)
# - Order Block Quality Engine (causal strength, freshness, defense)
# - Post-Sweep Response Evaluation (displacement, reclaim, volume)
# - Early Expansion Detector (room to run, exhaustion risk)
# - Composite Entry Quality Assessment (REJECT/WAIT/VALIDATE/EARLY_ENTRY/APPROVE)
# - Integrated into execute_entry() as the final authority.
# ====================================================================
# HOTFIX (2026-08-19): Fixed AttributeError in entry_quality_assessment
# when STATE.get('trade_thesis') returns None.
# ====================================================================
# RORO+ UPGRADE (2026-08-26):
# - Integrated Roro's exact institutional entry strategy
# - A-GRADE fast-confirm for candidates with Roro signal + Strong OB
# - Dynamic min_confirmations: 1 for A-GRADE, 2 for others
# - Strong OB selection with A+/A/B/INVALID grading
# - Full integration within ExecutionQueue (no parallel path)
# ====================================================================
# STAGGERED WATCHLIST ANALYSIS (2026-08-26):
# - Extended watchlist TTL to 1800s (30min)
# - Staggered analysis of up to 60 candidates (every 15s)
# - Full institutional/Roro/OB analysis performed on watchlist level
# - Dynamic ranking and promotion to ExecutionQueue
# - No changes to ExecutionQueue, PortfolioManager, OrderManager
# ====================================================================
# PRODUCTION FIXES (2026-08-26):
# - Added is_valid_dataframe() to prevent AttributeError on invalid df types
# - Fixed cleanup_watchlist to implement staged expiration (quarantine + expiry)
# - Enhanced _confirm_signature with DataFrame validation
# - Added robust checks in re_evaluate_all and _evaluate_liquidity
# - Fixed sweep recency state transition (LIQUIDITY_AVAILABLE added)
# - Fixed same-zone stale guard using stale_zone_refs comparison
# - Added is_valid_dataframe to all helper functions (detect_bos, build_liquidity_pools, etc.)
# ====================================================================
# EARLY INSTITUTIONAL RADAR + NEWS INTELLIGENCE (2026-08-27):
# - Added InstitutionalRadar for proactive watchlist monitoring
# - Added NewsRiskIntelligence for priority escalation (NOT entry signals)
# - Dynamic scheduling based on asset state and news impact
# - Watchlist → Early Institutional Radar → PRE_INSTITUTIONAL state → Queue
# - Institutional analysis precedes displacement, not follows it.
# ====================================================================
# TIMESTAMPED LOGGING & PIPELINE ORDER FIX (2026-08-27):
# - Added [WATCHLIST], [PRE_MOVE], [INSTITUTION], [CONFIRMATION], [EXPANSION], [LATENCY], [EXECUTION] logs
# - Stored timestamps in Watchlist and ExecutionCandidate
# - Ensured t2 < t4 (institutional analysis before expansion)
# ====================================================================
# STRATEGY UPGRADE (2026-08-28): Replaced entry logic with
# optimized strategy from test cv new.py (ADX 25-38, liquidity sweep,
# narrative confidence, etc.) while preserving all engine infrastructure.
# ====================================================================

import os
import time
import json
import threading
import atexit
import traceback
import math
import gc
import random
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional, Any
from enum import Enum
from collections import deque
import queue as qlib
import copy

try:
    from core.decision_journal import append_event as _append_decision_journal
except Exception:
    _append_decision_journal = None

# Process-wide shutdown barrier.  All background workers must observe this
# before interpreter finalization so daemon threads never write to stdout while
# Python is tearing down buffered I/O.
_RUNTIME_SHUTDOWN = threading.Event()
_RUNTIME_THREADS = []

def shutdown_requested():
    return _RUNTIME_SHUTDOWN.is_set()

def register_runtime_thread(thread):
    if thread is not None and thread not in _RUNTIME_THREADS:
        _RUNTIME_THREADS.append(thread)
    return thread

def request_shutdown():
    """Signal background workers and stop the internal event worker safely."""
    if _RUNTIME_SHUTDOWN.is_set():
        return
    _RUNTIME_SHUTDOWN.set()
    bus = globals().get("_event_bus")
    if bus is not None:
        try:
            bus.stop(join_timeout=2.0)
        except Exception:
            pass
    current = threading.current_thread()
    for thread in list(_RUNTIME_THREADS):
        if thread is None or thread is current:
            continue
        try:
            if thread.is_alive():
                thread.join(timeout=2.0)
        except Exception:
            pass

atexit.register(request_shutdown)

import ccxt
import pandas as pd
import numpy as np
from flask import Flask, jsonify, request
import requests

# Market-session context is pure/read-only and never places orders.
try:
    from core.market_sessions import session_allows_entry
except Exception:  # pragma: no cover
    session_allows_entry = None

# Pure institutional setup intelligence. It never places orders and is safe to
# use as an evidence layer around the preserved execution kernel.
try:
    from core.trade_intelligence import analyze_setup, TradeManagementBoard
    TRADE_INTELLIGENCE_AVAILABLE = True
except Exception as _ti_import_err:  # pragma: no cover
    analyze_setup = None
    TRADE_INTELLIGENCE_AVAILABLE = False
    print("[TRADE_INTEL] unavailable:", _ti_import_err)

# ========== ULTIMATE SNIPER CONFLUENCE LAYER ==========
# Optional enrichment for the causal OB. Guarded so an import/runtime failure in
# the enrichment module never breaks the engine's core flows.
try:
    from core.sniper_enrichment import (
        SniperEnrichmentEngine,
        SniperEnrichmentConfig,
        SniperConfluence,
        LONG as SNIPER_LONG,
        SHORT as SNIPER_SHORT,
        MAX_CONFLUENCE_BONUS as SNIPER_MAX_BONUS,
        MAX_CONFLUENCE_PENALTY as SNIPER_MAX_PENALTY,
    )
    SNIPER_ENRICHMENT_AVAILABLE = True
except Exception as _sniper_import_err:  # pragma: no cover - defensive
    SniperEnrichmentEngine = None
    SniperEnrichmentConfig = None
    SniperConfluence = None
    SNIPER_LONG = 1
    SNIPER_SHORT = -1
    SNIPER_MAX_BONUS = 12.0
    SNIPER_MAX_PENALTY = -10.0
    SNIPER_ENRICHMENT_AVAILABLE = False
    print("[SNIPER] enrichment unavailable:", _sniper_import_err)

# ========== ATOM INTELLIGENCE LAYER ==========
# Quality/verification layer ON TOP of Roro's entry model (the "Roro -> Atom
# approval" pipeline). Guarded so a failure here never breaks the entry engine.
try:
    from core.atom_intelligence import (
        AtomIntelligenceEngine,
        AtomIntelligence,
        ZoneFreshness,
        LiquiditySupport,
        TRADE_TREND,
        TRADE_REVERSAL,
        TRADE_SNIPER_REVERSAL,
        MAX_CONFIDENCE_BONUS as ATOM_MAX_BONUS,
        MAX_CONFIDENCE_PENALTY as ATOM_MAX_PENALTY,
    )
    ATOM_INTELLIGENCE_AVAILABLE = True
except Exception as _atom_import_err:  # pragma: no cover - defensive
    AtomIntelligenceEngine = None
    AtomIntelligence = None
    TRADE_TREND = "TREND"
    TRADE_REVERSAL = "REVERSAL"
    TRADE_SNIPER_REVERSAL = "SNIPER_REVERSAL"
    ATOM_MAX_BONUS = 12.0
    ATOM_MAX_PENALTY = -10.0
    ATOM_INTELLIGENCE_AVAILABLE = False
    print("[ATOM-INTEL] unavailable:", _atom_import_err)

# ========== EARLY ENTRY CONFLUENCE LAYER ==========
# Advisory "start of a move out of a fresh, liquidity-backed zone" intelligence
# ON TOP of Roro + Atom. Guarded so a failure here never breaks the entry path.
try:
    from core.early_entry_confluence import (
        EarlyEntryConfluenceEngine,
        analyze_early_entry,
        ACTION_EARLY_ENTRY as EARLY_ACTION_ENTRY,
        PHASE_FIRST as EARLY_PHASE_FIRST,
        PHASE_DEVELOPING as EARLY_PHASE_DEVELOPING,
        PHASE_LATE as EARLY_PHASE_LATE,
    )
    EARLY_CONFLUENCE_AVAILABLE = True
except Exception as _early_import_err:  # pragma: no cover - defensive
    EarlyEntryConfluenceEngine = None
    analyze_early_entry = None
    EARLY_CONFLUENCE_AVAILABLE = False
    print("[ATOM-EARLY] unavailable:", _early_import_err)

# ========== EXTERNAL MARKET INTELLIGENCE ==========
try:
    from external_intelligence import ExternalIntelligenceService
    EXTERNAL_INTELLIGENCE_AVAILABLE = True
except Exception as _ext_intel_err:  # pragma: no cover
    ExternalIntelligenceService = None
    EXTERNAL_INTELLIGENCE_AVAILABLE = False
    print("[EXT-INTEL] unavailable:", _ext_intel_err)

# ========== GLOBALS (FIX: MEMORY moved here) ==========
MEMORY = {
    "candidates": [],
    "top_candidates": [],
    "regime": "NEUTRAL",
    "last_scan": 0,
    "scanned_count": 0,
    "health": {"api": "OK", "errors": 0, "status": "RUNNING"},
    "rf_watchlist": [],
    "rf_dashboard": [],
    "scanner_v2_buy": [],
    "scanner_v2_sell": [],
    "scanner_v2_last_scan": 0,
    "radar_watchlist": [],
    "radar_top5": [],
    "log_debounce": {},
    "watchlist": {},
    "no_entry_feed": [],
    "gate_feed": [],
    "pipeline": {},
    "decision_log": [],
    "watchlist_quarantine": [],
    "stale_zone_refs": {},
    "institutional_radar_data": {},
    "pre_institutional_watch": {},
    "watchlist_top5": [],
    "news_alerts": {},
    "external_intelligence": {},
    "external_intelligence_top": [],
    "external_intelligence_last_scan": 0,
    "external_intelligence_status": "INIT"
}

# ===== Staggered Watchlist Analysis State =====
_watchlist_analysis_pointer = 0
_watchlist_analysis_last_run = 0
_watchlist_ranking_last_update = 0
_watchlist_analysis_stats = {
    "total_analyzed": 0,
    "analyzed_this_cycle": 0,
    "stale_analysis": 0,
    "promoted": 0,
    "skipped": 0,
    "expired": 0,
    "top_candidates": []
}

# ========== ANSI COLOR CODES ==========
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

def color_pnl(pnl_pct):
    return f"{GREEN}{pnl_pct:.2f}%{RESET}" if pnl_pct >= 0 else f"{RED}{pnl_pct:.2f}%{RESET}"

def color_text(text, color):
    return f"{color}{text}{RESET}"

# ========== SANITIZATION & JSON FIX ==========
def safe_json(obj):
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (pd.Series, pd.DataFrame)):
        return obj.to_dict() if hasattr(obj, 'to_dict') else str(obj)
    if isinstance(obj, dict):
        return {k: safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_json(i) for i in obj]
    return obj

def to_json_safe(obj):
    try:
        if obj is None:
            return {}
        if hasattr(obj, "to_dict"):
            return safe_json(obj.to_dict(orient="records"))
        if isinstance(obj, (dict, list, str, int, float, bool)):
            return safe_json(obj)
        return str(obj)
    except:
        return {}

def safe_get(d, key, default=None):
    if d is None:
        return default
    return d.get(key, default)

def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except:
        return default

# ========== CACHE & RATE LIMIT ==========
CACHE = {
    "balance": {"value": 0.0, "ts": 0},
    "free_balance": {"value": 0.0, "ts": 0},
    "ohlcv": {"value": {}, "ts": 0},
    "ticker": {"value": {}, "ts": 0},
    "orderbook": {"value": {}, "ts": 0},
    "dashboard": {"value": None, "ts": 0},
    "decision": {"value": None, "ts": 0}
}
_last_api_call = 0
MIN_API_INTERVAL = 0.2

def rate_limit():
    global _last_api_call
    now = time.time()
    elapsed = now - _last_api_call
    if elapsed < MIN_API_INTERVAL:
        time.sleep(MIN_API_INTERVAL - elapsed)
    _last_api_call = time.time()

def cache_get(key, ttl, subkey=None):
    item = CACHE.get(key)
    if item and isinstance(item, dict) and "ts" in item and "value" in item:
        if time.time() - item["ts"] < ttl:
            if subkey:
                val = item["value"]
                if isinstance(val, dict) and subkey in val:
                    return val[subkey]
                return None
            return item["value"]
    return None

def cache_set(key, value, subkey=None):
    if subkey:
        if key not in CACHE or not isinstance(CACHE.get(key), dict) or "value" not in CACHE[key]:
            CACHE[key] = {"value": {}, "ts": time.time()}
        CACHE[key]["value"][subkey] = value
    else:
        CACHE[key] = {"value": value, "ts": time.time()}

def safe_api_call(func, *args, **kwargs):
    for attempt in range(3):
        try:
            rate_limit()
            return func(*args, **kwargs)
        except Exception as e:
            if "rate limit" in str(e).lower() or "100410" in str(e):
                wait = 2 ** attempt
                print(color_text(f"Rate limit hit, waiting {wait}s...", YELLOW))
                time.sleep(wait)
                continue
            if attempt == 2:
                raise
            time.sleep(1)
    return None

# ========== VENUE SYMBOL AVAILABILITY GUARD ==========
# BingX can temporarily pause individual contracts (error 109415). A paused
# contract is NOT equivalent to an externally closed position. The guard keeps
# the scanner/portfolio loop from hammering the same dead symbol and, more
# importantly, prevents a failed symbol-specific position query from erasing a
# real local position.
class SymbolAvailabilityGuard:
    PAUSE_CODE = "109415"
    def __init__(self, cooldown_sec=300.0):
        self.cooldown_sec = float(os.getenv("SYMBOL_PAUSE_COOLDOWN_SEC", str(cooldown_sec)))
        self._paused = {}
        self._errors = {}
        self._lock = threading.RLock()

    @staticmethod
    def is_paused_error(exc) -> bool:
        text = str(exc or "")
        return SymbolAvailabilityGuard.PAUSE_CODE in text or "pause currently" in text.lower()

    def mark_paused(self, symbol, reason=None):
        key = str(symbol or "")
        with self._lock:
            self._paused[key] = {"until": time.time()+self.cooldown_sec, "reason": str(reason or "BINGX_109415")}
        MEMORY.setdefault("paused_symbols", {})[key] = dict(self._paused[key])

    def is_paused(self, symbol) -> bool:
        key = str(symbol or "")
        with self._lock:
            item = self._paused.get(key)
            if not item:
                return False
            if time.time() >= item["until"]:
                self._paused.pop(key, None)
                MEMORY.setdefault("paused_symbols", {}).pop(key, None)
                return False
            return True

    def status(self, symbol) -> str:
        return "PAUSED" if self.is_paused(symbol) else "AVAILABLE"

    def record_error(self, symbol, exc):
        if self.is_paused_error(exc):
            self.mark_paused(symbol, exc)
            return "PAUSED"
        with self._lock:
            self._errors[str(symbol or "")] = {"time": time.time(), "error": str(exc)}
        return "ERROR"

    def snapshot(self):
        with self._lock:
            now=time.time()
            return {k: dict(v) for k,v in self._paused.items() if v.get("until",0)>now}

SYMBOL_GUARD = SymbolAvailabilityGuard()

# ========== TELEGRAM ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_last_tg_msg = {}

def _tg_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=5)
    except:
        pass

def send_once(msg, key, cooldown=60):
    now = time.time()
    if key not in _last_tg_msg or now - _last_tg_msg[key] > cooldown:
        _last_tg_msg[key] = now
        _tg_send(msg)

def tg_start(balance, mode):
    send_once(f"🚀 <b>RF v28 Professional Edition (FIXED)</b>\nBalance: {balance:.2f} USDT\nMode: {mode}\nEntry Engine: ADX flexible + Sweep + MSS required for reversals", "startup", 86400)

def tg_entry(side, symbol, entry, sl, tp, score, reason, entry_type):
    side_emoji = "🟢" if side == "BUY" else "🔴"
    entry_type_str = f"{entry_type} NARRATIVE" if entry_type == "NARRATIVE" else entry_type
    send_once(f"{side_emoji} <b>{side} {entry_type_str}</b>\n📊 {symbol}\n💰 Entry: {entry:.4f}\n🛑 SL: {sl:.4f}\n🎯 TP: {tp:.4f}\n🧠 Score: {score}\n📌 {reason[:100]}", f"entry_{symbol}", 60)

def tg_tp_hit(symbol, tp_level, pnl_pct):
    send_once(f"🎯 <b>TP{tp_level} HIT</b> on {symbol}\nPnL: {pnl_pct:.2f}%", f"tp_{symbol}_{tp_level}", 30)

def tg_sl_hit(symbol, pnl_pct):
    send_once(f"🛑 <b>STOP LOSS HIT</b> on {symbol}\nPnL: {pnl_pct:.2f}%", f"sl_{symbol}", 30)

def tg_close(symbol, pnl_pct, duration_min, side):
    icon = "✅" if pnl_pct >= 0 else "❌"
    send_once(f"{icon} <b>CLOSE</b> {symbol} ({side})\nPnL: {pnl_pct:.2f}%\n⏱ {duration_min:.0f} min", f"close_{symbol}", 10)

def tg_error(err_msg, error_type="EXECUTION"):
    send_once(f"🚨 <b>ERROR</b> [{error_type}]\n{err_msg[:200]}", f"err_{error_type}_{err_msg[:50]}", 60)

# ========== CONFIGURATION ==========
API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_API_SECRET", "")
PAPER_MODE = os.getenv("PAPER_MODE", "True").strip().lower() in {"1", "true", "yes", "on"}
MODE_LIVE = bool(API_KEY and API_SECRET) and not PAPER_MODE

DEFAULT_SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
INTERVAL = os.getenv("INTERVAL", "15m")
LEVERAGE = 10

USE_PPE = True

# === EXECUTION QUEUE CONFIGURATION ===
USE_EXECUTION_QUEUE = os.getenv("USE_EXECUTION_QUEUE", "True") == "True"
QUEUE_MAX_SIZE = int(os.getenv("QUEUE_MAX_SIZE", "15"))
QUEUE_RE_EVAL_INTERVAL = int(os.getenv("QUEUE_RE_EVAL_INTERVAL", "5"))
QUEUE_PROMOTE_INTERVAL = int(os.getenv("QUEUE_PROMOTE_INTERVAL", "30"))

GLOBAL_SCAN_INTERVAL = 60 * 20
SCANNER_V2_INTERVAL = 60 * 20
MICRO_SCAN_INTERVAL = 5
TOP_LIQUID_COUNT = 80

MAX_SPREAD_PERCENT_DEFAULT = 0.08
MAX_SPREAD_PERCENT_VOLATILE = 0.15

MAX_SCALE_INS = 2
SCALE_IN_SIZE_PCT = 0.25
SCALE_IN_PROFIT_PCT = 0.5
RUNNER_PCT = 0.4
TRAIL_ATR_MULT = 1.4
ADVERSE_MOVE_ATR_MULT = 1.8
MAX_DAILY_LOSS_PCT = 5.0
MAX_CONSECUTIVE_LOSSES = 3
COOLDOWN_MINUTES_LOSS = 10
COOLDOWN_MINUTES_DRAWDOWN = 20

SNAPSHOT_INTERVAL = 15
BASE_SLEEP = 5
KEEP_ALIVE_INTERVAL = 300

# External intelligence is advisory/alert-only. It must never call execute_entry().
EXTERNAL_INTELLIGENCE_ENABLED = os.getenv("EXTERNAL_INTELLIGENCE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
EXTERNAL_INTELLIGENCE_INTERVAL_SEC = float(os.getenv("EXTERNAL_INTELLIGENCE_INTERVAL_SEC", "600"))
EXTERNAL_INTELLIGENCE_ALERT_SCORE = float(os.getenv("EXTERNAL_INTELLIGENCE_ALERT_SCORE", "70"))

BALANCE_SAFETY_FACTOR = 0.98
INSUFFICIENT_MARGIN_COOLDOWN_SEC = 60

# === PORTFOLIO MARGIN POLICY (real, end-to-end) ===
# Single source of truth mirrored inside portfolio/risk.PortfolioRiskGuard.
#   * POSITION_MARGIN_PCT: every position commits this slice of its available
#     free balance at OPEN (engine.execute_entry uses it directly).
#   * PORTFOLIO_MARGIN_CAP_PCT: total committed exposure across ALL open
#     positions can never exceed this fraction of account equity. The paper
#     engine enforces it against the actual committed_margin ledger before any
#     new commit, so a 7th direct entry is blocked even outside the manager.
#   * MAX_OPEN_POSITIONS: global cap on simultaneous open positions (6-slot
#     technical model: 2 CRYPTO / 2 INDEX / 1 GOLD / 1 OIL + independent NEWS).
POSITION_MARGIN_PCT = float(os.getenv("POSITION_MARGIN_PCT", "0.10"))
PORTFOLIO_MARGIN_CAP_PCT = float(os.getenv("PORTFOLIO_MARGIN_CAP_PCT", "0.60"))
MAX_OPEN_POSITIONS = max(1, int(os.getenv("MAX_OPEN_POSITIONS", "6")))

SCAN_INTERVAL = 900
WATCHLIST_REFRESH = 300
RADAR_COOLDOWN_SEC = 1800
LAST_ENTRY_PER_SYMBOL = {}

INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = None

ex = ccxt.bingx({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": int(os.getenv("EXCHANGE_TIMEOUT_MS", "10000")),
    "options": {"defaultType": "swap"}
})

def resolve_exchange_symbol(symbol):
    """Resolve a requested symbol to the exact CCXT market key when possible."""
    requested = str(symbol or "").strip()
    markets = getattr(ex, "markets", None) or {}
    if requested in markets:
        return requested
    upper = requested.upper()
    if upper in markets:
        return upper
    def aliases(value):
        raw = str(value).upper().strip()
        forms = {raw}
        forms.add(raw.replace(":USDT", ""))
        forms.add(raw.replace("-USDT", ""))
        forms.add(raw.replace("/USDT", ""))
        forms.add(raw.replace("/USDT:USDT", ""))
        forms.add(raw.replace(":USDT", "/USDT"))
        forms.add(raw.replace("-USDT", "/USDT"))
        return forms
    wanted = aliases(requested)
    for key in markets:
        if wanted.intersection(aliases(key)):
            return key
    return requested if requested.endswith(":USDT") else f"{requested}:USDT"

def normalize_symbol(symbol):
    return resolve_exchange_symbol(symbol)

def set_leverage(symbol, leverage):
    try:
        sym = normalize_symbol(symbol)
        if hasattr(ex, 'set_leverage'):
            ex.set_leverage(leverage, sym)
    except Exception as e:
        print(color_text(f"set_leverage warning: {e}", YELLOW))

# ========== LIVE HYBRID DATAFRAME ==========
_live_high = {}
_live_low = {}
_last_candle_timestamp = {}

def get_live_hybrid_df(symbol, base_df: pd.DataFrame, live_price: float) -> pd.DataFrame:
    if base_df is None or base_df.empty or live_price is None or live_price <= 0:
        return base_df
    df = base_df.copy()
    last_idx = df.index[-1]
    if 'timestamp' in df.columns:
        current_ts = df.loc[last_idx, 'timestamp']
    else:
        current_ts = last_idx
    global _last_candle_timestamp, _live_high, _live_low
    prev_ts = _last_candle_timestamp.get(symbol)
    if prev_ts is None or current_ts != prev_ts:
        _last_candle_timestamp[symbol] = current_ts
        _live_high[symbol] = df.loc[last_idx, 'high']
        _live_low[symbol] = df.loc[last_idx, 'low']
    else:
        _live_high[symbol] = max(_live_high.get(symbol, df.loc[last_idx, 'high']), live_price)
        _live_low[symbol] = min(_live_low.get(symbol, df.loc[last_idx, 'low']), live_price)
    df.loc[last_idx, 'high'] = _live_high[symbol]
    df.loc[last_idx, 'low'] = _live_low[symbol]
    df.loc[last_idx, 'close'] = live_price
    return df

# ========== DATA FETCHING (OPTIMIZED CACHE) ==========
def fetch_ohlcv(symbol, limit=150):
    try:
        if SYMBOL_GUARD.is_paused(symbol):
            return None
        sym = normalize_symbol(symbol)
        data = safe_api_call(ex.fetch_ohlcv, sym, INTERVAL, limit=limit)
        if not data or len(data) < 100:
            return None
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        df = df.dropna()
        if len(df) < 100:
            return None
        if (df['close'] == 0).any() or (df['high'] == 0).any() or (df['low'] == 0).any():
            return None
        df = df.sort_index().drop_duplicates(subset=['timestamp']).ffill().bfill()
        if len(df) < 100:
            return None
        return df
    except Exception as e:
        if SYMBOL_GUARD.record_error(symbol, e) == "PAUSED":
            log_execution(f"[SYMBOL_GUARD] {symbol} paused by venue; OHLCV suppressed for {SYMBOL_GUARD.cooldown_sec:.0f}s", "WARN", debounce_key=f"paused_ohlcv_{symbol}", debounce_sec=60)
        else:
            print(color_text(f"fetch_ohlcv error for {symbol}: {e}", YELLOW))
        return None

def fetch_ohlcv_htf(symbol, timeframe='1h', limit=200):
    try:
        if SYMBOL_GUARD.is_paused(symbol):
            return None
        sym = normalize_symbol(symbol)
        data = safe_api_call(ex.fetch_ohlcv, sym, timeframe, limit=limit)
        if not data or len(data) < 30:
            return None
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        df = df.dropna()
        if len(df) < 30:
            return None
        df = df.sort_index().drop_duplicates().ffill().bfill()
        return df
    except Exception as e:
        return None

def fetch_ticker(symbol):
    if SYMBOL_GUARD.is_paused(symbol):
        return None
    try:
        return safe_api_call(ex.fetch_ticker, normalize_symbol(symbol))
    except Exception as e:
        SYMBOL_GUARD.record_error(symbol, e)
        return None

def fetch_orderbook(symbol, limit=20):
    return safe_api_call(ex.fetch_order_book, normalize_symbol(symbol), limit)

def get_balance():
    if PAPER_MODE:
        return paper["balance"]
    bal = safe_api_call(ex.fetch_balance)
    if bal:
        return bal.get("total", {}).get("USDT", 0.0)
    return 0.0

def get_free_balance():
    if PAPER_MODE:
        return paper["balance"]
    bal = safe_api_call(ex.fetch_balance)
    if bal:
        return bal.get("free", {}).get("USDT", 0.0)
    return 0.0

def get_spread_bps(symbol):
    try:
        ob = get_orderbook_cached(symbol, 5)
        if ob and ob['asks'] and ob['bids']:
            ask = ob['asks'][0][0]
            bid = ob['bids'][0][0]
            return (ask - bid) / bid * 100
    except:
        pass
    return 100.0

def validate_dataframe(df, min_length=100):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    required = ["open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in required):
        return False
    if df[required].iloc[-min_length:].isna().any().any():
        return False
    if (df['close'].iloc[-min_length:] == 0).any():
        return False
    if df['close'].iloc[-min_length:].std() < 1e-8:
        return False
    return True

def is_valid_dataframe(df, required_cols=None):
    if df is None:
        return False
    if not isinstance(df, pd.DataFrame):
        return False
    if required_cols is None:
        required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    return all(col in df.columns for col in required_cols)

def get_ohlcv_safe(symbol, limit=120, htf=False):
    ttl = 15 if (STATE.get("open") or TRADE_STATE["in_position"]) else 30
    if htf:
        ttl = max(ttl, 45)
    cache_key = f"ohlcv_{symbol}_{INTERVAL}_{limit}_htf" if htf else f"ohlcv_{symbol}_{INTERVAL}_{limit}"
    cached = cache_get("ohlcv", ttl, cache_key)
    if cached is not None:
        if isinstance(cached, pd.DataFrame) and len(cached) >= 100:
            return cached
    if htf:
        df = fetch_ohlcv_htf(symbol, '1h', limit)
    else:
        df = fetch_ohlcv(symbol, limit)
    if df is not None and validate_dataframe(df, min(limit, 100)):
        cache_set("ohlcv", df, cache_key)
        return df
    return None

def get_ticker_safe(symbol):
    cached = cache_get("ticker", 2, symbol)
    if cached is not None:
        return cached
    ticker = fetch_ticker(symbol)
    if ticker:
        price = ticker["last"]
        if price and price > 0:
            cache_set("ticker", price, symbol)
            return price
    return None

def get_balance_safe():
    cached = cache_get("balance", 10)
    if cached is not None:
        return cached
    bal = get_balance()
    cache_set("balance", bal)
    return bal

def get_free_balance_safe():
    cached = cache_get("free_balance", 10)
    if cached is not None:
        return cached
    bal = get_free_balance()
    cache_set("free_balance", bal)
    return bal

def get_orderbook_cached(symbol, limit=20):
    if STATE.get("open") or TRADE_STATE["in_position"]:
        cached = cache_get("orderbook", 60, f"{symbol}_{limit}")
        if cached is not None:
            return cached
        return None
    cached = cache_get("orderbook", 1, f"{symbol}_{limit}")
    if cached is not None:
        return cached
    ob = fetch_orderbook(symbol, limit)
    if ob:
        cache_set("orderbook", ob, f"{symbol}_{limit}")
    return ob

# ========== EXCHANGE POSITION SYNC ==========
def fetch_position_status(symbol):
    """Return (position, status). status is OK/NOT_FOUND/PAUSED/ERROR.

    PAUSED and ERROR are intentionally distinct from NOT_FOUND so callers never
    convert an exchange query failure into a false local close.
    """
    if PAPER_MODE:
        return None, "NOT_FOUND"
    if SYMBOL_GUARD.is_paused(symbol):
        return None, "PAUSED"
    try:
        sym = normalize_symbol(symbol)
        # Prefer account-wide position snapshots. BingX may reject a query that
        # names a paused contract, while the account-wide endpoint remains a
        # valid source for other live positions and avoids repeated 109415 loops.
        if hasattr(ex, 'fetch_positions'):
            positions = safe_api_call(ex.fetch_positions)
        elif hasattr(ex, 'fetch_open_positions'):
            positions = safe_api_call(ex.fetch_open_positions)
        else:
            return None, "ERROR"
        if not positions:
            return None, "NOT_FOUND"
        wanted = normalize_symbol(symbol)
        for pos in positions:
            pos_sym = str(pos.get('symbol', '') or '')
            try:
                contracts = abs(float(pos.get('contracts', 0) or 0))
            except Exception:
                contracts = 0.0
            if contracts > 0 and (pos_sym == wanted or normalize_symbol(pos_sym) == wanted):
                return pos, "OK"
        return None, "NOT_FOUND"
    except Exception as e:
        status = SYMBOL_GUARD.record_error(symbol, e)
        if status == "PAUSED":
            log_execution(f"[SYMBOL_GUARD] {symbol} is PAUSED on BingX (109415); position query degraded safely", "WARN", debounce_key=f"paused_pos_{symbol}", debounce_sec=60)
            return None, "PAUSED"
        log_execution(f"[POS_SYNC] fetch_position error: {e}", "ERROR")
        return None, "ERROR"

def fetch_position(symbol):
    pos, status = fetch_position_status(symbol)
    return pos if status == "OK" else None

def get_mark_price(symbol):
    if PAPER_MODE:
        return get_ticker_safe(symbol)
    pos = fetch_position(symbol)
    if pos and 'markPrice' in pos and pos['markPrice']:
        return float(pos['markPrice'])
    return get_ticker_safe(symbol)

# ========== TRADE THESIS ENGINE ==========
from dataclasses import dataclass, field

@dataclass
class TradeThesis:
    thesis_id: str
    symbol: str
    side: str
    trade_type: str
    created_at: float
    entry_reason: List[str] = field(default_factory=list)
    continuation_factors: List[str] = field(default_factory=list)
    invalidation_factors: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    market_context: Dict = field(default_factory=dict)
    confidence: float = 0.0
    continuation_probability: float = 0.5
    exhaustion_probability: float = 0.0
    thesis_strength: float = 0.0
    current_status: str = "ACTIVE"
    last_update: float = field(default_factory=time.time)

class TradeThesisEngine:
    def build_thesis(self, symbol: str, side: str, trade_type: str, market_state: Dict,
                     narrative: Dict, entry_context: Dict) -> TradeThesis:
        reasons = []
        continuation = []
        invalidation = []
        risks = []
        adx = market_state.get("adx", 0)
        regime = market_state.get("regime", "UNKNOWN")
        continuation_probability = 0.5
        if adx > 25:
            reasons.append("strong_trend_environment")
            continuation.append("adx_expansion")
            continuation_probability += 0.1
        if market_state.get("di_dominance", False):
            reasons.append("di_dominance")
            continuation.append("persistent_pressure")
            continuation_probability += 0.1
        if market_state.get("weak_pullback", False):
            reasons.append("weak_pullback")
            continuation.append("counter_move_weakness")
            continuation_probability += 0.1
        if market_state.get("structure_aligned", False):
            reasons.append("market_structure_alignment")
            continuation_probability += 0.1
        narrative_class = narrative.get("classification", "NEUTRAL")
        if narrative_class in ("TREND_CONTINUATION", "INSTITUTIONAL_CONTINUATION"):
            reasons.append("institutional_narrative_alignment")
            continuation_probability += 0.1
        if adx > 45:
            risks.append("trend_exhaustion_risk")
        if market_state.get("counter_displacement", 0) > 1.0:
            risks.append("counter_displacement_risk")
        if regime == "CHOP":
            risks.append("choppy_environment")
        invalidation.extend(["ema_loss", "di_flip", "failed_continuation", "vwap_reclaim", "strong_counter_displacement"])
        confidence = min(continuation_probability, 0.95)
        thesis_strength = (len(reasons) * 1.2 + len(continuation) * 1.5 - len(risks) * 0.8)
        thesis = TradeThesis(
            thesis_id=f"{symbol}_{int(time.time())}",
            symbol=symbol,
            side=side,
            trade_type=trade_type,
            created_at=time.time(),
            entry_reason=reasons,
            continuation_factors=continuation,
            invalidation_factors=invalidation,
            risk_factors=risks,
            market_context=market_state,
            confidence=round(confidence, 2),
            continuation_probability=round(continuation_probability, 2),
            exhaustion_probability=0.0,
            thesis_strength=round(thesis_strength, 2)
        )
        return thesis

    def update_thesis(self, thesis: TradeThesis, market_state: Dict) -> TradeThesis:
        continuation_prob = thesis.continuation_probability
        exhaustion_prob = thesis.exhaustion_probability
        trend_health = market_state.get("trend_health", 5)
        if trend_health >= 7:
            continuation_prob += 0.05
        elif trend_health <= 3:
            continuation_prob -= 0.1
        adx_slope = market_state.get("adx_slope", 0)
        if adx_slope > 0:
            continuation_prob += 0.05
        else:
            continuation_prob -= 0.03
        counter_displacement = market_state.get("counter_displacement", 0)
        if counter_displacement > 1.2:
            continuation_prob -= 0.15
            exhaustion_prob += 0.2
        if market_state.get("weak_pullback", False):
            continuation_prob += 0.08
        continuation_prob = max(0.0, min(1.0, continuation_prob))
        exhaustion_prob = max(0.0, min(1.0, exhaustion_prob))
        thesis.continuation_probability = round(continuation_prob, 2)
        thesis.exhaustion_probability = round(exhaustion_prob, 2)
        thesis.last_update = time.time()
        return thesis

_thesis_engine = TradeThesisEngine()

# ========== REJECTION INTELLIGENCE ENGINE ==========
class RejectionIntelligence:
    @staticmethod
    def is_bearish_rejection(df, atr, zone_price=None):
        if len(df) < 1:
            return False, []
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ == 0:
            return False, []
        upper_wick = last['high'] - max(last['open'], last['close'])
        wick_condition = upper_wick >= 1.5 * body
        close_near_low = (last['close'] - last['low']) / range_ <= 0.3
        zone_failure = False
        if zone_price is not None:
            zone_failure = last['close'] < zone_price
        if len(df) >= 3:
            prev2 = df.iloc[-2]
            prev3 = df.iloc[-3]
            weak_continuation = (prev2['close'] < prev2['open'] or prev3['close'] < prev3['open'])
        else:
            weak_continuation = False
        vol_state = classify_volume(df)
        volume_ok = vol_state in ("expansion", "spike") and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 1.2
        di_plus, di_minus, _, _ = get_di_components(df)
        di_ok = (di_minus is not None and di_plus is not None and di_minus > di_plus and (di_minus - di_plus) > 2)
        is_shooting_star = (body / range_ <= 0.3 and upper_wick >= 2 * body and last['close'] < last['open'])
        is_bearish_engulfing = False
        if len(df) >= 2:
            prev = df.iloc[-2]
            is_bearish_engulfing = (prev['close'] > prev['open'] and last['close'] < last['open'] and
                                     last['high'] > prev['high'] and last['low'] < prev['low'])
        reasons = []
        score = 0
        if wick_condition:
            score += 2
            reasons.append("long_upper_wick")
        if close_near_low:
            score += 1
            reasons.append("close_near_low")
        if zone_failure:
            score += 2
            reasons.append("zone_failure")
        if weak_continuation:
            score += 1
            reasons.append("weak_continuation")
        if volume_ok:
            score += 1
            reasons.append("volume_absorption")
        if di_ok:
            score += 2
            reasons.append("di_dominance_sell")
        if is_shooting_star:
            score += 1.5
            reasons.append("shooting_star")
        if is_bearish_engulfing:
            score += 2
            reasons.append("bearish_engulfing")
        is_valid = score >= 5
        return is_valid, reasons

    @staticmethod
    def is_bullish_rejection(df, atr, zone_price=None):
        if len(df) < 1:
            return False, []
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ == 0:
            return False, []
        lower_wick = min(last['open'], last['close']) - last['low']
        wick_condition = lower_wick >= 1.5 * body
        close_near_high = (last['high'] - last['close']) / range_ <= 0.3
        zone_failure = False
        if zone_price is not None:
            zone_failure = last['close'] > zone_price
        if len(df) >= 3:
            prev2 = df.iloc[-2]
            prev3 = df.iloc[-3]
            weak_continuation = (prev2['close'] > prev2['open'] or prev3['close'] > prev3['open'])
        else:
            weak_continuation = False
        vol_state = classify_volume(df)
        volume_ok = vol_state in ("expansion", "spike") and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 1.2
        di_plus, di_minus, _, _ = get_di_components(df)
        di_ok = (di_plus is not None and di_minus is not None and di_plus > di_minus and (di_plus - di_minus) > 2)
        is_hammer = (body / range_ <= 0.3 and lower_wick >= 2 * body and last['close'] > last['open'])
        is_bullish_engulfing = False
        if len(df) >= 2:
            prev = df.iloc[-2]
            is_bullish_engulfing = (prev['close'] < prev['open'] and last['close'] > last['open'] and
                                     last['high'] > prev['high'] and last['low'] < prev['low'])
        reasons = []
        score = 0
        if wick_condition:
            score += 2
            reasons.append("long_lower_wick")
        if close_near_high:
            score += 1
            reasons.append("close_near_high")
        if zone_failure:
            score += 2
            reasons.append("zone_failure")
        if weak_continuation:
            score += 1
            reasons.append("weak_continuation")
        if volume_ok:
            score += 1
            reasons.append("volume_absorption")
        if di_ok:
            score += 2
            reasons.append("di_dominance_buy")
        if is_hammer:
            score += 1.5
            reasons.append("hammer")
        if is_bullish_engulfing:
            score += 2
            reasons.append("bullish_engulfing")
        is_valid = score >= 5
        return is_valid, reasons

# ========== MSS / CHOCH VALIDATION ==========
class MSSValidator:
    @staticmethod
    def validate_structure_shift(df, side, atr):
        if len(df) < 10:
            return False, [], 0
        last = df.iloc[-1]
        prev = df.iloc[-2]
        body = abs(last['close'] - last['open'])
        prev_body = abs(prev['close'] - prev['open'])
        displacement_strength = body / (prev_body + 1e-9) if prev_body > 0 else 1.0
        body_expansion = displacement_strength >= 1.5
        follow_through = False
        if len(df) >= 3:
            next_candle = df.iloc[-1]
            if side == "BUY":
                follow_through = next_candle['close'] > next_candle['open'] and body > prev_body
            else:
                follow_through = next_candle['close'] < next_candle['open'] and body > prev_body
        else:
            follow_through = True
        di_plus, di_minus, adx, adx_slope = get_di_components(df)
        di_spread = abs(di_plus - di_minus) if di_plus is not None and di_minus is not None else 0
        vol_state = classify_volume(df)
        volume_ok = vol_state in ("expansion", "spike")
        adx_acc = adx_slope > 0 and adx > 25
        score = 0
        reasons = []
        if body_expansion:
            score += 2
            reasons.append("body_expansion")
        if follow_through:
            score += 2
            reasons.append("follow_through")
        if di_spread > 8:
            score += 2
            reasons.append("di_spread_strong")
        if volume_ok:
            score += 1
            reasons.append("volume_confirm")
        if adx_acc:
            score += 2
            reasons.append("adx_accelerating")
        is_valid = score >= 5
        if adx < 20:
            is_valid = False
            reasons.append("adx_too_low")
        if not body_expansion and not follow_through:
            is_valid = False
            reasons.append("weak_displacement")
        return is_valid, reasons, score

# ========== ADX + DI INTELLIGENCE ==========
class ADXDIIntelligence:
    @staticmethod
    def get_adx_state(df):
        adx_series = compute_adx(df)
        if adx_series is None or len(adx_series) < 3:
            return {"state": "UNKNOWN", "value": 20, "slope": 0, "acceleration": 0}
        adx_val = adx_series.iloc[-1]
        adx_prev = adx_series.iloc[-2]
        adx_prev2 = adx_series.iloc[-3] if len(adx_series) >= 3 else adx_prev
        slope = adx_val - adx_prev
        accel = slope - (adx_prev - adx_prev2)
        if adx_val < 18:
            state = "CHOP"
        elif 18 <= adx_val < 22:
            state = "EMERGING"
        elif 22 <= adx_val < 35:
            state = "STRONG_TREND"
        elif 35 <= adx_val < 45:
            state = "VERY_STRONG"
        else:
            state = "EXHAUSTION"
        return {"state": state, "value": adx_val, "slope": slope, "acceleration": accel}

    @staticmethod
    def get_di_state(df):
        plus_di, minus_di, _, _ = get_di_components(df)
        if plus_di is None or minus_di is None:
            return {"dominant": "NEUTRAL", "spread": 0, "trend": "NEUTRAL"}
        spread = plus_di - minus_di
        if spread > 5:
            dominant = "BUY"
            trend = "BULLISH"
        elif spread < -5:
            dominant = "SELL"
            trend = "BEARISH"
        else:
            dominant = "NEUTRAL"
            trend = "CHOP"
        return {"dominant": dominant, "spread": spread, "trend": trend}

    @staticmethod
    def is_healthy_trend(df, side):
        adx_state = ADXDIIntelligence.get_adx_state(df)
        di_state = ADXDIIntelligence.get_di_state(df)
        if side == "BUY":
            return adx_state["state"] in ("STRONG_TREND", "VERY_STRONG") and di_state["dominant"] == "BUY" and adx_state["slope"] > 0
        else:
            return adx_state["state"] in ("STRONG_TREND", "VERY_STRONG") and di_state["dominant"] == "SELL" and adx_state["slope"] > 0

# ========== CONTINUATION PRESSURE ENGINE ==========
class ContinuationPressureEngine:
    @staticmethod
    def calculate_pressure(df, side, entry_price, atr, entry_time):
        if len(df) < 3:
            return 50, []
        score = 50
        reasons = []
        bodies = [abs(df['close'].iloc[-i] - df['open'].iloc[-i]) for i in range(1, 4)]
        if len(bodies) >= 2:
            growth = bodies[0] / (bodies[1] + 1e-9)
            if growth > 1.2:
                score += 10
                reasons.append("body_expansion")
            elif growth < 0.8:
                score -= 10
                reasons.append("body_contraction")
        adx_state = ADXDIIntelligence.get_adx_state(df)
        di_state = ADXDIIntelligence.get_di_state(df)
        if side == "BUY" and adx_state["slope"] > 0 and di_state["dominant"] == "BUY":
            score += 15
            reasons.append("adx_rising_di_bullish")
        elif side == "SELL" and adx_state["slope"] > 0 and di_state["dominant"] == "SELL":
            score += 15
            reasons.append("adx_rising_di_bearish")
        elif adx_state["slope"] <= 0:
            score -= 10
            reasons.append("adx_falling")
        if di_state["spread"] > 10:
            score += 10
            reasons.append("di_spread_wide")
        elif abs(di_state["spread"]) < 4:
            score -= 10
            reasons.append("di_tangled")
        vol_state = classify_volume(df)
        if vol_state == "expansion":
            score += 15
            reasons.append("volume_expansion")
        elif vol_state == "exhaustion":
            score -= 15
            reasons.append("volume_exhaustion")
        last_close = df['close'].iloc[-1]
        if side == "BUY" and last_close < entry_price:
            score -= 10
            reasons.append("price_below_entry")
        elif side == "SELL" and last_close > entry_price:
            score -= 10
            reasons.append("price_above_entry")
        consecutive = 0
        for i in range(1, min(5, len(df))):
            if side == "BUY" and df['close'].iloc[-i] > df['open'].iloc[-i]:
                consecutive += 1
            elif side == "SELL" and df['close'].iloc[-i] < df['open'].iloc[-i]:
                consecutive += 1
            else:
                break
        if consecutive >= 3:
            score += 10
            reasons.append(f"consecutive_{consecutive}")
        elif consecutive == 0:
            score -= 5
            reasons.append("no_follow_through")
        score = max(0, min(100, score))
        return score, reasons

# ========== THESIS FAILURE ENGINE ==========
class ThesisFailureEngine:
    @staticmethod
    def evaluate_failure(thesis: Dict, market_state: Dict, current_price, entry_price, side):
        if not thesis:
            return False, [], 0
        failure_score = 0
        reasons = []
        if market_state.get("strong_reclaim", False):
            failure_score += 30
            reasons.append("strong_reclaim")
        di_state = ADXDIIntelligence.get_di_state(market_state.get("df", None))
        if side == "BUY" and di_state.get("dominant") == "SELL":
            failure_score += 25
            reasons.append("di_flip_bearish")
        elif side == "SELL" and di_state.get("dominant") == "BUY":
            failure_score += 25
            reasons.append("di_flip_bullish")
        adx_state = ADXDIIntelligence.get_adx_state(market_state.get("df", None))
        if adx_state.get("state") == "CHOP" and adx_state.get("value") < 18:
            failure_score += 20
            reasons.append("adx_collapse")
        last_candle = market_state.get("last_candle", {})
        if side == "SELL" and last_candle.get("close", 0) > last_candle.get("open", 0):
            body = abs(last_candle.get("close",0)-last_candle.get("open",0))
            if body > market_state.get("atr", 0) * 0.6:
                failure_score += 20
                reasons.append("strong_bullish_candle")
        elif side == "BUY" and last_candle.get("close", 0) < last_candle.get("open", 0):
            body = abs(last_candle.get("close",0)-last_candle.get("open",0))
            if body > market_state.get("atr", 0) * 0.6:
                failure_score += 20
                reasons.append("strong_bearish_candle")
        continuation_pressure = market_state.get("continuation_pressure", 50)
        if continuation_pressure < 30:
            failure_score += 25
            reasons.append("low_continuation_pressure")
        sl_distance = abs(current_price - thesis.get("sl", entry_price)) / entry_price
        if sl_distance < 0.005:
            failure_score += 15
            reasons.append("sl_too_close")
        failed = failure_score >= 50
        return failed, reasons, failure_score

# ========== MARKET REGIME CLASSIFIER ==========
class MarketRegimeClassifier:
    @staticmethod
    def classify(df, ob=None):
        if df is None or len(df) < 50:
            return "UNKNOWN"
        adx_state = ADXDIIntelligence.get_adx_state(df)
        di_state = ADXDIIntelligence.get_di_state(df)
        atr = compute_atr(df).iloc[-1]
        price = df['close'].iloc[-1]
        atr_pct = (atr / price) * 100
        vol_state = classify_volume(df)
        range_20 = (df['high'].rolling(20).max() - df['low'].rolling(20).min()).iloc[-1]
        range_pct = (range_20 / price) * 100
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1]
        price_above_ema = price > ema20 and ema20 > ema50
        price_below_ema = price < ema20 and ema20 < ema50
        bos_up, bos_down = detect_bos(df, lookback=5)
        struct_shift = detect_structure_shift(df)
        if adx_state["state"] in ("STRONG_TREND", "VERY_STRONG") and di_state["dominant"] != "NEUTRAL":
            if (price_above_ema and di_state["dominant"] == "BUY") or (price_below_ema and di_state["dominant"] == "SELL"):
                if atr_pct > 2.0:
                    return "EXPANSION"
                else:
                    return "STRONG_TREND"
        if adx_state["state"] == "EMERGING" and adx_state["slope"] > 0:
            return "WEAK_TREND"
        if adx_state["value"] < 18 or di_state["dominant"] == "NEUTRAL":
            if range_pct < 1.5:
                return "COMPRESSION"
            else:
                return "CHOPPY"
        if vol_state == "expansion" and adx_state["value"] > 25:
            return "EXPANSION"
        if vol_state == "exhaustion" and adx_state["value"] > 30:
            return "DISTRIBUTION"
        if (struct_shift == "bullish_shift" and bos_up) or (struct_shift == "bearish_shift" and bos_down):
            return "TRANSITION"
        if vol_state == "absorption":
            return "ACCUMULATION"
        return "RANGE"

# ========== CONFIDENCE ENGINE ==========
class ConfidenceEngine:
    @staticmethod
    def calculate_initial_confidence(entry_score, narrative_score, regime, adx, di_spread, location_quality):
        base = (entry_score / 10) * 30 + (narrative_score / 10) * 30
        regime_map = {"STRONG_TREND": 20, "WEAK_TREND": 10, "EXPANSION": 25, "COMPRESSION": 5, "CHOPPY": 0, "ACCUMULATION": 15, "DISTRIBUTION": 10, "TRANSITION": 10}
        regime_bonus = regime_map.get(regime, 5)
        adx_bonus = min(20, max(0, (adx - 20) * 2))
        di_bonus = min(15, abs(di_spread))
        location_bonus = {"discount": 10, "premium": 10, "mid": 0}.get(location_quality, 0)
        total = base + regime_bonus + adx_bonus + di_bonus + location_bonus
        return min(100, total)

    @staticmethod
    def update_live_confidence(current_confidence, continuation_pressure, thesis_failure_score, adx_slope, di_spread_change):
        new_conf = current_confidence
        new_conf += (continuation_pressure - 50) * 0.3
        new_conf -= thesis_failure_score * 0.5
        new_conf += adx_slope * 2
        new_conf += di_spread_change * 1.5
        return max(0, min(100, new_conf))

    @staticmethod
    def apply_institutional_modifiers(base_confidence, smart_money, momentum, continuation_strength):
        conf = base_confidence
        if smart_money.get("smart_money_dominant", False):
            conf += 10
            log_execution("[CONF] Smart money dominant: +10", "INFO", debounce_key="conf_smart", debounce_sec=30)
        else:
            conf -= 8
        cont = min(100, max(0, continuation_strength))
        if cont > 20:
            conf += 8
        elif cont < 5:
            conf -= 10
        mom_health = momentum.get("momentum_health", 50)
        if mom_health > 15:
            conf += 6
        elif mom_health < 0:
            conf -= 8
        banker = smart_money.get("banker_pressure", 50)
        retail = smart_money.get("retailer_pressure", 50)
        if banker > retail:
            conf += 5
        else:
            conf -= 6
        dist = smart_money.get("distribution_risk", 0)
        if dist > 45:
            conf -= 12
        climax = momentum.get("climax_risk", 0)
        if climax > 50:
            conf -= 10
        conf = max(0, min(100, conf))
        return conf

# ========== PRECISION SAFETY ==========
class PrecisionSafety:
    @staticmethod
    def normalize_price(symbol, price):
        try:
            market = ex.market(normalize_symbol(symbol))
            prec = market['precision']['price']
            return round(price, prec)
        except:
            return price

    @staticmethod
    def normalize_amount(symbol, amount):
        try:
            market = ex.market(normalize_symbol(symbol))
            prec = market['precision']['amount']
            return math.floor(amount / (10 ** -prec)) * (10 ** -prec)
        except:
            return amount

    @staticmethod
    def adjust_sl_tp(symbol, entry, sl, tp, side, atr):
        min_dist = max(atr * 0.5, entry * 0.002)
        if side == "BUY":
            if entry - sl < min_dist:
                sl = entry - min_dist
            if tp - entry < min_dist:
                tp = entry + min_dist
        else:
            if sl - entry < min_dist:
                sl = entry + min_dist
            if entry - tp < min_dist:
                tp = entry - min_dist
        sl = PrecisionSafety.normalize_price(symbol, sl)
        tp = PrecisionSafety.normalize_price(symbol, tp)
        return sl, tp

# ========== CONTINUATION PROBABILITY ENGINE ==========
from dataclasses import dataclass

@dataclass
class ContinuationEvaluation:
    continuation_probability: float
    trend_strength: float
    exhaustion_probability: float
    reclaim_risk: float
    counter_pressure: float
    confidence: float
    reasons: List[str]
    should_hold: bool
    hold_quality: str

class ContinuationProbabilityEngine:
    HOLD_THRESHOLD = 0.62

    def evaluate(self, side: str, df, market_state: Dict, thesis: Dict) -> ContinuationEvaluation:
        score = 0.0
        reasons = []
        close = df["close"].iloc[-1]
        atr = market_state.get("atr", 0)
        adx = market_state.get("adx", 0)
        adx_slope = market_state.get("adx_slope", 0)
        di_plus = market_state.get("di_plus", 0)
        di_minus = market_state.get("di_minus", 0)
        trend_health = market_state.get("trend_health", 5)
        weak_pullback = market_state.get("weak_pullback", False)
        counter_displacement = market_state.get("counter_displacement", 0)
        volume_ratio = market_state.get("volume_ratio", 1.0)
        ema20 = df["close"].ewm(span=20).mean().iloc[-1]
        ema50 = df["close"].ewm(span=50).mean().iloc[-1]
        exhaustion_probability = 0.0
        reclaim_risk = 0.0
        counter_pressure = 0.0

        if side == "BUY":
            di_spread = di_plus - di_minus
        else:
            di_spread = di_minus - di_plus
        if di_spread > 8:
            score += 2.5
            reasons.append("strong_di_pressure")
        elif di_spread > 4:
            score += 1.5
            reasons.append("moderate_di_pressure")
        else:
            score -= 2.0
            reasons.append("weak_di_pressure")

        if adx > 25:
            score += 2.5
            reasons.append("healthy_adx")
        elif adx > 18:
            score += 1.0
            reasons.append("developing_adx")
        else:
            score -= 2.5
            reasons.append("dead_adx")
        if adx_slope > 0:
            score += 1.5
            reasons.append("adx_expanding")
        else:
            score -= 1.0
            reasons.append("adx_fading")

        if trend_health >= 8:
            score += 3.0
            reasons.append("excellent_trend_health")
        elif trend_health >= 6:
            score += 2.0
            reasons.append("healthy_trend")
        elif trend_health <= 3:
            score -= 3.0
            reasons.append("trend_breakdown")

        if weak_pullback:
            score += 2.0
            reasons.append("weak_pullback_detected")

        if counter_displacement > 1.5:
            counter_pressure += 0.5
            score -= 3.0
            reasons.append("strong_counter_pressure")
        elif counter_displacement > 0.8:
            counter_pressure += 0.25
            score -= 1.5
            reasons.append("moderate_counter_pressure")

        if side == "BUY":
            if close > ema20:
                score += 1.5
                reasons.append("holding_ema20")
            if close > ema50:
                score += 2.0
                reasons.append("holding_ema50")
            if close < ema20:
                reclaim_risk += 0.2
            if close < ema50:
                reclaim_risk += 0.4
        else:
            if close < ema20:
                score += 1.5
                reasons.append("holding_ema20")
            if close < ema50:
                score += 2.0
                reasons.append("holding_ema50")
            if close > ema20:
                reclaim_risk += 0.2
            if close > ema50:
                reclaim_risk += 0.4

        if atr > 0:
            extension = abs(close - ema20) / atr
            if extension > 3:
                exhaustion_probability += 0.5
                score -= 1.5
                reasons.append("overextended")
            elif extension > 2:
                exhaustion_probability += 0.25
                reasons.append("extended_move")

        if volume_ratio > 1.2:
            score += 1.5
            reasons.append("volume_confirmation")
        elif volume_ratio < 0.7:
            score -= 1.5
            reasons.append("weak_volume")

        if thesis is None:
            thesis = {}
        thesis_strength = thesis.get("thesis_strength", 5)
        score += thesis_strength * 0.3

        smart_money = SmartMoneyEngine.analyze_smart_money(df)
        momentum = MomentumFlowEngine.analyze_momentum_flow(df)

        adx_strength = (adx - 18) / 22 * 100 if adx > 18 else 0
        structure_aligned = 1 if market_state.get("structure_aligned", False) else 0
        continuation_strength = (
            momentum.get("momentum_health", 50) * 0.35 +
            smart_money.get("banker_pressure", 50) * 0.25 +
            adx_strength * 0.25 +
            structure_aligned * 15
        )
        continuation_strength = max(0, min(100, continuation_strength))
        market_state["continuation_strength_scaled"] = continuation_strength
        score += (continuation_strength - 50) / 10

        dominance_weight = 0.7 if smart_money["smart_money_dominant"] else 0.3
        score += (dominance_weight - 0.5) * 6

        if momentum["trend_expansion"]:
            score += 2.0
            reasons.append("momentum_expansion")
        if momentum["momentum_decay"]:
            score -= 2.5
            reasons.append("momentum_decay")
        dist_risk = smart_money["distribution_risk"] / 100.0
        score -= dist_risk * 3.0
        if dist_risk > 0.7:
            reasons.append("high_distribution_risk")
        if smart_money["retail_euphoria"]:
            score -= 1.5
            reasons.append("retail_euphoria")

        probability = (score + 15) / 30
        probability = max(0.0, min(1.0, probability))
        confidence = min(abs(score) / 15, 1.0)
        should_hold = probability >= self.HOLD_THRESHOLD
        if probability >= 0.8:
            hold_quality = "STRONG"
        elif probability >= 0.65:
            hold_quality = "HEALTHY"
        elif probability >= 0.5:
            hold_quality = "NEUTRAL"
        else:
            hold_quality = "WEAK"

        return ContinuationEvaluation(
            continuation_probability=round(probability, 2),
            trend_strength=round(trend_health / 10, 2),
            exhaustion_probability=round(exhaustion_probability, 2),
            reclaim_risk=round(reclaim_risk, 2),
            counter_pressure=round(counter_pressure, 2),
            confidence=round(confidence, 2),
            reasons=reasons,
            should_hold=should_hold,
            hold_quality=hold_quality
        )

_continuation_engine = ContinuationProbabilityEngine()

# ========== LIVE TRADE MANAGEMENT SYSTEM (FIXED) ==========
class TradeLifecycleState(Enum):
    IDLE = "IDLE"
    OPEN_REQUESTED = "OPEN_REQUESTED"
    OPEN_PENDING_CONFIRMATION = "OPEN_PENDING_CONFIRMATION"
    LIVE = "LIVE"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    RECOVERING = "RECOVERING"
    ERROR_DEGRADED = "ERROR_DEGRADED"

class PositionSnapshot:
    def __init__(self):
        self.symbol = None
        self.side = None
        self.qty = 0.0
        self.entry_price = 0.0
        self.mark_price = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.roe_pct = 0.0
        self.leverage = LEVERAGE
        self.margin = 0.0
        self.liquidation_price = 0.0
        self.tp1_hit = False
        self.tp2_hit = False
        self.trailing_active = False
        self.trailing_stop = 0.0
        self.sl_price = 0.0
        self.partial_closed = False
        self.stale = False
        self.updated_at = 0.0
        self.source = "unknown"

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "roe_pct": self.roe_pct,
            "leverage": self.leverage,
            "margin": self.margin,
            "liquidation_price": self.liquidation_price,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "trailing_active": self.trailing_active,
            "trailing_stop": self.trailing_stop,
            "sl_price": self.sl_price,
            "partial_closed": self.partial_closed,
            "stale": self.stale,
            "updated_at": self.updated_at,
            "source": self.source
        }

class EventBus:
    def __init__(self):
        self._handlers = {}
        self._queue = qlib.Queue()
        self._running = True
        self._thread = threading.Thread(
            target=self._process, daemon=True, name="event_bus_worker"
        )
        self._thread.start()

    def stop(self, join_timeout=2.0):
        self._running = False
        try:
            self._queue.put_nowait(("__shutdown__", None))
        except Exception:
            pass
        try:
            if self._thread.is_alive() and self._thread is not threading.current_thread():
                self._thread.join(timeout=max(0.0, float(join_timeout)))
        except Exception:
            pass

    def __del__(self):
        # Best-effort only; explicit request_shutdown/atexit is authoritative.
        try:
            self._running = False
        except Exception:
            pass

    def subscribe(self, event_type, handler):
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def emit(self, event_type, data=None):
        self._queue.put((event_type, data))

    def _process(self):
        while self._running and not shutdown_requested():
            try:
                event_type, data = self._queue.get(timeout=0.1)
                if event_type == "__shutdown__":
                    break
                for handler in self._handlers.get(event_type, []):
                    try:
                        handler(data)
                    except Exception as e:
                        log_execution(f"[EVENT] handler error: {e}", "ERROR")
            except qlib.Empty:
                continue
            except Exception:
                continue

class ExchangeSyncService:
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self._last_snapshot = PositionSnapshot()
        self._reconcile_count = 0
        self._last_reconcile = 0

    def fetch_all_open_positions(self):
        """All open positions across symbols (recovery source for PortfolioManager)."""
        if PAPER_MODE:
            pos = paper.get("position") if isinstance(paper, dict) else None
            if not pos:
                return []
            return [{
                "symbol": pos.get("symbol"),
                "side": pos.get("side", "BUY"),
                "entryPrice": float(pos.get("entry", 0.0) or 0.0),
                "contracts": float(pos.get("qty", 0.0) or 0.0),
                "markPrice": float(pos.get("mark_price", pos.get("entry", 0.0)) or 0.0),
            }]
        positions = safe_api_call(ex.fetch_positions)
        if not positions:
            return []
        out = []
        for p in positions:
            try:
                contracts = abs(float(p.get("contracts") or 0.0))
            except (TypeError, ValueError):
                continue
            if contracts <= 0:
                continue
            side = str(p.get("side") or "").lower()
            out.append({
                "symbol": p.get("symbol"),
                "side": "BUY" if side == "long" else "SELL",
                "entryPrice": float(p.get("entryPrice") or 0.0),
                "contracts": contracts,
                "markPrice": float(p.get("markPrice") or 0.0),
            })
        return out

    def fetch_live_snapshot(self, symbol):
        if PAPER_MODE:
            return self._paper_snapshot(symbol)
        try:
            pos, pos_status = fetch_position_status(symbol)
            if pos is None:
                # CRITICAL: PAUSED/ERROR means the exchange could not answer the
                # symbol query. It is never evidence that the position vanished.
                if pos_status in ("PAUSED", "ERROR"):
                    # Preserve the last known position snapshot. If this is the
                    # first sync, synthesize a stale snapshot from local state so
                    # management can continue safely until the venue recovers.
                    if self._last_snapshot.symbol != symbol or self._last_snapshot.qty <= 0:
                        stale = PositionSnapshot()
                        stale.symbol = symbol
                        stale.side = STATE.get("side", "BUY")
                        stale.qty = safe_float(STATE.get("remaining_qty", STATE.get("qty", 0)))
                        stale.entry_price = safe_float(STATE.get("entry", 0))
                        stale.mark_price = safe_float(STATE.get("mark_price", STATE.get("entry", 0)))
                        stale.unrealized_pnl = safe_float(STATE.get("unrealized_pnl_usdt", 0))
                        stale.margin = safe_float(STATE.get("margin", 0))
                        stale.leverage = safe_float(STATE.get("leverage", LEVERAGE))
                        stale.liquidation_price = safe_float(STATE.get("liquidation_price", 0))
                        stale.roe_pct = safe_float(STATE.get("roe_pct", 0))
                        stale.source = f"local_stale_{pos_status.lower()}"
                        stale.stale = True
                        stale.updated_at = time.time()
                        return stale if stale.qty > 0 else None
                    self._last_snapshot.stale = True
                    self._last_snapshot.updated_at = time.time()
                    self._last_snapshot.source = f"rest_sync_{pos_status.lower()}"
                    return self._last_snapshot
                if STATE.get("open"):
                    self.event_bus.emit("position_closed_external", {"symbol": symbol})
                return None
            snapshot = PositionSnapshot()
            snapshot.symbol = symbol
            snapshot.side = 'BUY' if pos.get('side', '').lower() == 'long' else 'SELL'
            snapshot.qty = safe_float(pos.get('contracts', 0))
            snapshot.entry_price = safe_float(pos.get('entryPrice', 0))
            snapshot.mark_price = safe_float(pos.get('markPrice', 0))
            snapshot.unrealized_pnl = safe_float(pos.get('unrealizedPnl', 0))
            snapshot.margin = safe_float(pos.get('initialMargin', 0))
            snapshot.leverage = safe_float(pos.get('leverage', LEVERAGE))
            snapshot.liquidation_price = safe_float(pos.get('liquidationPrice', 0))
            if snapshot.margin > 0:
                snapshot.roe_pct = (snapshot.unrealized_pnl / snapshot.margin) * 100
            else:
                raw_move = (snapshot.mark_price - snapshot.entry_price)/snapshot.entry_price*100 if snapshot.side=="BUY" else (snapshot.entry_price - snapshot.mark_price)/snapshot.entry_price*100
                snapshot.roe_pct = raw_move * snapshot.leverage
            snapshot.updated_at = time.time()
            snapshot.source = "rest_sync"
            self._last_snapshot = snapshot
            return snapshot
        except Exception as e:
            log_execution(f"[SYNC] REST snapshot error: {e}", "ERROR")
            return None

    def _paper_snapshot(self, symbol):
        if not STATE.get("open") or STATE.get("current_symbol") != symbol:
            return None
        snap = PositionSnapshot()
        snap.symbol = symbol
        snap.side = STATE["side"]
        snap.qty = STATE["qty"]
        snap.entry_price = STATE["entry"]
        snap.mark_price = get_ticker_safe(symbol) or snap.entry_price
        snap.unrealized_pnl = (snap.mark_price - snap.entry_price) * snap.qty if snap.side == "BUY" else (snap.entry_price - snap.mark_price) * snap.qty
        snap.margin = snap.entry_price * snap.qty / LEVERAGE
        snap.roe_pct = (snap.unrealized_pnl / snap.margin) * 100 if snap.margin else 0
        snap.updated_at = time.time()
        snap.source = "paper"
        return snap

    def reconcile(self, symbol, local_state):
        now = time.time()
        if now - self._last_reconcile < 10:
            return
        self._last_reconcile = now
        log_execution(f"[RECONCILIATION] Starting for {symbol}", "INFO")
        self._reconcile_count += 1
        snap = self.fetch_live_snapshot(symbol)
        if snap is None:
            if SYMBOL_GUARD.is_paused(symbol):
                log_execution(f"[RECONCILIATION] {symbol} venue-paused; keeping local position state intact", "WARN", debounce_key=f"reconcile_paused_{symbol}", debounce_sec=60)
                return
            if local_state.get("open"):
                log_execution(f"[RECONCILIATION] Position vanished, marking closed", "WARN")
                self.event_bus.emit("force_close_local")
            return
        with _TRADE_LOCK:
            STATE["entry"] = snap.entry_price
            STATE["qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["side"] = snap.side
            STATE["mark_price"] = snap.mark_price
            STATE["unrealized_pnl_usdt"] = snap.unrealized_pnl
            STATE["roe_pct"] = snap.roe_pct
            STATE["margin"] = snap.margin
            STATE["liquidation_price"] = snap.liquidation_price
            TRADE_STATE.update({
                "symbol": symbol,
                "side": snap.side,
                "entry": snap.entry_price,
                "qty": snap.qty,
                "last_update_ts": time.time()
            })
            if not STATE.get("open"):
                STATE["open"] = True
                STATE["current_symbol"] = symbol
                STATE["entry_time"] = time.time()
        log_execution(f"[RECONCILIATION] Completed: ROE={snap.roe_pct:.2f}%, Qty={snap.qty}", "SUCCESS")
        self.event_bus.emit("reconciled", snap)

class RecoveryGuard:
    def __init__(self, event_bus, exchange_sync):
        self.event_bus = event_bus
        self.exchange_sync = exchange_sync
        self.recovery_attempts = 0
        self.last_recovery = 0

    def check_and_recover(self, symbol):
        now = time.time()
        if self.recovery_attempts > 5 and now - self.last_recovery < 300:
            log_execution("[RECOVERY] Too many attempts, cooling down", "WARN")
            return False
        log_execution(f"[RECOVERY] Entering RECOVERING mode for {symbol}", "WARN")
        self.event_bus.emit("lifecycle_change", TradeLifecycleState.RECOVERING)
        success = False
        for attempt in range(3):
            try:
                snap = self.exchange_sync.fetch_live_snapshot(symbol)
                if snap is not None:
                    self.recovery_attempts = 0
                    self.last_recovery = now
                    self.event_bus.emit("recovery_success", snap)
                    success = True
                    break
                time.sleep(1)
            except:
                continue
        if not success:
            log_execution("[RECOVERY] Failed to recover, staying in degraded", "ERROR")
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.ERROR_DEGRADED)
            self.recovery_attempts += 1
            self.last_recovery = now
        return success

# ========== INSTITUTIONAL ENGINES (UNCHANGED) ==========
class SmartMoneyEngine:
    @staticmethod
    def _rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def analyze_smart_money(df):
        if df is None or df.empty:
            return SmartMoneyEngine._default_state()
        required = ["close", "high", "low", "volume"]
        if not all(c in df.columns for c in required):
            return SmartMoneyEngine._default_state()
        if len(df) < 20:
            return SmartMoneyEngine._default_state()
        close = df["close"]
        volume = df["volume"]
        rsi = SmartMoneyEngine._rsi(close, 14)
        vol_ma = volume.rolling(20).mean()
        volume_impulse = volume / vol_ma.replace(0, np.nan)
        vwma = (close * volume).rolling(20).sum() / volume.rolling(20).sum()
        price_distance = ((close - vwma) / vwma.replace(0, np.nan)) * 100
        momentum = close.pct_change(5) * 100
        banker_pressure = (rsi * 0.35) + (volume_impulse * 15) + (momentum * 2) + (price_distance * 1.5)
        banker_pressure = banker_pressure.clip(0, 100)
        retailer_pressure = (100 - banker_pressure).clip(0, 100)
        hot_money_pressure = (abs(momentum) * 5).clip(0, 100)
        smart_money_dominant = (
            banker_pressure.iloc[-1] > 52 and
            banker_pressure.iloc[-1] > retailer_pressure.iloc[-1] + 6
        )
        retail_euphoria = retailer_pressure.iloc[-1] > 75
        distribution_risk = max(0, retailer_pressure.iloc[-1] - banker_pressure.iloc[-1])
        accumulation_strength = banker_pressure.iloc[-1]
        if banker_pressure.iloc[-1] > 60:
            institutional_bias = "BUY"
        elif retailer_pressure.iloc[-1] > 70:
            institutional_bias = "SELL"
        else:
            institutional_bias = "NEUTRAL"
        delta = banker_pressure.iloc[-1] - retailer_pressure.iloc[-1]
        if delta >= 30:
            institutional_bias_detailed = "STRONG_BUY"
        elif delta >= 12:
            institutional_bias_detailed = "BUY"
        elif delta >= 5:
            institutional_bias_detailed = "WEAK_BUY"
        elif delta <= -30:
            institutional_bias_detailed = "STRONG_SELL"
        elif delta <= -12:
            institutional_bias_detailed = "SELL"
        elif delta <= -5:
            institutional_bias_detailed = "WEAK_SELL"
        else:
            institutional_bias_detailed = "NEUTRAL"
        trend_quality = banker_pressure.iloc[-1] - retailer_pressure.iloc[-1]
        flow_alignment = (banker_pressure.iloc[-1] / (retailer_pressure.iloc[-1] + 1)) * 50

        def safe_float_val(v):
            if pd.isna(v) or np.isinf(v):
                return 0.0
            return float(v)

        return {
            "banker_pressure": safe_float_val(banker_pressure.iloc[-1]),
            "retailer_pressure": safe_float_val(retailer_pressure.iloc[-1]),
            "hot_money_pressure": safe_float_val(hot_money_pressure.iloc[-1]),
            "smart_money_dominant": bool(smart_money_dominant),
            "retail_euphoria": bool(retail_euphoria),
            "distribution_risk": safe_float_val(distribution_risk),
            "accumulation_strength": safe_float_val(accumulation_strength),
            "institutional_bias": institutional_bias,
            "institutional_bias_detailed": institutional_bias_detailed,
            "trend_quality": safe_float_val(trend_quality),
            "flow_alignment": safe_float_val(flow_alignment)
        }

    @staticmethod
    def _default_state():
        return {
            "banker_pressure": 50.0,
            "retailer_pressure": 50.0,
            "hot_money_pressure": 50.0,
            "smart_money_dominant": False,
            "retail_euphoria": False,
            "distribution_risk": 0.0,
            "accumulation_strength": 0.0,
            "institutional_bias": "NEUTRAL",
            "institutional_bias_detailed": "NEUTRAL",
            "trend_quality": 0.0,
            "flow_alignment": 25.0
        }


class MomentumFlowEngine:
    @staticmethod
    def _rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def analyze_momentum_flow(df):
        if df is None or df.empty:
            return MomentumFlowEngine._default_state()
        required = ["close", "high", "low", "volume"]
        if not all(c in df.columns for c in required):
            return MomentumFlowEngine._default_state()
        if len(df) < 20:
            return MomentumFlowEngine._default_state()
        close = df["close"]
        rsi = MomentumFlowEngine._rsi(close, 14)
        ema_fast = close.ewm(span=9).mean()
        ema_slow = close.ewm(span=21).mean()
        momentum_spread = ((ema_fast - ema_slow) / ema_slow.replace(0, np.nan)) * 100
        norm_spread = max(-2.5, min(2.5, momentum_spread.iloc[-1])) / 2.5
        momentum_health = 50 + (norm_spread * 50)
        continuation_strength = min(100, max(0, abs(momentum_spread.iloc[-1]) * 20))
        trend_expansion = momentum_spread.iloc[-1] > 0.35
        momentum_decay = momentum_spread.iloc[-1] < 0.05
        climax_risk = max(0, rsi.iloc[-1] - 70) * 3
        exhaustion_risk = max(0, 40 - momentum_health)
        greed_state = (rsi.iloc[-1] > 75) and trend_expansion
        if momentum_spread.iloc[-1] > 0:
            flow_bias = "BUY"
        elif momentum_spread.iloc[-1] < 0:
            flow_bias = "SELL"
        else:
            flow_bias = "NEUTRAL"

        def safe_float_val(v):
            if pd.isna(v) or np.isinf(v):
                return 0.0
            return float(v)

        return {
            "continuation_strength": safe_float_val(continuation_strength),
            "momentum_health": safe_float_val(momentum_health),
            "trend_expansion": bool(trend_expansion),
            "momentum_decay": bool(momentum_decay),
            "climax_risk": safe_float_val(climax_risk),
            "exhaustion_risk": safe_float_val(exhaustion_risk),
            "greed_state": bool(greed_state),
            "flow_bias": flow_bias
        }

    @staticmethod
    def _default_state():
        return {
            "continuation_strength": 0.0,
            "momentum_health": 50.0,
            "trend_expansion": False,
            "momentum_decay": False,
            "climax_risk": 0.0,
            "exhaustion_risk": 0.0,
            "greed_state": False,
            "flow_bias": "NEUTRAL"
        }


# ============================================================
# NEW: INSTITUTIONAL INTENT ENGINE (9 LAYERS)
# ============================================================
class InstitutionalIntentEngine:
    """
    9‑layer pre‑filter to detect early institutional accumulation/distribution.
    Returns: score (0-100), status ('ACCUMULATION'/'DISTRIBUTION'/'NEUTRAL'), details dict.
    """
    @staticmethod
    def detect(df, ob=None, symbol=None):
        if df is None or len(df) < 30:
            return 0, "NEUTRAL", {}

        details = {}
        score = 0

        # ----- Layer 1: Liquidity Heatmap -----
        price = df['close'].iloc[-1]
        pools = build_liquidity_pools(df)
        eq_high, eq_low = detect_equal_highs_lows(df, lookback=30)
        liq_score = 0
        if pools.get("high_pools") or pools.get("low_pools"):
            liq_score += 25
        if eq_high or eq_low:
            liq_score += 15
        recent_high = df['high'].iloc[-10:].max()
        recent_low = df['low'].iloc[-10:].min()
        if abs(price - recent_high) / price < 0.002:
            liq_score += 10
        if abs(price - recent_low) / price < 0.002:
            liq_score += 10
        liq_score = min(100, liq_score)
        details['liquidity_score'] = liq_score
        score += liq_score * 0.12

        # ----- Layer 2: Advanced Absorption (multi‑candle) -----
        absorption_score, is_abs = InstitutionalIntentEngine._detect_absorption_sequence(df)
        details['absorption_score'] = absorption_score
        details['absorption'] = is_abs
        score += absorption_score * 0.15

        # ----- Layer 3: Volatility Compression -----
        atr = compute_atr(df)
        atr_current = atr.iloc[-1]
        atr_ma = atr.rolling(20).mean().iloc[-1] if len(atr) >= 20 else atr_current
        atr_ratio = atr_current / atr_ma if atr_ma > 0 else 1.0
        bb_std = df['close'].rolling(20).std().iloc[-1]
        bb_mid = df['close'].rolling(20).mean().iloc[-1]
        bb_width = (2 * bb_std) / bb_mid if bb_mid > 0 else 0.01
        compression = (atr_ratio < 0.8) and (bb_width < 0.05)
        vol_score = 85 if compression else (65 if atr_ratio < 0.9 else 40)
        details['volatility_score'] = vol_score
        details['atr_ratio'] = round(atr_ratio, 2)
        details['bb_width'] = round(bb_width * 100, 2)
        score += vol_score * 0.10

        # ----- Layer 4: Institutional Flow (Smart Money) -----
        smart = SmartMoneyEngine.analyze_smart_money(df)
        banker = smart.get("banker_pressure", 50)
        retail = smart.get("retailer_pressure", 50)
        acc = smart.get("accumulation_strength", 0)
        dist = smart.get("distribution_risk", 0)
        pressure_diff = banker - retail
        if pressure_diff > 15:
            flow_score = 80
            status_candidate = "ACCUMULATION"
        elif pressure_diff < -15:
            flow_score = 80
            status_candidate = "DISTRIBUTION"
        else:
            flow_score = 50 + pressure_diff * 1.5
            status_candidate = "NEUTRAL"
        if acc > 60:
            flow_score = min(100, flow_score + 15)
        if dist > 50:
            flow_score = max(0, flow_score - 20)
        details['flow_score'] = flow_score
        details['banker_pressure'] = round(banker, 1)
        details['retail_pressure'] = round(retail, 1)
        score += flow_score * 0.15

        # ----- Layer 5: Fractal Structure Shift -----
        struct_type, struct_score = InstitutionalIntentEngine._detect_fractal_structure(df)
        details['structure_type'] = struct_type
        details['structure_score'] = struct_score
        score += struct_score * 0.12

        # ----- Layer 6: Momentum Ignition -----
        adx_series = compute_adx(df)
        adx_current = adx_series.iloc[-1] if len(adx_series) > 0 else 20
        adx_prev = adx_series.iloc[-2] if len(adx_series) > 1 else adx_current
        adx_slope = adx_current - adx_prev
        mom = MomentumFlowEngine.analyze_momentum_flow(df)
        mom_health = mom.get("momentum_health", 50)
        expansion = mom.get("trend_expansion", False)
        if adx_slope > 0 and expansion and mom_health > 55:
            mom_score = 85
        elif adx_slope > 0 and mom_health > 50:
            mom_score = 70
        elif mom_health > 60:
            mom_score = 60
        else:
            mom_score = 40
        details['momentum_score'] = mom_score
        details['adx_slope'] = round(adx_slope, 2)
        score += mom_score * 0.15

        # ----- Layer 7: Volume Context -----
        vol = df['volume']
        vol_ma = vol.rolling(20).mean().iloc[-1]
        vol_ratio = vol.iloc[-1] / vol_ma if vol_ma > 0 else 1.0
        vol_accel = vol.iloc[-5:].mean() / (vol.iloc[-10:-5].mean() + 1e-9)
        vol_score = 0
        if vol_ratio > 1.5 and vol_accel > 1.2:
            vol_score = 85
        elif vol_ratio > 1.2:
            vol_score = 65
        elif vol_ratio < 0.7:
            vol_score = 20
        else:
            vol_score = 50
        details['volume_score'] = vol_score
        details['vol_ratio'] = round(vol_ratio, 2)
        details['vol_accel'] = round(vol_accel, 2)
        score += vol_score * 0.08

        # ----- Layer 8: Institutional Narrative (the "Why") -----
        narrative, narrative_score = InstitutionalIntentEngine._institutional_narrative(df, smart, pools, struct_type, price)
        details['narrative'] = narrative
        details['narrative_score'] = narrative_score
        score += narrative_score * 0.13

        # ----- Layer 9: Dynamic Probability Engine (regime‑adaptive weights) -----
        regime_input = MEMORY.get("regime", "RANGE")
        regime_map = {"CHOP": "RANGE", "RANGE": "RANGE", "TREND": "TREND", "NEWS": "NEWS"}
        regime_tag = regime_map.get(str(regime_input).upper(), "NEUTRAL")
        weights = InstitutionalIntentEngine._get_regime_weights(regime_tag)
        final_score = (
            liq_score * weights['liquidity'] +
            absorption_score * weights['absorption'] +
            vol_score * weights['volatility'] +
            flow_score * weights['institutional_flow'] +
            struct_score * weights['structure'] +
            mom_score * weights['momentum'] +
            vol_score * weights['volume'] +
            narrative_score * weights['narrative']
        ) / 100
        final_score = max(0, min(100, final_score))

        # Determine overall status
        if final_score >= 70:
            if pressure_diff > 10 or (acc > 50 and dist < 30):
                status = "ACCUMULATION"
            elif pressure_diff < -10 or (dist > 50 and acc < 30):
                status = "DISTRIBUTION"
            else:
                status = "NEUTRAL"
        else:
            status = "NEUTRAL"

        details['regime_weights'] = weights
        details['regime'] = regime_tag
        details['regime_input'] = regime_input
        return round(final_score, 2), status, details

    @staticmethod
    def _detect_absorption_sequence(df, window=4):
        if len(df) < window:
            return 0, False
        last_n = df.iloc[-window:]
        vol_avg = last_n['volume'].mean()
        overall_avg = df['volume'].iloc[-20:].mean()
        vol_ratio = vol_avg / overall_avg if overall_avg > 0 else 1.0
        body_range_ratios = []
        for i in range(window):
            candle = last_n.iloc[i]
            body = abs(candle['close'] - candle['open'])
            range_ = candle['high'] - candle['low']
            body_range_ratios.append(body / range_ if range_ > 0 else 1.0)
        avg_br_ratio = sum(body_range_ratios) / window
        is_absorption = vol_ratio > 1.2 and avg_br_ratio < 0.35
        wick_score = 0
        for i in range(window):
            candle = last_n.iloc[i]
            upper_wick = candle['high'] - max(candle['open'], candle['close'])
            lower_wick = min(candle['open'], candle['close']) - candle['low']
            if upper_wick > (candle['high'] - candle['low']) * 0.4:
                wick_score += 1
            if lower_wick > (candle['high'] - candle['low']) * 0.4:
                wick_score += 1
        wick_absorption = wick_score >= window * 0.75
        if is_absorption and wick_absorption:
            return 80, True
        elif is_absorption:
            return 60, True
        else:
            return max(0, 50 - (vol_ratio - 1) * 30), False

    @staticmethod
    def _detect_fractal_structure(df):
        if len(df) < 30:
            return "NONE", 0
        internal_high = df['high'].iloc[-6:-1].max()
        internal_low = df['low'].iloc[-6:-1].min()
        curr_close = df['close'].iloc[-1]
        internal_break_up = curr_close > internal_high
        internal_break_down = curr_close < internal_low
        external_high = df['high'].iloc[-21:-1].max()
        external_low = df['low'].iloc[-21:-1].min()
        external_break_up = curr_close > external_high
        external_break_down = curr_close < external_low
        if external_break_up or external_break_down:
            return "EXTERNAL", 90
        elif internal_break_up or internal_break_down:
            return "INTERNAL", 60
        else:
            return "NONE", 30

    @staticmethod
    def _institutional_narrative(df, smart, pools, struct_type, price):
        narrative = []
        confidence = 0
        side = "BUY" if smart.get("institutional_bias") == "BUY" else "SELL"
        if side == "BUY" and pools.get("low_pools") and price < pools["low_pools"][0]:
            narrative.append("Sweep of major low")
            confidence += 20
        if side == "SELL" and pools.get("high_pools") and price > pools["high_pools"][0]:
            narrative.append("Sweep of major high")
            confidence += 20
        if smart.get("accumulation_strength", 0) > 60:
            narrative.append("Accumulation strength")
            confidence += 15
        if struct_type in ("INTERNAL", "EXTERNAL"):
            narrative.append(f"{struct_type} structure break")
            confidence += 15
        supports, resistances = get_clustered_zones(df, lookback=60)
        if side == "BUY" and resistances:
            next_res = min([r for r in resistances if r > price], default=price*2)
            if (next_res - price) / price > 0.03:
                narrative.append("Room to run")
                confidence += 10
        if side == "SELL" and supports:
            next_sup = max([s for s in supports if s < price], default=price*0.5)
            if (price - next_sup) / price > 0.03:
                narrative.append("Room to run")
                confidence += 10
        return " | ".join(narrative) if narrative else "NEUTRAL", min(100, confidence)

    @staticmethod
    def _get_regime_weights(regime):
        if regime == "RANGE":
            return {
                'liquidity': 20, 'absorption': 18, 'volatility': 10,
                'institutional_flow': 15, 'structure': 12, 'momentum': 5,
                'volume': 8, 'narrative': 12
            }
        elif regime == "TREND":
            return {
                'liquidity': 12, 'absorption': 12, 'volatility': 10,
                'institutional_flow': 18, 'structure': 15, 'momentum': 18,
                'volume': 8, 'narrative': 7
            }
        elif regime == "NEWS":
            return {
                'liquidity': 10, 'absorption': 15, 'volatility': 5,
                'institutional_flow': 20, 'structure': 10, 'momentum': 10,
                'volume': 20, 'narrative': 10
            }
        else:
            return {k: 12.5 for k in ['liquidity','absorption','volatility','institutional_flow','structure','momentum','volume','narrative']}


# ============================================================
# NEW: DYNAMIC TRADE MANAGER
# ============================================================
class DynamicTradeManager:
    """
    Manages an active trade with adaptive SL, multi-stage TP, runner mode,
    and institutional-based exit decisions.
    """
    def __init__(self, symbol, side, entry, qty, atr, initial_sl, tp1, tp2):
        self.symbol = symbol
        self.side = side
        self.entry = entry
        self.qty = qty
        self.atr = atr
        self.sl = initial_sl
        self.tp1 = tp1
        self.tp2 = tp2
        self.tp1_hit = False
        self.tp2_hit = False
        self.trailing_activated = False
        self.trailing_stop = 0.0
        self.runner_active = False
        self.peak_price = entry
        self.peak_roe = 0.0
        self.drawdown = 0.0
        self.lifecycle = "LIVE"
        self.last_update = time.time()
        self.smart_exit_triggered = False
        self.partial_closed = False

    def update(self, current_price, df, ob, atr):
        self.last_update = time.time()
        roe = self.calculate_roe(current_price)
        self.peak_price = max(self.peak_price, current_price) if self.side == "BUY" else min(self.peak_price, current_price)
        self.peak_roe = max(self.peak_roe, roe)
        self.drawdown = max(0, (self.peak_roe - roe) if self.peak_roe > 0 else 0)

        # ---- 1. Dynamic Stop Loss ----
        if roe > 0.4 and not self.trailing_activated:
            self.trailing_activated = True
            self.trailing_stop = current_price - atr * 0.8 if self.side == "BUY" else current_price + atr * 0.8
            log_execution(f"[DYN_SL] Trailing activated for {self.symbol} at ROE={roe:.2f}%", "INFO")

        if self.trailing_activated:
            if self.side == "BUY":
                new_stop = current_price - atr * 1.2
                if new_stop > self.trailing_stop:
                    self.trailing_stop = new_stop
            else:
                new_stop = current_price + atr * 1.2
                if new_stop < self.trailing_stop:
                    self.trailing_stop = new_stop
            if (self.side == "BUY" and current_price <= self.trailing_stop) or \
               (self.side == "SELL" and current_price >= self.trailing_stop):
                self.lifecycle = "SL_HIT"
                log_execution(f"[DYN_SL] Stop hit at {current_price:.4f}", "WARN")
                return "EXIT"

        # ---- 2. Multi-Stage Take Profit ----
        if not self.tp1_hit:
            if (self.side == "BUY" and current_price >= self.tp1) or (self.side == "SELL" and current_price <= self.tp1):
                self.tp1_hit = True
                log_execution(f"[TP1] Hit for {self.symbol} at {current_price:.4f}", "SUCCESS")
                return "PARTIAL"
        if self.tp1_hit and not self.tp2_hit:
            if (self.side == "BUY" and current_price >= self.tp2) or (self.side == "SELL" and current_price <= self.tp2):
                self.tp2_hit = True
                self.runner_active = True
                log_execution(f"[TP2] Hit for {self.symbol} at {current_price:.4f}", "SUCCESS")
                return "TP2"

        # ---- 3. Runner Management ----
        if self.tp1_hit and self.runner_active:
            if self.drawdown > 3.0:
                log_execution(f"[RUNNER] Drawdown {self.drawdown:.1f}% – tightening", "WARN")
                if self.side == "BUY":
                    self.trailing_stop = max(self.trailing_stop, current_price - atr * 0.6)
                else:
                    self.trailing_stop = min(self.trailing_stop, current_price + atr * 0.6)

        # ---- 4. Institutional Exit Signals ----
        if self.tp1_hit:
            smart = SmartMoneyEngine.analyze_smart_money(df)
            mom = MomentumFlowEngine.analyze_momentum_flow(df)
            if smart.get("distribution_risk", 0) > 60 and mom.get("momentum_decay", False):
                self.lifecycle = "INSTITUTIONAL_EXIT"
                log_execution(f"[INST_EXIT] Distribution risk {smart['distribution_risk']:.1f}, momentum decay", "WARN")
                return "EXIT"

        # ---- 5. Structure Loss Check ----
        struct_type, _ = InstitutionalIntentEngine._detect_fractal_structure(df)
        if struct_type == "EXTERNAL" and self.side == "BUY" and df['close'].iloc[-1] < df['close'].iloc[-3]:
            return "EXIT"
        if struct_type == "EXTERNAL" and self.side == "SELL" and df['close'].iloc[-1] > df['close'].iloc[-3]:
            return "EXIT"

        return "HOLD"

    def calculate_roe(self, price):
        if self.side == "BUY":
            return ((price - self.entry) / self.entry) * 100
        else:
            return ((self.entry - price) / self.entry) * 100


# ============================================================
# NEW: WATCHLIST PRIORITY MANAGER
# ============================================================
class WatchlistPriorityManager:
    @staticmethod
    def update_priorities():
        """
        Scans watchlist and assigns higher priority to symbols approaching
        institutional conditions. Prevents premature removal.
        """
        now = time.time()
        watchlist = MEMORY.get("watchlist", {})
        for sym, entry in list(watchlist.items()):
            if now - entry.get("last_update", 0) > 3600:
                continue
            df = get_ohlcv_safe(sym, 100)
            if df is None:
                continue
            intent_score, status, details = InstitutionalIntentEngine.detect(df, None, sym)
            if intent_score > 60:
                entry["priority"] = intent_score
                entry["intent_status"] = status
                entry["priority_until"] = now + 7200
                log_execution(f"[PRIORITY] {sym} priority boosted to {intent_score:.1f}", "INFO")
        sorted_watch = sorted(watchlist.items(), key=lambda x: x[1].get("priority", 0), reverse=True)
        MEMORY["watchlist"] = dict(sorted_watch)


# =============================================================================
# POSITION MANAGEMENT ENGINE (PHASE 1 - ADVISORY/CLASSIFICATION LAYER)
#
# These classes build on the ALREADY-LIVE systems (_apply_management,
# TradeStateMachine, ThesisFailureEngine, ContinuationProbabilityEngine,
# SmartMoney/Momentum engines, close_partial/close_position_full) rather than
# replacing them. They add:
#   1. DynamicPositionProfile  -> dynamic trade_type (TREND/REVERSAL/BREAKOUT/)
#                                 PULLBACK/RETEST) + asset_class captured at
#                                 OPEN and re-evaluated through the trade life.
#   2. AssetBehaviorProfile    -> per asset-class sensitivity params used to
#                                 adapt profit-taking, trailing and SL behaviour.
#   3. PositionHealthScore     -> unified 0-100 score combining the parallel
#                                 channels (trend, structure, liquidity, momentum,
#                                 institutional, zone, risk) + advisory ACTION.
#
# PHASE 1 IS ADVISORY-ONLY: it STORES the profile/health and LOGS [POSITION]
# decisions but does NOT change entry/exit behaviour. Phase 2 will turn these
# advisory signals into enforceable rules (profit-taking / trailing / close).
# =============================================================================


# -----------------------------------------------------------------------------
# AssetBehaviorProfile: per asset-class sensitivity parameters
# -----------------------------------------------------------------------------
class AssetBehaviorProfile:
    """
    Adapts profit-taking / trailing / SL / runner behaviour per asset class:
      - CRYPTO / STOCK / INDEX: WIDE breakouts -> allow more room, favour runner.
      - GOLD / OIL: MEAN-REVERTING intraday -> take profit sooner, trailing tighter.
    All values are adjustable via env vars with safe defaults that preserve the
    previous hardcoded behaviour (CRYPTO default matches legacy 1.6 SL, 1.5% ROE
    trail activation, 4%/5% TP1/TP2).
    """

    # XATR enthusiasts: TP distances are expressed in ATR multiples from entry.
    DEFAULT = {
        "CRYPTO": {"tp1_atr": 2.5, "tp2_atr": 4.0, "sl_mult": 1.6, "trail_mult": 3.0,
                   "roe_trail_activate": 1.5, "runner_bias": 1.0},
        "INDEX":  {"tp1_atr": 2.5, "tp2_atr": 4.0, "sl_mult": 1.5, "trail_mult": 2.8,
                   "roe_trail_activate": 1.2, "runner_bias": 0.9},
        "STOCK":  {"tp1_atr": 2.5, "tp2_atr": 4.0, "sl_mult": 1.5, "trail_mult": 2.6,
                   "roe_trail_activate": 1.2, "runner_bias": 0.9},
        "GOLD":   {"tp1_atr": 1.8, "tp2_atr": 3.0, "sl_mult": 1.3, "trail_mult": 2.0,
                   "roe_trail_activate": 0.8, "runner_bias": 0.6},
        "OIL":    {"tp1_atr": 1.8, "tp2_atr": 3.0, "sl_mult": 1.3, "trail_mult": 1.8,
                   "roe_trail_activate": 0.8, "runner_bias": 0.6},
        "NEWS":   {"tp1_atr": 1.6, "tp2_atr": 2.6, "sl_mult": 1.3, "trail_mult": 1.8,
                   "roe_trail_activate": 0.8, "runner_bias": 0.5},
    }

    # Per asset-class Order-Block / zone detection tuning (OB_ASSET_TUNING).
    # OB_UNIFIED mirrors the legacy hardcoded behaviour exactly; when tuning is
    # disabled the merged config for EVERY class equals OB_UNIFIED so no existing
    # decision changes. When enabled, OB_ASSET overrides apply per class.
    OB_UNIFIED = {
        "ob_min_disp_atr": 0.6,
        "ob_search_bars": 35,
        "ob_fresh_grade": (10, 20, 40),
        "ob_require_sweep_aplus": False,
        "ob_require_pd_aplus": False,
        "ob_round_number_liq": False,
        "ob_pd_extend_bars": 0,
        "ob_news_events": (),
        "ob_spread_cap_pct": None,
    }

    OB_ASSET = {
        "CRYPTO": {"ob_min_disp_atr": 1.1, "ob_search_bars": 35, "ob_fresh_grade": (12, 24, 45),
                   "ob_require_sweep_aplus": False, "ob_require_pd_aplus": True,
                   "ob_round_number_liq": False, "ob_pd_extend_bars": 24,
                   "ob_news_events": (), "ob_spread_cap_pct": None},
        "INDEX":  {"ob_min_disp_atr": 1.2, "ob_search_bars": 30, "ob_fresh_grade": (10, 20, 40),
                   "ob_require_sweep_aplus": True, "ob_require_pd_aplus": True,
                   "ob_round_number_liq": False, "ob_pd_extend_bars": 40,
                   "ob_news_events": ("NFP", "CPI", "FOMC"), "ob_spread_cap_pct": 0.05},
        "STOCK":  {"ob_min_disp_atr": 1.0, "ob_search_bars": 25, "ob_fresh_grade": (5, 10, 20),
                   "ob_require_sweep_aplus": False, "ob_require_pd_aplus": False,
                   "ob_round_number_liq": False, "ob_pd_extend_bars": 0,
                   "ob_news_events": ("EARNINGS",), "ob_spread_cap_pct": 0.05},
        "GOLD":   {"ob_min_disp_atr": 2.5, "ob_search_bars": 45, "ob_fresh_grade": (8, 16, 32),
                   "ob_require_sweep_aplus": True, "ob_require_pd_aplus": True,
                   "ob_round_number_liq": True, "ob_pd_extend_bars": 32,
                   "ob_news_events": ("FOMC", "NFP", "CPI"), "ob_spread_cap_pct": 0.20},
        "OIL":    {"ob_min_disp_atr": 2.0, "ob_search_bars": 40, "ob_fresh_grade": (10, 20, 40),
                   "ob_require_sweep_aplus": True, "ob_require_pd_aplus": True,
                   "ob_round_number_liq": False, "ob_pd_extend_bars": 32,
                   "ob_news_events": ("EIA", "API", "OPEC", "REGULATORY"), "ob_spread_cap_pct": 0.15},
        "NEWS":   {"ob_min_disp_atr": 0.6, "ob_search_bars": 35, "ob_fresh_grade": (10, 20, 40),
                   "ob_require_sweep_aplus": False, "ob_require_pd_aplus": False,
                   "ob_round_number_liq": False, "ob_pd_extend_bars": 0,
                   "ob_news_events": (), "ob_spread_cap_pct": None},
    }

    ENTRY_PROFILES = {
        "CRYPTO": {"sweep_bars": 12, "min_disp_atr": 0.75, "min_adx": 16, "max_adx": 55, "retest_atr": 0.90, "ready_score": 68},
        "INDEX":  {"sweep_bars": 10, "min_disp_atr": 0.90, "min_adx": 18, "max_adx": 60, "retest_atr": 0.85, "ready_score": 68},
        "STOCK":  {"sweep_bars": 10, "min_disp_atr": 0.80, "min_adx": 16, "max_adx": 55, "retest_atr": 0.90, "ready_score": 67},
        "GOLD":   {"sweep_bars": 10, "min_disp_atr": 1.10, "min_adx": 18, "max_adx": 60, "retest_atr": 0.80, "ready_score": 70},
        "OIL":    {"sweep_bars": 10, "min_disp_atr": 1.00, "min_adx": 18, "max_adx": 60, "retest_atr": 0.85, "ready_score": 70},
        "NEWS":   {"sweep_bars": 8, "min_disp_atr": 0.80, "min_adx": 16, "max_adx": 70, "retest_atr": 0.90, "ready_score": 70},
    }

    @classmethod
    def entry_config(cls, asset_class: str) -> dict:
        ac = (asset_class or "CRYPTO").upper()
        cfg = dict(cls.ENTRY_PROFILES.get(ac, cls.ENTRY_PROFILES["CRYPTO"]))
        prefix = f"INSTITUTIONAL_{ac}_"
        env_map = {
            "sweep_bars": "SWEEP_BARS", "min_disp_atr": "MIN_DISP_ATR",
            "min_adx": "MIN_ADX", "max_adx": "MAX_ADX", "retest_atr": "RETEST_ATR",
            "ready_score": "READY_SCORE"
        }
        for key, suffix in env_map.items():
            raw = os.getenv(prefix + suffix)
            if raw is not None:
                try:
                    cfg[key] = int(float(raw)) if key == "sweep_bars" else float(raw)
                except ValueError:
                    pass
        return cfg

    @classmethod
    def _ob_tuning_enabled(cls) -> bool:
        return os.getenv("OB_ASSET_TUNING", "false").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def ob_config(cls, asset_class: str = None) -> dict:
        """Merged OB-detection config for an asset class. Returns OB_UNIFIED
        exactly (legacy behaviour) whenever OB_ASSET_TUNING is disabled; when
        enabled, per-class overrides apply and ob_min_disp_atr remains
        overridable per class via OB_<CLASS>_DISP_ATR."""
        cfg = dict(cls.OB_UNIFIED)
        if not cls._ob_tuning_enabled():
            return cfg
        ac = (asset_class or "CRYPTO").upper()
        overrides = cls.OB_ASSET.get(ac)
        if overrides:
            cfg.update(overrides)
        env_disp = os.getenv("OB_{}_DISP_ATR".format(ac))
        if env_disp:
            try:
                cfg["ob_min_disp_atr"] = float(env_disp)
            except ValueError:
                pass
        return cfg

    @classmethod
    def get(cls, asset_class: str) -> dict:
        params = cls.DEFAULT.get((asset_class or "CRYPTO").upper(), cls.DEFAULT["CRYPTO"])
        return {k: v for k, v in params.items()}

    @classmethod
    def resolve_asset_class(cls, symbol: str) -> str:
        """Best-effort asset classification from a symbol when portfolio context
        is not available. Returns one of CRYPTO/INDEX/GOLD/OIL/STOCK."""
        try:
            from portfolio.manager import PortfolioManager
            ac = PortfolioManager._asset_class(symbol)
            if ac and ac.upper() in cls.DEFAULT:
                return ac.upper()
        except Exception:
            pass
        up = (symbol or "").upper()
        if "GOLD" in up or "XAU" in up:
            return "GOLD"
        if "OIL" in up or "WTI" in up or "CL" in up or "BRENT" in up:
            return "OIL"
        if any(t in up for t in ("US30", "NAS100", "SPX500", "US500", "IDX", "DXY", "USTEC")):
            return "INDEX"
        if any(t in up for t in ("AAPL", "MSFT", "TSLA", "AMZN", "GOOG", "NVDA", "META", "NFLX")):
            return "STOCK"
        return "CRYPTO"


# -----------------------------------------------------------------------------
# DynamicPositionProfile: dynamic trade classification captured at OPEN
# -----------------------------------------------------------------------------
class DynamicPositionProfile:
    """
    Holds the dynamic classification of an open trade. Unlike legacy STATE keys
    (trade_type set once at entry), this profile is RE-EVALUATED through the
    trade life so management can adapt as the thesis evolves.
    """

    TRADE_TYPES = ("TREND", "REVERSAL", "SNIPER_REVERSAL", "BREAKOUT", "PULLBACK", "RETEST", "NEWS")

    def __init__(self, symbol: str, side: str, entry: float, atr: float,
                 classification: str = "", trade_type: str = "TREND",
                 asset_class: str = "CRYPTO"):
        self.symbol = symbol
        self.side = side
        self.entry = entry
        self.atr = max(atr, 1e-9)
        self.asset_class = AssetBehaviorProfile.resolve_asset_class(symbol) if not asset_class else asset_class.upper()
        # initial classification from the entry-time classifier (narrative based)
        self.trade_type = self._normalize_type(trade_type, classification)
        self.classification = classification or "NORMAL"
        # persistent advisory metrics
        self.trend_health = 5.0
        self.structure_aligned = False
        self.continuation_probability = 0.5
        self.trade_state = "RANGE_CHOP"
        self.trade_state_class = "NEUTRAL"
        self.created_at = time.time()

    def _normalize_type(self, trade_type, classification) -> str:
        if trade_type and trade_type.upper() in self.TRADE_TYPES:
            return trade_type.upper()
        # map legacy narrative classifications into one of the 6 types
        cl = (classification or "").upper()
        if cl in ("REVERSAL", "ANTI_TREND", "STRONG_REVERSAL"):
            return "REVERSAL"
        if cl in ("SNIPER_REVERSAL",):
            return "SNIPER_REVERSAL"
        if cl in ("BREAKOUT", "STRONG_BREAKOUT", "SNIPER"):
            return "BREAKOUT"
        if cl in ("PULLBACK", "PULLBACK_TREND"):
            return "PULLBACK"
        if cl in ("RETEST", "RETEST_TREND"):
            return "RETEST"
        return "TREND"

    def _trade_state_class(self, trade_state: str) -> str:
        """Classify the 12 TradeStateMachine states into a directional category
        to help the advisory decision without touching the real state machine."""
        s = (trade_state or "").upper()
        if s in ("TREND_RIDE", "EXPANSION", "ACCUMULATION"):
            return "HEALTHY"
        if s in ("HEALTHY_PULLBACK",):
            return "PULLBACK"
        if s in ("DISTRIBUTION", "PROFIT_DEFENSE", "EXHAUSTION"):
            return "DEFENSE"
        if s in ("MOMENTUM_COLLAPSE", "PANIC_EXIT", "LIQUIDITY_EXHAUSTION", "FAKE_BREAKOUT"):
            return "FAILURE"
        return "NEUTRAL"

    def update(self, *, trade_state: str, trend_health: float, structure_aligned: bool,
               continuation_probability: float, smart_money: dict = None,
               structure_shift: str = None, exhaustion_evidence: bool = False,
               reversal_confirmed: bool = False) -> None:
        """
        Re-evaluate the trade_type dynamically from the live channels.
        This is ADVISORY (stores only). Real exit decisions run later.
        """
        self.trade_state = trade_state or self.trade_state
        self.trade_state_class = self._trade_state_class(trade_state)
        self.trend_health = trend_health if trend_health is not None else self.trend_health
        self.structure_aligned = bool(structure_aligned)
        self.continuation_probability = continuation_probability if continuation_probability is not None else self.continuation_probability

        cur = self.trade_type
        # A NEWS-driven trade keeps its own thesis: it must never be silently
        # folded into the technical TREND/REVERSAL taxonomy. There is no
        # designed NEWS -> TREND / NEWS -> REVERSAL transition, so a NEWS
        # position stays NEWS for its whole life (managed as NEWS_DRIVEN).
        if cur == "NEWS":
            return
        state_cls = self.trade_state_class
        cont = self.continuation_probability
        dist_risk = (smart_money or {}).get("distribution_risk", 0) if smart_money else 0

        # Priority: confirmed reversal / exhaustion overrides everything.
        if reversal_confirmed:
            self.trade_type = "REVERSAL"
        elif state_cls == "FAILURE":
            self.trade_type = "REVERSAL" if not structure_aligned else cur
        elif exhaustion_evidence or dist_risk > 65:
            self.trade_type = "REVERSAL" if not (cont > 0.7 and structure_aligned) else cur
        elif state_cls == "PULLBACK" and cont > 0.6 and trend_health >= 5:
            # trend continuation pullback, NOT a reversal
            self.trade_type = "PULLBACK" if cur in ("TREND", "PULLBACK", "RETEST") else cur
        elif state_cls == "HEALTHY" and cont > 0.65 and structure_aligned:
            self.trade_type = "TREND"
        elif state_cls == "DEFENSE":
            self.trade_type = "REVERSAL" if dist_risk > 50 else cur
        # leave BREAKOUT/RETEST/REVERSAL set at entry unless evidence overrides

    def to_state_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side, "asset_class": self.asset_class,
            "trade_type": self.trade_type, "classification": self.classification,
            "trade_state": self.trade_state, "trade_state_class": self.trade_state_class,
            "created_at": self.created_at
        }


# -----------------------------------------------------------------------------
# PositionHealthScore: unified 0-100 score + advisory ACTION
# -----------------------------------------------------------------------------
class PositionHealthScore:
    """
    Aggregates the parallel live channels into one actionable health score (0-100)
    plus an advisory ACTION. Weights are fixed per call; asset-specific tuning is
    applied at decision time (Phase 2) via AssetBehaviorProfile.
    """

    def compute(self, *, profile: DynamicPositionProfile, trade_state: str,
                trend_health: float, structure_aligned: bool, adx: float,
                smart_money: dict, momentum: dict, continuation_probability: float,
                continuation_pressure: float, thesis_failure_score: float,
                exit_warning: float, zone_strength: float = 0.0,
                roe: float = 0.0, drawdown_from_peak: float = 0.0,
                news_state: str = "NEWS_NEUTRAL") -> dict:
        dist_risk = smart_money.get("distribution_risk", 0)
        mom_health = momentum.get("momentum_health", 50)
        cont_strength = momentum.get("continuation_strength", 50)
        exhaustion_risk = momentum.get("exhaustion_risk", 0)

        # 1) TREND health (0-10 -> 0-100)
        trend = max(0.0, min(1.0, trend_health / 10.0)) if trend_health else 0.5

        # 2) STRUCTURE health (aligned structure + DI dominance)
        structure = 1.0 if structure_aligned else 0.3

        # 3) LIQUIDITY health (low distribution risk)
        liquidity = 1.0 - max(0.0, min(1.0, dist_risk / 100.0))

        # 4) MOMENTUM health (0-100 -> 0-100)
        mom = max(0.0, min(1.0, mom_health / 100.0)) if mom_health else 0.5

        # 5) INSTITUTIONAL health (cont pressure + smart money dominance)
        inst = max(0.0, min(1.0, continuation_pressure / 100.0)) if continuation_pressure else 0.5

        # 6) ZONE health (reclaim-risk based; default neutral when unavailable)
        zone = max(0.0, min(1.0, zone_strength / 100.0)) if zone_strength else 0.5

        # 7) RISK health (inverse of thesis failure + exit warning + drawdown)
        risk = 1.0 - max(0.0, min(1.0, thesis_failure_score / 100.0))
        risk = max(0.0, risk - exit_warning * 0.05 - drawdown_from_peak * 0.02)

        # News dampens risk-health but never forces a blind close.
        if news_state == "NEWS_CRITICAL":
            risk = risk * 0.6
        elif news_state == "NEWS_HIGH":
            risk = risk * 0.8
        elif news_state == "NEWS_MEDIUM":
            risk = risk * 0.9

        weights = {"trend": 0.25, "structure": 0.15, "liquidity": 0.15,
                   "momentum": 0.15, "institutional": 0.10, "zone": 0.10, "risk": 0.10}
        score = (weights["trend"] * trend + weights["structure"] * structure +
                 weights["liquidity"] * liquidity + weights["momentum"] * mom +
                 weights["institutional"] * inst + weights["zone"] * zone +
                 weights["risk"] * risk) * 100.0
        score = max(0.0, min(100.0, score))

        # Advisory ACTION + REASON (Phase 2 turns this into enforceable rules)
        action = "HOLD"
        reason = "health stable"
        confidence = 0.6
        if score >= 80:
            action = "HOLD_TRAIL" if roe > 0 and profile.trade_type == "TREND" else "HOLD"
            reason = "strong all-channel health"
            confidence = 0.85
        elif score >= 65:
            action = "HOLD"
            reason = "moderate health; monitor closely"
            confidence = 0.7
        elif score >= 45:
            action = "PROTECT_PROFIT"
            reason = "weakening health; consider partial or trailing"
            confidence = 0.65
        elif score >= 30:
            action = "PARTIAL"
            reason = "poor health; partial de-risk advised"
            confidence = 0.75
        else:
            action = "EXIT"
            reason = "critical health; exit advised"
            confidence = 0.9

        return {
            "score": round(score, 1),
            "action": action,
            "reason": reason,
            "confidence": confidence,
            "components": {
                "trend": round(trend * 100, 1), "structure": round(structure * 100, 1),
                "liquidity": round(liquidity * 100, 1), "momentum": round(mom * 100, 1),
                "institutional": round(inst * 100, 1), "zone": round(zone * 100, 1),
                "risk": round(risk * 100, 1)
            },
            "news_state": news_state
        }


# -----------------------------------------------------------------------------
# Advisory [POSITION] logging helper (mirrors the required rich log format)
# -----------------------------------------------------------------------------
def log_position_decision(symbol, side, asset_class, trade_type, regime, entry,
                          mark, unrealized_pnl, atr, health, action, reason,
                          confidence, force=False, min_interval=5.0):
    """Emit a structured [POSITION] advisory log. Throttled unless force=True
    (used at key transitions). Preserves the required field layout."""
    try:
        now = time.time()
        last = STATE.get("last_position_log_ts", 0.0)
        if not force and now - last < min_interval:
            return
        STATE["last_position_log_ts"] = now
        hs = ", ".join(f"{k}={int(v)}" for k, v in (health.get("components", {})).items())
        log_execution(
            f"[POSITION] SUBJECT={symbol} {side} ASSET_TYPE={asset_class} "
            f"TRADE_TYPE={trade_type} REGIME={regime} ENTRY={entry:.4f} "
            f"MARK={mark:.4f} UNREALIZED_PNL={unrealized_pnl:.2f}% ATR={atr:.4f} "
            f"HEALTH={health.get('score', 0):.1f} [{hs}] ACTION={action} "
            f"REASON={reason} CONFIDENCE={confidence:.2f}",
            "INFO"
        )
    except Exception:
        pass


def _get_advisory_news_state(symbol: str) -> str:
    """
    Map the stored per-symbol news risk (0-100) into the management-friendly
    NEWS_LOW/MEDIUM/HIGH/CRITICAL states (NEWS_NEUTRAL when unknown). This is the
    news-AWARE input to the advisory risk-health: it DAMPENS risk-health on high
    impact but never forces a blind close (Phase 2 governs the actual handling).
    """
    try:
        wl = MEMORY.get("watchlist", {})
        info = (wl or {}).get(symbol, {}) or {}
        raw = info.get("news_risk", 0) or 0
        try:
            risk = float(raw)
        except (TypeError, ValueError):
            risk = 0.0
        if risk >= 90:
            return "NEWS_CRITICAL"
        if risk >= 70:
            return "NEWS_HIGH"
        if risk >= 40:
            return "NEWS_MEDIUM"
        if risk <= 0:
            return "NEWS_NEUTRAL"
        return "NEWS_LOW"
    except Exception:
        return "NEWS_NEUTRAL"


def _advisory_news_context(symbol: str, side: str) -> dict:
    """
    Phase-3 (G4): side-aware news context for the advisory stack.

    Unlike _get_advisory_news_state (pure risk band), this adds DIRECTION:
    news bias aligned with the position side -> SUPPORT, opposed -> CONFLICT.
    News is evidence, never an order signal: it can nudge risk-health but can
    never close a position by itself (Phase-2 guards govern the handling).
    """
    try:
        wl = MEMORY.get("watchlist", {})
        info = (wl or {}).get(symbol, {}) or {}
        news = info.get("news") or {}
        raw = news.get("bias") if isinstance(news, dict) else None
        bias = str(raw or info.get("news_bias") or "NEUTRAL").upper()
        risk = 0.0
        try:
            risk = float(news.get("risk", info.get("news_risk", 0) or 0) or 0) if isinstance(news, dict) else float(info.get("news_risk", 0) or 0)
        except (TypeError, ValueError):
            risk = 0.0
        state = _get_advisory_news_state(symbol)
        aligned = (side == "BUY" and bias == "BULLISH") or (side == "SELL" and bias == "BEARISH")
        opposed = (side == "BUY" and bias == "BEARISH") or (side == "SELL" and bias == "BULLISH")
        return {
            "state": state,
            "risk": risk,
            "bias": bias,
            "aligned": bool(aligned),
            "opposed": bool(opposed),
            "directional": bool(aligned or opposed),
            "available": bool(info.get("news_available", False) or news.get("available", False)) if isinstance(news, dict) else bool(info.get("news_available", False)),
        }
    except Exception:
        return {"state": "NEWS_NEUTRAL", "risk": 0.0, "bias": "NEUTRAL",
                "aligned": False, "opposed": False, "directional": False,
                "available": False}


# ========== TRADE STATE MACHINE (UNCHANGED) ==========
class TradeStateMachine:
    STATES = {
        "ACCUMULATION": 0,
        "EXPANSION": 1,
        "TREND_RIDE": 2,
        "DISTRIBUTION": 3,
        "EXHAUSTION": 4,
        "FAKE_BREAKOUT": 5,
        "MOMENTUM_COLLAPSE": 6,
        "PANIC_EXIT": 7,
        "RANGE_CHOP": 8,
        "HEALTHY_PULLBACK": 9,
        "PROFIT_DEFENSE": 10,
        "LIQUIDITY_EXHAUSTION": 11
    }

    def __init__(self):
        self.current_state = "RANGE_CHOP"
        self.last_state_change = 0
        self.state_confidence = 0.0

    def update(self, smart: dict, momentum: dict, adx: float, regime: str) -> str:
        banker = smart.get("banker_pressure", 50)
        retail = smart.get("retailer_pressure", 50)
        dist_risk = smart.get("distribution_risk", 0)
        accum = smart.get("accumulation_strength", 0)
        mom_health = momentum.get("momentum_health", 50)
        cont_strength = momentum.get("continuation_strength", 50)
        exh_risk = momentum.get("exhaustion_risk", 0)
        climax = momentum.get("climax_risk", 0)
        expansion = momentum.get("trend_expansion", False)
        decay = momentum.get("momentum_decay", False)
        bias_detailed = smart.get("institutional_bias_detailed", "NEUTRAL")

        if (bias_detailed in ("STRONG_SELL", "STRONG_BUY") and dist_risk > 75 and mom_health < 15 and cont_strength < 20):
            new_state = "PANIC_EXIT"
        elif mom_health < 15 and cont_strength < 25 and decay:
            new_state = "MOMENTUM_COLLAPSE"
        elif exh_risk > 70 or climax > 75:
            new_state = "LIQUIDITY_EXHAUSTION"
        elif dist_risk > 50 and banker < 45 and mom_health < 30:
            new_state = "PROFIT_DEFENSE"
        elif dist_risk > 60 and banker < 45:
            new_state = "DISTRIBUTION"
        elif banker > 65 and dist_risk < 25 and mom_health > 40:
            new_state = "ACCUMULATION"
        elif adx > 30 and expansion and cont_strength > 60 and mom_health > 50:
            new_state = "EXPANSION"
        elif cont_strength > 75 and mom_health > 60 and dist_risk < 30:
            new_state = "TREND_RIDE"
        elif 20 <= adx <= 35 and mom_health > 45 and not expansion and not decay and dist_risk < 40:
            new_state = "HEALTHY_PULLBACK"
        elif retail > 70 and banker < 45 and climax > 60:
            new_state = "FAKE_BREAKOUT"
        elif adx < 22 or regime in ("CHOPPY", "COMPRESSION"):
            new_state = "RANGE_CHOP"
        else:
            new_state = self.current_state

        if new_state != self.current_state:
            self.last_state_change = time.time()
            self.state_confidence = 0.5
            log_execution(f"[STATE] {self.current_state} -> {new_state}", "INFO")
        else:
            self.state_confidence = min(1.0, self.state_confidence + 0.05)

        self.current_state = new_state
        return new_state

    def get_trail_multiplier(self) -> float:
        mult_map = {
            "ACCUMULATION": 3.0,
            "EXPANSION": 3.5,
            "TREND_RIDE": 4.0,
            "HEALTHY_PULLBACK": 2.8,
            "PROFIT_DEFENSE": 1.2,
            "DISTRIBUTION": 1.2,
            "EXHAUSTION": 1.0,
            "LIQUIDITY_EXHAUSTION": 0.8,
            "FAKE_BREAKOUT": 0.8,
            "MOMENTUM_COLLAPSE": 0.6,
            "PANIC_EXIT": 0.5,
            "RANGE_CHOP": 1.5
        }
        return mult_map.get(self.current_state, 1.5)

    def should_delay_tp1(self) -> bool:
        return self.current_state in ("ACCUMULATION", "EXPANSION", "TREND_RIDE", "HEALTHY_PULLBACK")

    def should_aggressive_profit_lock(self) -> bool:
        return self.current_state in ("EXHAUSTION", "DISTRIBUTION", "MOMENTUM_COLLAPSE", "PROFIT_DEFENSE", "LIQUIDITY_EXHAUSTION")

    def should_hard_exit(self) -> bool:
        return self.current_state in ("PANIC_EXIT", "MOMENTUM_COLLAPSE", "LIQUIDITY_EXHAUSTION")

    def get_patience_level(self) -> str:
        if self.current_state in ("ACCUMULATION", "EXPANSION", "TREND_RIDE", "HEALTHY_PULLBACK"):
            return "HIGH"
        elif self.current_state in ("DISTRIBUTION", "EXHAUSTION", "PROFIT_DEFENSE"):
            return "LOW"
        else:
            return "MEDIUM"


# ========== TRADE STATE & PERFORMANCE TRACKING ==========
TRADE_STATE = {
    "in_position": False,
    "symbol": None,
    "side": None,
    "entry": 0.0,
    "qty": 0.0,
    "tp1_hit": False,
    "tp2_hit": False,
    "trail_on": False,
    "zone": None,
    "location": None,
    "reason": [],
    "last_update_ts": 0
}

PERF = {
    "total_pnl_pct": 0.0,
    "total_pnl_usdt": 0.0,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "last_trade": None
}

# ========== INSTITUTIONAL TREND ENGINE ==========
class TrendState(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    PROBATION_BULLISH = "PROBATION_BULLISH"
    PROBATION_BEARISH = "PROBATION_BEARISH"
    CHOP = "CHOP"

class InstitutionalTrendEngine:
    def __init__(self):
        self.trend_state = TrendState.CHOP
        self.trend_persistence = 0
        self.last_state_change = 0
        self.state_confidence = 0.0

    def analyze_adx_momentum(self, adx_series):
        if adx_series is None or len(adx_series) < 10:
            return {"value": 20, "slope": 0, "acceleration": 0, "state": "UNKNOWN", "rising": False}
        current = adx_series.iloc[-1]
        slope = adx_series.iloc[-1] - adx_series.iloc[-4] if len(adx_series) >= 4 else 0
        accel = slope - (adx_series.iloc[-4] - adx_series.iloc[-7]) if len(adx_series) >= 7 else 0
        if current < 18:
            state = "CHOP"
        elif 18 <= current < 25:
            state = "EMERGING"
        elif 25 <= current < 35:
            state = "HEALTHY"
        elif 35 <= current < 45:
            state = "STRONG"
        else:
            state = "EXHAUSTION"
        return {"value": current, "slope": slope, "acceleration": accel, "state": state, "rising": slope > 0}

    def analyze_di_pressure(self, df):
        plus_di, minus_di, _, _ = get_di_components(df)
        if plus_di is None or minus_di is None:
            return {"dominant": "NEUTRAL", "spread": 0, "persistent": False}
        spread = plus_di - minus_di
        dominant = "BUY" if plus_di > minus_di else "SELL" if minus_di > plus_di else "NEUTRAL"
        persistent = False
        if len(df) >= 6:
            buy_count = 0
            sell_count = 0
            for i in range(-5, 0):
                p, m, _, _ = get_di_components(df.iloc[:i+1] if i < 0 else df)
                if p is not None and m is not None:
                    if p > m:
                        buy_count += 1
                    elif m > p:
                        sell_count += 1
            if dominant == "BUY" and buy_count >= 4:
                persistent = True
            elif dominant == "SELL" and sell_count >= 4:
                persistent = True
        return {"dominant": dominant, "spread": spread, "persistent": persistent}

    def analyze_pullback(self, df, side, atr):
        if len(df) < 5:
            return "NO_PULLBACK"
        last = df.iloc[-1]
        prev_candles = df.iloc[-5:-1]
        if side == "SELL":
            bullish_candles = [c for i, c in prev_candles.iterrows() if c['close'] > c['open']]
            if not bullish_candles and last['close'] <= last['open']:
                return "NO_PULLBACK"
            avg_body = sum(abs(c['close'] - c['open']) for _, c in prev_candles.iterrows()) / len(prev_candles)
            upper_wicks = sum((c['high'] - max(c['close'], c['open'])) for _, c in prev_candles.iterrows()) / len(prev_candles)
            vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].iloc[-10:-1].mean()
            di = self.analyze_di_pressure(df)
            adx_mom = self.analyze_adx_momentum(compute_adx(df))
            weak_conditions = (avg_body < atr * 0.4 and upper_wicks > avg_body and vol < avg_vol * 0.8 and di["dominant"] == "SELL" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
            if weak_conditions:
                return "WEAK_PULLBACK"
            if last['close'] > last['open'] and last['close'] > prev_candles['close'].max():
                if vol > avg_vol * 1.5 and di["dominant"] == "BUY" and not adx_mom["rising"]:
                    return "REVERSAL"
            return "STRONG_PULLBACK"
        else:
            bearish_candles = [c for i, c in prev_candles.iterrows() if c['close'] < c['open']]
            if not bearish_candles and last['close'] >= last['open']:
                return "NO_PULLBACK"
            avg_body = sum(abs(c['close'] - c['open']) for _, c in prev_candles.iterrows()) / len(prev_candles)
            lower_wicks = sum((min(c['open'], c['close']) - c['low']) for _, c in prev_candles.iterrows()) / len(prev_candles)
            vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].iloc[-10:-1].mean()
            di = self.analyze_di_pressure(df)
            adx_mom = self.analyze_adx_momentum(compute_adx(df))
            weak_conditions = (avg_body < atr * 0.4 and lower_wicks > avg_body and vol < avg_vol * 0.8 and di["dominant"] == "BUY" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
            if weak_conditions:
                return "WEAK_PULLBACK"
            if last['close'] < last['open'] and last['close'] < prev_candles['close'].min():
                if vol > avg_vol * 1.5 and di["dominant"] == "SELL" and not adx_mom["rising"]:
                    return "REVERSAL"
            return "STRONG_PULLBACK"

    def calculate_exit_score(self, ctx):
        score = 0
        if ctx.get("di_flip", False): score += 3
        if ctx.get("adx_collapse", False): score += 2
        if ctx.get("strong_reclaim", False): score += 3
        if ctx.get("exhaustion", False): score += 4
        if ctx.get("htf_opposite", False): score += 2
        if ctx.get("momentum_loss", False): score += 2
        if ctx.get("failed_continuation", False): score += 2
        return score

    def is_chop(self, df):
        adx = compute_adx(df)
        if adx is None or len(adx) < 20:
            return True
        adx_val = adx.iloc[-1]
        plus_di, minus_di, _, _ = get_di_components(df)
        if plus_di is None or minus_di is None:
            return True
        di_spread = abs(plus_di - minus_di)
        atr = compute_atr(df).iloc[-1]
        atr_ma = compute_atr(df).rolling(20).mean().iloc[-1] if len(df) >= 20 else atr
        atr_flat = abs(atr - atr_ma) / atr_ma < 0.1 if atr_ma > 0 else True
        vol_state = classify_volume(df)
        low_volume = vol_state in ("exhaustion", "neutral") and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 0.7
        return adx_val < 18 and di_spread < 5 and atr_flat and low_volume

    def update_trend_state(self, df, ob):
        adx_series = compute_adx(df)
        adx_mom = self.analyze_adx_momentum(adx_series)
        di_pressure = self.analyze_di_pressure(df)
        chop = self.is_chop(df)
        now = time.time()
        if chop:
            if self.trend_state != TrendState.CHOP:
                self.trend_state = TrendState.CHOP
                self.last_state_change = now
                self.trend_persistence = 0
                self.state_confidence = 0.0
            return
        bullish = (di_pressure["dominant"] == "BUY" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
        bearish = (di_pressure["dominant"] == "SELL" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
        if bullish and not bearish:
            target_state = TrendState.BULLISH
        elif bearish and not bullish:
            target_state = TrendState.BEARISH
        else:
            target_state = TrendState.CHOP
        if target_state != self.trend_state:
            if self.trend_state in (TrendState.BULLISH, TrendState.BEARISH):
                if self.trend_state == TrendState.BULLISH and target_state == TrendState.BEARISH:
                    self.trend_state = TrendState.PROBATION_BEARISH
                elif self.trend_state == TrendState.BEARISH and target_state == TrendState.BULLISH:
                    self.trend_state = TrendState.PROBATION_BULLISH
                else:
                    self.trend_state = target_state
                self.last_state_change = now
                self.trend_persistence = 0
                self.state_confidence = 0.3
            elif self.trend_state in (TrendState.PROBATION_BULLISH, TrendState.PROBATION_BEARISH):
                if now - self.last_state_change > 3600:
                    self.trend_state = target_state
                    self.state_confidence = 0.6
            else:
                self.trend_state = target_state
                self.last_state_change = now
                self.trend_persistence = 0
                self.state_confidence = 0.5
        else:
            self.trend_persistence += 1
            self.state_confidence = min(1.0, self.state_confidence + 0.02)

    def should_enter(self, side, df, ob, atr, price):
        if self.is_chop(df):
            return False, "CHOP market"
        if side == "BUY" and self.trend_state not in (TrendState.BULLISH, TrendState.PROBATION_BULLISH):
            return False, f"Trend not bullish ({self.trend_state.value})"
        if side == "SELL" and self.trend_state not in (TrendState.BEARISH, TrendState.PROBATION_BEARISH):
            return False, f"Trend not bearish ({self.trend_state.value})"
        pullback = self.analyze_pullback(df, side, atr)
        if pullback == "REVERSAL":
            return False, "Strong reversal detected"
        if pullback == "STRONG_PULLBACK":
            return False, "Pullback too strong"
        adx_series = compute_adx(df)
        adx_mom = self.analyze_adx_momentum(adx_series)
        if adx_mom["state"] not in ("HEALTHY", "STRONG", "EMERGING"):
            return False, f"ADX weak ({adx_mom['state']})"
        di = self.analyze_di_pressure(df)
        if not di["persistent"] and di["spread"] < 3:
            return False, "DI not persistent"
        return True, f"Trend {self.trend_state.value}, Pullback={pullback}"

    def get_trend_health(self, df, side):
        adx_mom = self.analyze_adx_momentum(compute_adx(df))
        di = self.analyze_di_pressure(df)
        health = 5
        if adx_mom["rising"]: health += 2
        if adx_mom["state"] == "HEALTHY": health += 1
        elif adx_mom["state"] == "STRONG": health += 2
        elif adx_mom["state"] == "EXHAUSTION": health -= 2
        if di["persistent"]: health += 2
        if side == "BUY" and di["dominant"] == "BUY": health += 1
        elif side == "SELL" and di["dominant"] == "SELL": health += 1
        else: health -= 2
        return max(0, min(10, health))

    def should_hold(self, df, side, atr, current_roe):
        pullback = self.analyze_pullback(df, side, atr)
        adx_mom = self.analyze_adx_momentum(compute_adx(df))
        di = self.analyze_di_pressure(df)
        if pullback == "WEAK_PULLBACK" and adx_mom["rising"] and di["persistent"]:
            return True, "Weak pullback, trend healthy"
        if adx_mom["value"] > 25 and di["dominant"] == side:
            return True, f"ADX {adx_mom['value']:.1f} still strong"
        if current_roe > 3.0 and pullback != "REVERSAL":
            return True, "High profit, allowing pullback"
        return False, "Trend weakening"

    def compute_exit_score_live(self, df, side, entry_price, current_price, atr):
        adx_series = compute_adx(df)
        adx_mom = self.analyze_adx_momentum(adx_series)
        di = self.analyze_di_pressure(df)
        last = df.iloc[-1]
        di_flip = (side == "BUY" and di["dominant"] == "SELL") or (side == "SELL" and di["dominant"] == "BUY")
        adx_collapse = adx_mom["value"] < 18 or (adx_mom["slope"] < -2 and adx_mom["state"] in ("HEALTHY", "STRONG"))
        if side == "SELL":
            strong_reclaim = current_price > entry_price and current_price > last['open']
        else:
            strong_reclaim = current_price < entry_price and current_price < last['open']
        exhaustion = adx_mom["state"] == "EXHAUSTION" and not adx_mom["rising"]
        momentum_loss = adx_mom["slope"] < -1 and adx_mom["acceleration"] < 0
        failed_cont = False
        if side == "SELL" and last['close'] > last['open']:
            if last['high'] - last['low'] > atr * 1.2:
                failed_cont = True
        elif side == "BUY" and last['close'] < last['open']:
            if last['high'] - last['low'] > atr * 1.2:
                failed_cont = True
        ctx = {"di_flip": di_flip, "adx_collapse": adx_collapse, "strong_reclaim": strong_reclaim,
               "exhaustion": exhaustion, "htf_opposite": False, "momentum_loss": momentum_loss,
               "failed_continuation": failed_cont}
        return self.calculate_exit_score(ctx)

trend_engine = InstitutionalTrendEngine()

# ========== INSTITUTIONAL TRADE BRAIN ==========
class InstitutionalTradeBrain:
    def __init__(self):
        self.state_machine = TradeStateMachine()
        self.last_update = 0
        self.current_trade_state = "RANGE_CHOP"

    def update(self, smart: dict, momentum: dict, adx: float, regime: str):
        self.current_trade_state = self.state_machine.update(smart, momentum, adx, regime)
        self.last_update = time.time()
        return self.current_trade_state

    def get_trail_multiplier(self) -> float:
        return self.state_machine.get_trail_multiplier()

    def should_delay_tp1(self) -> bool:
        return self.state_machine.should_delay_tp1()

    def should_aggressive_profit_lock(self) -> bool:
        return self.state_machine.should_aggressive_profit_lock()

    def should_hard_exit(self) -> bool:
        return self.state_machine.should_hard_exit()

    def get_patience_level(self) -> str:
        return self.state_machine.get_patience_level()

# ========== FIXED: ORDER VERIFICATION HELPER ==========
def verify_order_filled(symbol, order_id, side, expected_qty, timeout=10):
    if PAPER_MODE:
        return True, expected_qty
    start = time.time()
    sym = normalize_symbol(symbol)
    while time.time() - start < timeout:
        try:
            order = safe_api_call(ex.fetch_order, order_id, sym)
            if order:
                status = order.get('status')
                filled = order.get('filled', 0)
                if status == 'closed' and filled >= expected_qty * 0.999:
                    return True, filled
                elif status == 'open' or status == 'partial':
                    time.sleep(0.5)
                    continue
            pos = fetch_position(symbol)
            if pos is not None:
                pass
            time.sleep(0.5)
        except Exception as e:
            log_execution(f"[ORDER_VERIFY] Error: {e}", "WARN")
            time.sleep(0.5)
    return False, 0

# ========== FIXED: close_partial with robust verification ==========
_reconciliation_pending = False


def _hedge_position_side(direction):
    """Derive the BingX hedge-mode PositionSide from the POSITION direction.

    The account runs in Hedge Mode, which requires PositionSide to be LONG or
    SHORT (never BOTH). This maps the bot's internal position direction
    (BUY=long, SELL=short; also accepts buy/sell/LONG/SHORT/long/short) to the
    exchange's hedge-mode value. PositionSide always reflects the position being
    opened or closed, not the order side.
    """
    d = str(direction).upper()
    if d in ("BUY", "LONG"):
        return "LONG"
    if d in ("SELL", "SHORT"):
        return "SHORT"
    raise ValueError(f"cannot derive hedge positionSide from direction: {direction!r}")


def close_partial(ratio):
    global _closing_in_progress, _reconciliation_pending
    if _closing_in_progress:
        log_execution("[CLOSE_PARTIAL] Already closing, skipping", "WARN")
        return
    if _reconciliation_pending:
        log_execution("[CLOSE_PARTIAL] Reconciliation pending, skipping", "WARN")
        return
    _closing_in_progress = True
    _reconciliation_pending = True
    try:
        if PAPER_MODE:
            if paper["position"]:
                paper["position"]["remaining_qty"] *= (1-ratio)
                STATE["remaining_qty"] *= (1-ratio)
                TRADE_STATE["qty"] = STATE["remaining_qty"]
                log_execution(f"[CLOSE_PARTIAL] Paper partial close {ratio*100:.0f}%", "SUCCESS")
            return

        symbol = STATE["current_symbol"]
        qty_to_close = STATE["remaining_qty"] * ratio
        if qty_to_close <= 0:
            log_execution("[CLOSE_PARTIAL] No quantity to close", "WARN")
            return

        side = "sell" if STATE["side"] == "BUY" else "buy"
        sym = normalize_symbol(symbol)
        qty_precise = float(ex.amount_to_precision(sym, qty_to_close))
        order = safe_api_call(ex.create_order, sym, "market", side, qty_precise, params={"reduceOnly": True, "positionSide": _hedge_position_side(STATE["side"])})
        if order is None:
            log_execution("[CLOSE_PARTIAL] Order creation failed (None)", "ERROR")
            return
        order_id = order.get('id')
        if not order_id:
            log_execution("[CLOSE_PARTIAL] No order ID returned", "ERROR")
            return

        filled, filled_qty = verify_order_filled(symbol, order_id, side, qty_precise, timeout=10)
        if filled:
            time.sleep(1)
            pos = fetch_position(symbol)
            if pos is None:
                log_execution("[CLOSE_PARTIAL] Position vanished unexpectedly; treating as full close", "WARN")
                STATE["open"] = False
                TRADE_STATE["in_position"] = False
                DASHBOARD_STATE["live_trade_mode"] = False
                finalize_trade_with_reality(symbol)
                return
            current_qty = float(pos.get('contracts', 0))
            expected_remaining = max(0, STATE["remaining_qty"] - filled_qty)
            if abs(current_qty - expected_remaining) < 0.0001 * max(1, expected_remaining):
                STATE["remaining_qty"] = current_qty
                TRADE_STATE["qty"] = current_qty
                log_execution(f"[CLOSE_PARTIAL] Partial close confirmed, remaining qty: {current_qty:.6f}", "SUCCESS")
            else:
                log_execution(f"[CLOSE_PARTIAL] Position mismatch: expected {expected_remaining:.6f}, got {current_qty:.6f}. Using exchange value.", "WARN")
                STATE["remaining_qty"] = current_qty
                TRADE_STATE["qty"] = current_qty
                if current_qty <= 0:
                    STATE["open"] = False
                    TRADE_STATE["in_position"] = False
                    DASHBOARD_STATE["live_trade_mode"] = False
                    finalize_trade_with_reality(symbol)
            _exchange_sync.reconcile(symbol, STATE)
        else:
            log_execution(f"[CLOSE_PARTIAL] Partial close failed to fill after timeout", "ERROR")
            _exchange_sync.reconcile(symbol, STATE)
    except Exception as e:
        log_execution(f"[CLOSE_PARTIAL] Error: {traceback.format_exc()}", "ERROR")
    finally:
        _closing_in_progress = False
        _reconciliation_pending = False

# ========== FIXED: close_position_full with robust verification ==========
def close_position_full():
    global _closing_in_progress, _reconciliation_pending
    if _closing_in_progress:
        log_execution("[CLOSE] Already closing, skipping", "WARN")
        return False
    if _reconciliation_pending:
        log_execution("[CLOSE] Reconciliation pending, skipping", "WARN")
        return False
    _closing_in_progress = True
    _reconciliation_pending = True
    try:
        if PAPER_MODE:
            symbol = STATE.get("current_symbol") or DEFAULT_SYMBOL
            paper["position"] = None
            finalize_trade_with_reality(symbol)
            DASHBOARD_STATE["live_trade_mode"] = False
            log_execution("[CLOSE] Paper position closed", "SUCCESS")
            return True

        if not STATE["open"]:
            log_execution("[CLOSE] No position to close", "WARN")
            return False

        symbol = STATE["current_symbol"]
        qty_to_close = STATE["remaining_qty"]
        if qty_to_close <= 0:
            log_execution("[CLOSE] No quantity to close", "WARN")
            return False

        side = "sell" if STATE["side"] == "BUY" else "buy"
        sym = normalize_symbol(symbol)
        qty_precise = float(ex.amount_to_precision(sym, qty_to_close))

        for attempt in range(3):
            order = safe_api_call(ex.create_order, sym, "market", side, qty_precise, params={"reduceOnly": True, "positionSide": _hedge_position_side(STATE["side"])})
            if order is None:
                log_execution(f"[CLOSE] Order creation failed (attempt {attempt+1})", "ERROR")
                time.sleep(1)
                continue
            order_id = order.get('id')
            if not order_id:
                log_execution(f"[CLOSE] No order ID returned (attempt {attempt+1})", "ERROR")
                time.sleep(1)
                continue

            filled, filled_qty = verify_order_filled(symbol, order_id, side, qty_precise, timeout=10)
            if filled:
                time.sleep(1)
                pos = fetch_position(symbol)
                if pos is None:
                    log_execution("[CLOSE] Position confirmed closed (no position found)", "SUCCESS")
                    STATE["open"] = False
                    TRADE_STATE["in_position"] = False
                    DASHBOARD_STATE["live_trade_mode"] = False
                    finalize_trade_with_reality(symbol)
                    return True
                else:
                    current_qty = float(pos.get('contracts', 0))
                    if current_qty <= 0:
                        log_execution("[CLOSE] Position confirmed closed (qty=0)", "SUCCESS")
                        STATE["open"] = False
                        TRADE_STATE["in_position"] = False
                        DASHBOARD_STATE["live_trade_mode"] = False
                        finalize_trade_with_reality(symbol)
                        return True
                    else:
                        log_execution(f"[CLOSE] Position still has qty {current_qty:.6f} after close order. Retrying...", "WARN")
                        qty_to_close = current_qty
                        qty_precise = float(ex.amount_to_precision(sym, qty_to_close))
                        continue
            else:
                log_execution(f"[CLOSE] Order did not fill (attempt {attempt+1})", "ERROR")
                time.sleep(1)
                continue

        log_execution("[CLOSE] All close attempts failed. Attempting emergency close via position close.", "ERROR")
        try:
            order = safe_api_call(ex.create_order, sym, "market", side, qty_precise, params={"reduceOnly": True, "positionSide": _hedge_position_side(STATE["side"])})
            if order:
                time.sleep(2)
                pos = fetch_position(symbol)
                if pos is None or float(pos.get('contracts', 0)) <= 0:
                    STATE["open"] = False
                    TRADE_STATE["in_position"] = False
                    DASHBOARD_STATE["live_trade_mode"] = False
                    finalize_trade_with_reality(symbol)
                    log_execution("[CLOSE] Emergency close succeeded", "SUCCESS")
                    return True
        except Exception as e:
            log_execution(f"[CLOSE] Emergency close failed: {e}", "ERROR")
        return False
    except Exception as e:
        log_execution(f"[CLOSE] Error: {traceback.format_exc()}", "ERROR")
        return False
    finally:
        _closing_in_progress = False
        _reconciliation_pending = False

# ========== FIXED: apply_50_50_profit_engine with pre-TP1 BE and regime awareness ==========
def apply_50_50_profit_engine(df, idx, price, atr, side, entry, state, roe_pct, trade_state=None, continuation_probability=0.5):
    if roe_pct is None:
        return "HOLD", state.get("sl", 0.0), state.get("trail_stop", 0.0)
    high = df['high'].iloc[-1]
    low = df['low'].iloc[-1]
    state["max_price"] = max(state["max_price"], high)
    state["min_price"] = min(state["min_price"], low)
    smart = state.get("smart_money", {})
    mom = state.get("momentum_flow", {})
    dist_risk = smart.get("distribution_risk", 0)
    continuation_strength = mom.get("continuation_strength", 50)
    momentum_health = mom.get("momentum_health", 50)
    institutional_bias = smart.get("institutional_bias", "NEUTRAL")

    if dist_risk > 45 and not state.get("profit_lock_activated", False):
        log_execution("[PPE] Distribution risk >45 – activating profit lock", "WARN")
        if not state.get("tp1_done", False):
            close_partial(0.5)
        state["profit_lock_activated"] = True

    climax_risk = mom.get("climax_risk", 0)
    if climax_risk > 50 and state.get("trail_active", False):
        if not state.get("trail_tightened", False):
            state["smart_trail_mult"] = max(0.6, state.get("smart_trail_mult", 1.5) * 0.7)
            state["trail_tightened"] = True
            log_execution(f"[PPE] Climax risk {climax_risk:.1f} > 50 – tightened trail to {state['smart_trail_mult']:.2f}x", "WARN")

    if momentum_health < -8 and roe_pct > 2 and not state.get("tp1_hit", False):
        log_execution("[PPE] Negative momentum health – exiting partial to lock profit", "WARN")
        close_partial(0.5)
        state["sl"] = entry
        return "HOLD", state["sl"], state.get("trail_stop", 0.0)

    # Pre-TP1 BE: require meaningful continuation evidence
    if not state.get("trail_active", False) and roe_pct >= 1.5:
        if continuation_probability > 0.6 or mom.get("trend_expansion", False) or (institutional_bias == side and smart.get("smart_money_dominant", False)):
            state["sl"] = entry
            if side == "BUY":
                state["trail_stop"] = price - 1.2 * atr
            else:
                state["trail_stop"] = price + 1.2 * atr
            state["trail_active"] = True
            log_execution(f"[PPE] {state['symbol']} Stage1: BE SL, trail_active @ 1.2xATR (ROE={roe_pct:.2f}%)", "INFO")
            return "HOLD", state["sl"], state["trail_stop"]
        else:
            log_execution(f"[PPE] ROE {roe_pct:.1f}% but continuation evidence insufficient; delaying BE", "INFO")

    remaining = state.get("remaining_qty", 0)
    adx_series = compute_adx(df)
    if not state.get("runner_mode", False) and len(adx_series) > idx:
        adx_val = adx_series.iloc[idx]
        if adx_val >= 25:
            state["runner_mode"] = True
            log_execution(f"[PPE] {state['symbol']} Stage3: Runner mode activated (ADX={adx_val:.1f})", "INFO")

    trail_mult = state.get("smart_trail_mult", 1.5)
    if state.get("trail_active", False):
        if side == "BUY":
            new_stop = state["max_price"] - trail_mult * atr
            if new_stop > state.get("trail_stop", 0):
                state["trail_stop"] = new_stop
        else:
            new_stop = state["min_price"] + trail_mult * atr
            if new_stop < state.get("trail_stop", float('inf')):
                state["trail_stop"] = new_stop

    if state.get("trail_active", False) and state.get("trail_stop", 0):
        if (side == "BUY" and price <= state["trail_stop"]) or (side == "SELL" and price >= state["trail_stop"]):
            log_execution(f"[PPE] {state['symbol']} Exit: trailing stop hit at price {price:.4f}", "WARN")
            return "EXIT", state["sl"], state["trail_stop"]

    if state.get("runner_mode", False) and len(adx_series) >= 2:
        adx_now = adx_series.iloc[idx]
        adx_prev = adx_series.iloc[idx-1]
        last_candle = df.iloc[-1]
        is_bearish = last_candle['close'] < last_candle['open']
        is_bullish = last_candle['close'] > last_candle['open']
        if (side == "BUY" and adx_now < adx_prev and is_bearish) or (side == "SELL" and adx_now < adx_prev and is_bullish):
            log_execution(f"[PPE] {state['symbol']} Exit: momentum weakness (ADX decreasing)", "WARN")
            return "EXIT", state["sl"], state["trail_stop"]

    bos_up, bos_down = detect_bos(df, lookback=5)
    if state.get("runner_mode", False):
        if (side == "BUY" and bos_down) or (side == "SELL" and bos_up):
            log_execution(f"[PPE] {state['symbol']} Exit: structure break (BOS)", "WARN")
            return "EXIT", state["sl"], state["trail_stop"]

    if mom.get("greed_state", False) and roe_pct > 4 and not state.get("profit_lock_activated", False):
        log_execution(f"[PPE] Profit lock activated: greed state, ROE={roe_pct:.2f}%", "WARN")
        if not state.get("tp1_done", False):
            close_partial(0.5)
        state["profit_lock_activated"] = True

    return "HOLD", state["sl"], state["trail_stop"]

# ========== REAL EXCHANGE ORDERS (ONLY ENTRY, PARTIAL, FULL CLOSE) ==========
# No native SL/TP orders are sent.

# ========== LIVE TRADE MANAGER WITH SYNTHETIC PROTECTION (FIXED) ==========
class LiveTradeManager:
    def __init__(self, event_bus, exchange_sync, recovery_guard):
        self.event_bus = event_bus
        self.exchange_sync = exchange_sync
        self.recovery = recovery_guard
        self.lifecycle_state = TradeLifecycleState.IDLE
        self.current_snapshot = None
        self.last_management_ts = 0
        self.last_log_ts = 0
        self.last_live_debug_ts = 0
        self.last_heavy_calc_ts = 0
        self.last_position_sync_ts = 0
        self.continuation_pressure_engine = ContinuationPressureEngine()
        self.thesis_failure_engine = ThesisFailureEngine()
        self.confidence_engine = ConfidenceEngine()
        self.regime_classifier = MarketRegimeClassifier()
        self.brain = InstitutionalTradeBrain()
        # Position Management Engine (Phase 1 advisory layer)
        self.position_profile = None
        self.health_engine = PositionHealthScore()
        self._advisory_last_ts = 0.0
        self._last_position_action = None
        self._last_position_trade_type = None
        self.trade_board = TradeManagementBoard() if TRADE_INTELLIGENCE_AVAILABLE else None
        event_bus.subscribe("reconciled", self._on_reconciled)
        event_bus.subscribe("force_close_local", self._force_close)
        event_bus.subscribe("lifecycle_change", self._set_lifecycle)

    def _set_lifecycle(self, state):
        self.lifecycle_state = state
        log_execution(f"[LIFECYCLE] New state: {state.value}", "INFO")
        DASHBOARD_STATE["lifecycle_state"] = state.value

    def _on_reconciled(self, snapshot):
        self.current_snapshot = snapshot
        DASHBOARD_STATE["live_trade_mode"] = True
        if self.lifecycle_state == TradeLifecycleState.RECOVERING:
            self.lifecycle_state = TradeLifecycleState.LIVE

    def _force_close(self, _):
        if STATE["open"]:
            close_position_full()
            self.lifecycle_state = TradeLifecycleState.CLOSED
            DASHBOARD_STATE["live_trade_mode"] = False

    def start_trade(self, symbol, side, entry_price, qty, sl, tp1, tp2):
        self.trade_board = TradeManagementBoard() if TRADE_INTELLIGENCE_AVAILABLE else None
        STATE["trade_board"] = {}
        self.lifecycle_state = TradeLifecycleState.OPEN_PENDING_CONFIRMATION
        self.event_bus.emit("lifecycle_change", TradeLifecycleState.OPEN_PENDING_CONFIRMATION)
        log_execution(f"[LIFECYCLE] Trade open requested for {symbol} {side}", "INFO")

    def set_entry_atr(self, entry_atr):
        STATE["entry_atr"] = entry_atr
        # Phase 1: adapt initial SL width to asset-class behaviour while keeping
        # the legacy default (CRYPTO = 1.6) unchanged so no existing behaviour
        # shifts. GOLD/OIL mean-revert faster -> tighter initial SL by default.
        asset_class = AssetBehaviorProfile.resolve_asset_class(STATE.get("current_symbol", ""))
        base_sl_mult = AssetBehaviorProfile.get(asset_class).get("sl_mult", 1.6)
        if STATE["side"] == "BUY":
            STATE["synthetic_sl"] = STATE["entry"] - entry_atr * base_sl_mult
        else:
            STATE["synthetic_sl"] = STATE["entry"] + entry_atr * base_sl_mult
        log_execution(f"[SL_FIXED] Initial SL set to {STATE['synthetic_sl']:.4f} based on entry ATR={entry_atr:.4f} (asset={asset_class}, sl_mult={base_sl_mult})", "INFO")

    # ---- Position Management Engine: advisory layer (Phase 1) ----
    def _ensure_position_profile(self, symbol, entry, side, atr, classification="", trade_type=""):
        """Create/re-wrap the DynamicPositionProfile for the active trade. Called
        at OPEN inside execute_entry. If an existing profile matches this symbol it
        is reused (avoids clobbering mid-trade reclassification)."""
        if self.position_profile is not None and self.position_profile.symbol == symbol:
            return self.position_profile
        asset_class = AssetBehaviorProfile.resolve_asset_class(symbol)
        self.position_profile = DynamicPositionProfile(
            symbol, side, entry, atr, classification=classification,
            trade_type=trade_type, asset_class=asset_class
        )
        STATE["position_profile"] = self.position_profile.to_state_dict()
        return self.position_profile

    def call_block_validation(self):
        """Placeholder technical hook kept for symmetric lifecycle; no-op in Phase 1."""
        return True

    def _update_advisory_profile(self, *, smart_money, momentum, trade_state, trend_health,
                                 struct_shift, structure_aligned, continuation_eval):
        """Re-evaluate trade_type (ADVISORY only) and store it. Does not change
        entry/exit behaviour; simply keeps the classification current so the
        user-visible [POSITION] log reflects the live thesis."""
        profile = self.position_profile
        if profile is None:
            if not STATE.get("open"):
                return None
            # rebuild profile if it was lost (e.g. recovery path)
            profile = self._ensure_position_profile(
                STATE.get("current_symbol", ""), STATE.get("entry", 0.0),
                STATE.get("side", "BUY"), STATE.get("entry_atr", 0.0),
                classification=STATE.get("narrative_classification", ""),
                trade_type=STATE.get("trade_type", "TREND")
            )
            if profile is None:
                return None
        smart = smart_money or {}
        mom = momentum or {}
        profile.update(
            trade_state=trade_state,
            trend_health=trend_health,
            structure_aligned=structure_aligned,
            continuation_probability=continuation_eval.continuation_probability if continuation_eval is not None else 0.5,
            smart_money=smart,
            structure_shift=struct_shift,
            exhaustion_evidence=mom.get("exhaustion_risk", 0) > 60,
            reversal_confirmed=((smart.get("distribution_risk", 0) > 70) and
                                 (mom.get("momentum_decay", False)) and
                                 not structure_aligned)
        )
        STATE["position_profile"] = profile.to_state_dict()
        return profile

    def _advisory_action(self, *, profile, health, roe, mark_price, trade_state,
                         asset_class, regime):
        """Normalise the advisory health ACTION into the user-facing decision
        (HOLD / HOLD_TRAIL / PROTECT_PROFIT / PARTIAL / EXIT). Still advisory:
        no execution happens from here in Phase 1."""
        return health.get("action", "HOLD")

    def _run_advisory_health(self, *, symbol, mark_price, atr, side, entry, roe,
                             smart_money, momentum, trade_state, regime,
                             continuation_eval, trend_health, struct_shift,
                             structure_aligned, tp1_hold_score, exit_warning,
                             news_state="NEWS_NEUTRAL", force=False):
        """Compute the PositionHealthScore + dynamic trade_type (ADVISORY only)
        and emit the rich [POSITION] log. Returns the computed health dict."""
        profile = self._update_advisory_profile(
            smart_money=smart_money, momentum=momentum, trade_state=trade_state,
            trend_health=trend_health, struct_shift=struct_shift,
            structure_aligned=structure_aligned, continuation_eval=continuation_eval
        )
        if profile is None:
            return None
        asset_class = profile.asset_class
        # A NEWS-driven trade is managed and reported under its own regime
        # (NEWS_DRIVEN) for the advisory/health view. This is presentational
        # only: it does not change any entry/exit/risk decision.
        if profile.trade_type == "NEWS":
            regime = "NEWS_DRIVEN"
        thesis_failure = STATE.get("thesis_failure_score", 0.0)
        cont_pressure = STATE.get("continuation_pressure", 50.0)
        zone_strength = STATE.get("zone_strength_score", 0.0)
        drawdown = STATE.get("drawdown_from_peak", 0.0)

        # ---- Phase-3 (G2/G7): advisory signal decoding ----
        # IFVG conflict: an inverse FVG in front of the position argues against
        # the OB/zone thesis -> dampen the zone-health component (never below 0).
        # This is ADVISORY only: the enforceable rules read STATE directly.
        ifvg_state = STATE.get("ifvg_state", {})
        ifvg_blocking = bool(ifvg_state.get("blocking", False)) and bool(ifvg_state.get("has_inverse", False))
        ifvg_pen = float(ifvg_state.get("penalty", 0.0) or 0.0)
        if ifvg_blocking and ifvg_pen > 0:
            zone_strength = zone_strength * (1.0 - min(1.0, ifvg_pen))
        zone_strength = max(0.0, min(100.0, zone_strength))
        STATE["position_ifvg_penalty"] = ifvg_pen if ifvg_blocking else 0.0

        health = self.health_engine.compute(
            profile=profile, trade_state=trade_state, trend_health=trend_health,
            structure_aligned=structure_aligned, adx=STATE.get("adx_live", 20.0),
            smart_money=smart_money, momentum=momentum,
            continuation_probability=continuation_eval.continuation_probability if continuation_eval is not None else 0.5,
            continuation_pressure=cont_pressure, thesis_failure_score=thesis_failure,
            exit_warning=exit_warning, zone_strength=zone_strength,
            roe=roe, drawdown_from_peak=drawdown, news_state=news_state
        )
        action = self._advisory_action(
            profile=profile, health=health, roe=roe, mark_price=mark_price,
            trade_state=trade_state, asset_class=asset_class, regime=regime
        )
        # persist advisory state for dashboards/tests
        STATE["position_health"] = health.get("score", 0.0)
        STATE["position_action"] = action
        STATE["position_health_components"] = health.get("components", {})
        STATE["position_trade_type"] = profile.trade_type
        STATE["position_asset_class"] = asset_class
        STATE["position_confidence"] = health.get("confidence", 0.0)

        # ---- Phase-3 (G2/G4/G7): advisory observations ----
        # HOLD-not-exit: an inverse FVG being retested while the trend is still
        # healthy and aligned is a RETEST, not a reversal trigger. It must never
        # be auto-treated as a normal pullback either, but the HOLD verdict
        # keeps the position alive and the retest monitored.
        closest_retest = (ifvg_state.get("closest") or {}).get("retest", "NONE")
        if ifvg_state.get("has_inverse") and closest_retest in ("PROBING", "SWEEP", "REJECTED") and \
           structure_aligned and trend_health >= 6 and \
           (continuation_eval.continuation_probability if continuation_eval is not None else 0.5) >= 0.6:
            STATE["position_ifvg_hold"] = True
            log_execution(
                f"[IFVG] {symbol} inverse FVG retest '{closest_retest}' on healthy "
                f"trend ({trade_state}) -> HOLD, no exit",
                "INFO", debounce_key=f"ifvg_hold_{symbol}", debounce_sec=30,
            )
        else:
            STATE["position_ifvg_hold"] = bool(STATE.get("position_ifvg_hold", False)) and not structure_aligned
        # RSI/MACD opposition (G2) -> evidence for the dynamic reversal rules.
        rsi_now = float(STATE.get("position_rsi", 50.0))
        macd_hist = float(STATE.get("position_macd_hist", 0.0))
        opposed_rsi = (side == "BUY" and rsi_now < 45) or (side == "SELL" and rsi_now > 55)
        opposed_macd = (side == "BUY" and macd_hist < 0) or (side == "SELL" and macd_hist > 0)
        STATE["position_opposed_momentum"] = bool(opposed_rsi or opposed_macd)
        # G4: directional news is evidence, never an order. Opposed + high-impact
        # headlines dampen risk-health already; here they are recorded + logged
        # without ever forcing a close by themselves.
        _news_ctx = STATE.get("advisory_news_ctx", {}) or {}
        STATE["position_news_opposed"] = bool(_news_ctx.get("opposed", False))
        if _news_ctx.get("opposed", False) and _news_ctx.get("state") in ("NEWS_HIGH", "NEWS_CRITICAL"):
            log_execution(
                f"[NEWS] {symbol} {side} faces opposed {_news_ctx.get('state')} "
                f"news (risk={_news_ctx.get('risk', 0) or 0:.0f}) -> monitoring only",
                "INFO", debounce_key=f"news_opposed_{symbol}", debounce_sec=60,
            )

        action_changed = action != self._last_position_action
        type_changed = profile.trade_type != self._last_position_trade_type
        self._last_position_action = action
        self._last_position_trade_type = profile.trade_type

        log_position_decision(
            symbol, side, asset_class, profile.trade_type, regime, entry,
            mark_price, roe, atr, health, action, health.get("reason", "n/a"),
            health.get("confidence", 0.0),
            force=(force or action_changed or type_changed)
        )
        return health

    # -------------------------------------------------------------------------
    # PHASE 2: Enforceable dynamic profit / reversal / exhaustion rules.
    #
    # This runs AFTER the existing hard guards (thesis failure, hard exit,
    # synthetic SL, TP1/TP2, trailing, PPE). It converts the Phase-1 advisory
    # signals (health + trade_type + asset_class) into REAL actions that are
    # ADDITIVE to the live system:
    #   1. Confirmed Reversal  -> full exit (strong evidence) / partial+runner (medium)
    #   2. Exhaustion (runner) -> protect by ratcheting SL to breakeven
    #   3. ATR/asset-class partial profit-taking for REVERSAL/tight assets
    # Every action is guarded by a once-flag so it fires at most once, exactly
    # like the existing profit_lock/hard_exit guards.
    # -------------------------------------------------------------------------
    def _is_correction(self, *, trade_state, cont, dist_risk, exhaustion_risk,
                       momentum_decay, structure_aligned) -> bool:
        """Distinguish a CORRECTION from the neighbouring regimes so a healthy
        TREND / SNIPER position gets the right management:

          * HEALTHY_PULLBACK  -> shallow dip, structure aligned, continuation
                                 intact  -> HOLD (never close).
          * DISTRIBUTION/EXHAUSTION -> high distribution / exhaustion risk
                                 -> profit-lock / de-risk (defense).
          * HARD FAILURE      -> MOMENTUM_COLLAPSE / PANIC_EXIT -> full exit.
          * CORRECTION        -> deeper/weaker dip than a healthy pullback:
                                 continuation faded + momentum decaying, but
                                 NOT yet a confirmed distribution/exhaustion
                                 or hard failure, and structure shape holding.
                                 Management: bank a partial + protect the rest
                                 (runner + breakeven) instead of holding fully.
        Returns True only for the CORRECTION regime.
        """
        if structure_aligned and cont >= 0.55:
            return False                       # healthy pullback -> hold
        if dist_risk > 50 or exhaustion_risk > 60:
            return False                       # defense/profit-lock regime
        if trade_state in ("MOMENTUM_COLLAPSE", "PANIC_EXIT", "LIQUIDITY_EXHAUSTION",
                           "FAKE_BREAKOUT"):
            return False                       # hard-failure exit regime
        return (momentum_decay or cont < 0.55) and cont < 0.68

    def _apply_dynamic_profit_and_exit_rules(self, *, symbol, mark_price, atr, side,
                                             entry, roe, trade_state, smart_money,
                                             momentum, continuation_eval,
                                             structure_aligned):
        """Enforce dynamic rules. Returns True if the position was closed (the
        caller should then return from _apply_management)."""
        profile = self.position_profile
        if profile is None:
            return False

        asset_class = profile.asset_class
        asset_cfg = AssetBehaviorProfile.get(asset_class)
        trade_type_cur = profile.trade_type
        health_score = STATE.get("position_health", 50.0)
        health_action = STATE.get("position_action", "HOLD")
        confidence = STATE.get("position_confidence", 0.0)
        cont = continuation_eval.continuation_probability if continuation_eval is not None else 0.5
        dist_risk = smart_money.get("distribution_risk", 0) if smart_money else 0
        exhaustion_risk = momentum.get("exhaustion_risk", 0) if momentum else 0
        momentum_decay = momentum.get("momentum_decay", False) if momentum else False

        # ---- Rule 1: Confirmed Reversal exit -------------------------------
        # A trend/runner trade that flips to a confirmed REVERSAL must be let go
        # before it gives back the move. Strong evidence -> full close, medium ->
        # de-risk with a partial + runner, mirroring the user's aggressiveness
        # preference (full on strong, partial on medium).
        if not STATE.get("dynamic_reversal_exit_done", False):
            # Phase-3 (G7): IFVG reversal confluence. An inverse FVG ahead that
            # has been REJECTED/SWEEPED, combined with reverse structure and
            # weak momentum/liquidity, raises the reversal probability. IFVG
            # alone NEVER closes: it only upgrades already-present evidence.
            ifvg_state = STATE.get("ifvg_state", {})
            ifvg_strong = bool(ifvg_state.get("blocking", False)) and \
                          ((ifvg_state.get("closest") or {}).get("retest") in ("REJECTED", "SWEEP"))
            ifvg_weak = bool(ifvg_state.get("blocking", False)) and \
                        (ifvg_state.get("closest") or {}).get("retest") == "PROBING"
            opposed_mom = bool(STATE.get("position_opposed_momentum", False))
            reversal_strong = (trade_state in ("MOMENTUM_COLLAPSE", "PANIC_EXIT")) and \
                              dist_risk > 65 and momentum_decay and not structure_aligned
            combined_strong = ifvg_strong and not structure_aligned and \
                              (momentum.get("momentum_health", 50) < 40 or opposed_mom) and \
                              trade_state in ("MOMENTUM_COLLAPSE", "PANIC_EXIT", "LIQUIDITY_EXHAUSTION")
            reversal_medium = health_action in ("PARTIAL", "EXIT") and \
                              (dist_risk > 50 or exhaustion_risk > 60) and \
                              cont < 0.55 and not structure_aligned
            combined_medium = (ifvg_strong or (ifvg_weak and opposed_mom)) and \
                              not structure_aligned and cont < 0.5 and \
                              (dist_risk > 35 or exhaustion_risk > 45 or momentum_decay) and \
                              roe > 0
            if (reversal_strong or combined_strong) and health_score < 50 and confidence >= 0.7:
                STATE["dynamic_reversal_exit_done"] = True
                new_sl = entry if side == "BUY" else entry
                STATE["synthetic_sl"] = new_sl
                log_execution(
                    f"[DYNAMIC] {trade_type_cur}->REVERSAL FULL EXIT | {symbol} ROE={roe:.2f}% "
                    f"health={health_score:.1f} dist_risk={dist_risk:.0f} state={trade_state} "
                    f"ifvg={'YES' if ifvg_strong else 'no'}",
                    "WARN"
                )
                log_position_decision(
                    symbol, side, asset_class, "REVERSAL",
                    STATE.get("market_regime", "UNKNOWN"), entry, mark_price, roe,
                    atr, {"score": health_score, "components": {}}, "EXIT",
                    "confirmed reversal (strong evidence)", confidence, force=True,
                )
                close_position_full()
                return True
            elif (reversal_medium or combined_medium) and not STATE.get("tp1_hit", False) and roe > 0:
                # Medium evidence: de-risk 50% and convert remainder to a runner
                # with a breakeven guard, rather than a full blind close.
                STATE["dynamic_reversal_exit_done"] = True
                close_partial(0.5)
                STATE["tp1_hit"] = True
                STATE["runner_mode"] = True
                STATE["synthetic_sl"] = entry
                log_execution(
                    f"[DYNAMIC] {trade_type_cur} reversal medium evidence -> "
                    f"partial 50% + runner | {symbol} ROE={roe:.2f}% health={health_score:.1f} "
                    f"ifvg={'YES' if combined_medium else 'no'}",
                    "WARN"
                )
                log_position_decision(
                    symbol, side, asset_class, "REVERSAL",
                    STATE.get("market_regime", "UNKNOWN"), entry, mark_price, roe,
                    atr, {"score": health_score, "components": {}}, "PARTIAL",
                    "medium reversal evidence; de-risk and run", confidence, force=True,
                )

        # ---- Rule 2: Exhaustion protection (runner mode) -------------------
        # When a runner is holding gains but the move is exhausting, protect the
        # realised profit by ratcheting the SL up to breakeven. Fires once.
        if not STATE.get("dynamic_exhaustion_protect_done", False):
            if STATE.get("runner_mode", False) and (
                (exhaustion_risk > 65) or (health_action in ("PARTIAL", "PROTECT_PROFIT") and cont < 0.5)
            ):
                STATE["dynamic_exhaustion_protect_done"] = True
                STATE["synthetic_sl"] = entry  # lock at breakeven
                STATE["trail_stop"] = entry if side == "BUY" else entry
                log_execution(
                    f"[DYNAMIC] Exhaustion protect | {symbol} SL ratcheted to breakeven "
                    f"(exhaustion_risk={exhaustion_risk:.0f}) ROE={roe:.2f}%",
                    "INFO"
                )

        # ---- Rule 3: ATR/asset-class / CORRECTION partial profit-taking -------
        # Reversal & mean-reverting assets (GOLD/OIL) should bank profit sooner.
        # A TREND/SNIPER trade entering a CORRECTION (continuation faded but not
        # yet a distribution/exhaustion/failure) should bank a partial + protect
        # the remainder (runner + breakeven) rather than hold the full position.
        # When TP1 hasn't fired yet and ROE reaches the asset-class ATR target
        # while continuation faded, take a partial and switch to runner.
        if not STATE.get("dynamic_partial_done", False) and not STATE.get("tp1_hit", False):
            roe_target = float(asset_cfg.get("tp1_atr", 2.5)) * (atr / entry) * 100.0
            early_reversal_profit = trade_type_cur in ("REVERSAL", "RETEST") or asset_class in ("GOLD", "OIL")
            is_correction = self._is_correction(
                trade_state=trade_state, cont=cont, dist_risk=dist_risk,
                exhaustion_risk=exhaustion_risk, momentum_decay=momentum_decay,
                structure_aligned=structure_aligned)
            if (early_reversal_profit or is_correction) and roe >= roe_target and cont < 0.62:
                STATE["dynamic_partial_done"] = True
                STATE["tp1_hit"] = True
                STATE["runner_mode"] = True
                close_partial(0.5)
                STATE["synthetic_sl"] = entry
                log_execution(
                    f"[DYNAMIC] Early profit bank ({trade_type_cur}/{asset_class}"
                    f"{'/CORRECTION' if is_correction else ''}) "
                    f"roe={roe:.2f}% >= {roe_target:.2f}% -> partial 50% + runner",
                    "SUCCESS"
                )

        return False

    # FIX: Regime-aware TP1 hold score – do not penalize high ROE in healthy continuation states
    def _compute_tp1_hold_score(self, smart: dict, momentum: dict, adx: float, adx_slope: float,
                                 trade_state: str, continuation_eval: ContinuationEvaluation,
                                 distribution_risk: float, rejection_detected: bool,
                                 failed_breakout: bool, roe: float) -> int:
        score = 0
        if smart.get("smart_money_dominant", False):
            score += 4
        banker = smart.get("banker_pressure", 50)
        retail = smart.get("retailer_pressure", 50)
        if banker > retail + 10:
            score += 3
        cont_strength = momentum.get("continuation_strength", 0)
        if cont_strength > 70:
            score += 4
        mom_health = momentum.get("momentum_health", 50)
        if mom_health > 65:
            score += 2
        if momentum.get("trend_expansion", False):
            score += 3
        if continuation_eval.continuation_probability > 0.75:
            score += 3
        if trade_state in ("TREND_RIDE", "EXPANSION", "ACCUMULATION", "HEALTHY_PULLBACK"):
            score += 3
        if adx > 25 and adx_slope > 0:
            score += 2
        if momentum.get("exhaustion_risk", 0) > 50:
            score -= 5
        if momentum.get("climax_risk", 0) > 60:
            score -= 4
        if distribution_risk > 45:
            score -= 6
        if smart.get("retail_euphoria", False):
            score -= 3
        if momentum.get("momentum_decay", False):
            score -= 4
        if trade_state in ("PROFIT_DEFENSE", "DISTRIBUTION", "LIQUIDITY_EXHAUSTION", "MOMENTUM_COLLAPSE"):
            score -= 5
        if failed_breakout:
            score -= 5
        if rejection_detected:
            score -= 4
        if continuation_eval.continuation_probability < 0.45:
            score -= 5
        # ROE penalty only applies if not in healthy continuation regimes
        if trade_state not in ("TREND_RIDE", "EXPANSION", "ACCUMULATION", "HEALTHY_PULLBACK"):
            if roe > 80:
                score -= 8
            elif roe > 50:
                score -= 4
            elif roe > 20:
                score -= 2
        return max(0, min(20, score))

    def _compute_institutional_exit_warning(self, smart: dict, momentum: dict,
                                             distribution_risk: float, continuation_prob: float,
                                             rejection_detected: bool, adx_slope: float,
                                             di_spread_change: float) -> int:
        warning = 0
        if smart.get("banker_pressure", 50) < 45:
            warning += 1
        if smart.get("retailer_pressure", 50) > 60:
            warning += 1
        if distribution_risk > 50:
            warning += 2
        if momentum.get("exhaustion_risk", 0) > 60:
            warning += 1
        if momentum.get("climax_risk", 0) > 70:
            warning += 1
        if momentum.get("momentum_decay", False):
            warning += 2
        if continuation_prob < 0.5:
            warning += 1
        if rejection_detected:
            warning += 1
        if adx_slope < -2:
            warning += 1
        if di_spread_change < -5:
            warning += 1
        return min(5, warning)

    # FIX: Healthy pullback vs distribution – do not tighten aggressively during HEALTHY_PULLBACK
    def _apply_runner_defense(self, roe: float, peak_roe: float, drawdown: float,
                               exit_warning: int, continuation_prob: float,
                               trail_mult: float, trade_state: str) -> float:
        mult = trail_mult
        if STATE.get("tp1_hit", False):
            mult *= 0.9
            if roe > 50:
                mult *= 0.85
            if drawdown > 12:
                if trade_state in ("TREND_RIDE", "EXPANSION", "ACCUMULATION", "HEALTHY_PULLBACK"):
                    mult *= 0.85
                else:
                    mult *= 0.7
            if exit_warning >= 3:
                if trade_state in ("TREND_RIDE", "EXPANSION", "ACCUMULATION", "HEALTHY_PULLBACK"):
                    mult *= 0.8
                else:
                    mult *= 0.6
            if continuation_prob < 0.55:
                mult *= 0.9
        return max(0.5, min(4.0, mult))

    def _update_peak_profit(self, roe: float, price: float):
        if roe > STATE.get("peak_roe", 0.0):
            STATE["peak_roe"] = roe
            STATE["peak_price"] = price
            STATE["peak_unrealized_pnl"] = STATE.get("unrealized_pnl_usdt", 0.0)
        peak = STATE.get("peak_roe", roe)
        if peak > 0:
            drawdown = peak - roe
            STATE["drawdown_from_peak"] = max(0.0, drawdown)
        else:
            STATE["drawdown_from_peak"] = 0.0

    def manage_live_trade(self):
        if not (STATE.get("open") and STATE.get("current_symbol")):
            if self.lifecycle_state not in (TradeLifecycleState.IDLE, TradeLifecycleState.CLOSED):
                self.lifecycle_state = TradeLifecycleState.IDLE
                DASHBOARD_STATE["live_trade_mode"] = False
            return
        if self.lifecycle_state == TradeLifecycleState.OPEN_PENDING_CONFIRMATION:
            self._set_lifecycle(TradeLifecycleState.LIVE)
        now = time.time()
        roe = STATE.get("roe_pct", 0.0)
        adx = STATE.get("adx_live", 20.0)
        calm_conditions = abs(roe) < 1.5 and 18 < adx < 30
        target_interval = 5 if calm_conditions else 2
        if now - self.last_management_ts < target_interval:
            return
        self.last_management_ts = now
        symbol = STATE["current_symbol"]
        with _TRADE_LOCK:
            if now - self.last_position_sync_ts >= 10:
                self.exchange_sync.reconcile(symbol, STATE)
                self.last_position_sync_ts = now
            self._apply_management(symbol, now)
        self._log_live_status()

    def _log_live_status(self):
        now = time.time()
        if now - self.last_log_ts < 5:
            return
        if not STATE.get("open"):
            return
        roe = STATE.get("roe_pct", 0.0)
        side = STATE.get("side", "?")
        entry = STATE.get("entry", 0.0)
        mark = STATE.get("mark_price", 0.0)
        pnl_usdt = STATE.get("unrealized_pnl_usdt", 0.0)
        margin = STATE.get("margin", 0.0)
        sl = STATE.get("synthetic_sl", 0.0)
        tp1 = STATE.get("synthetic_tp1", 0.0)
        trail = STATE.get("trail_activated", False)
        tp1_hit = STATE.get("tp1_hit", False)
        direction_icon = "🟢" if side == "BUY" else "🔴"
        roe_color = color_pnl(roe)
        pnl_color = GREEN if pnl_usdt >= 0 else RED
        state_str = self.brain.current_trade_state
        log_msg = (f"{BLUE}[LIVE_MGMT]{RESET} {direction_icon} {STATE['current_symbol']} {side} | "
                   f"Entry: {entry:.2f} | Mark: {mark:.2f} | ROE: {roe_color} | "
                   f"PnL: {pnl_color}{pnl_usdt:.2f} USDT{RESET} | Margin: {margin:.2f} | "
                   f"SL: {sl:.2f} | TP1: {tp1:.2f} | Trail: {'✅' if trail else '❌'} | TP1 Hit: {'✅' if tp1_hit else '❌'} | "
                   f"State: {state_str}")
        log_execution(log_msg, "INFO")

    def _apply_management(self, symbol, now):
        if not STATE.get("open"):
            return
        if self.lifecycle_state != TradeLifecycleState.LIVE:
            return

        df_closed = get_ohlcv_safe(symbol, 50)
        if df_closed is None:
            return
        mark_price = STATE.get("mark_price", get_ticker_safe(symbol))
        if not mark_price:
            return

        df_live = get_live_hybrid_df(symbol, df_closed, mark_price)
        atr = compute_atr(df_live).iloc[-1] if len(df_live) > 14 else mark_price * 0.01
        side = STATE["side"]
        entry = STATE["entry"]
        roe = STATE.get("roe_pct", 0.0)

        self._update_peak_profit(roe, mark_price)

        ob = None

        if now - self.last_heavy_calc_ts >= 5:
            plus_di, minus_di, adx_now, adx_slope = get_di_components(df_live)
            if plus_di is None: plus_di = 20.0
            if minus_di is None: minus_di = 20.0
            if adx_now is None: adx_now = 20.0
            if adx_slope is None: adx_slope = 0.0

            pullback_type = trend_engine.analyze_pullback(df_live, side, atr)
            weak_pullback = (pullback_type == "WEAK_PULLBACK")
            counter_displacement = 0.0
            last_candle = df_live.iloc[-1]
            if side == "SELL" and last_candle['close'] > last_candle['open']:
                body = abs(last_candle['close'] - last_candle['open'])
                if body > atr * 0.6:
                    counter_displacement = body / atr
            elif side == "BUY" and last_candle['close'] < last_candle['open']:
                body = abs(last_candle['close'] - last_candle['open'])
                if body > atr * 0.6:
                    counter_displacement = body / atr
            volume_ratio = df_live['volume'].iloc[-1] / df_live['volume'].iloc[-10:-1].mean() if len(df_live) >= 10 else 1.0
            trend_health = trend_engine.get_trend_health(df_live, side)
            struct_shift = detect_structure_shift(df_live)
            structure_aligned = (side == "BUY" and struct_shift == "bullish_shift") or (side == "SELL" and struct_shift == "bearish_shift")
            market_state = {
                "atr": atr,
                "adx": adx_now,
                "adx_slope": adx_slope,
                "di_plus": plus_di,
                "di_minus": minus_di,
                "trend_health": trend_health,
                "weak_pullback": weak_pullback,
                "counter_displacement": counter_displacement,
                "volume_ratio": volume_ratio,
                "df": df_live,
                "last_candle": last_candle,
                "structure_aligned": structure_aligned,
                "continuation_pressure": 50
            }

            smart_money = SmartMoneyEngine.analyze_smart_money(df_live)
            momentum = MomentumFlowEngine.analyze_momentum_flow(df_live)

            regime = self.regime_classifier.classify(df_live, ob)
            trade_state = self.brain.update(smart_money, momentum, adx_now, regime)
            STATE["trade_state"] = trade_state
            STATE["smart_trail_mult"] = self.brain.get_trail_multiplier()
            STATE["delay_tp1"] = self.brain.should_delay_tp1()
            STATE["adx_live"] = adx_now
            STATE["di_plus_live"] = plus_di
            STATE["di_minus_live"] = minus_di
            STATE["smart_money"] = smart_money
            STATE["momentum_flow"] = momentum
            STATE["market_regime"] = regime

            # ---- Phase-3 (G2/G7): advisory signal stack ----
            # RSI / MACD-histogram / volume-regime / VWAP context plus the
            # live OB-zone strength feed the advisory health and the dynamic
            # rules. They are PERSISTED into STATE so the non-heavy path reuses
            # the same values (recomputed only every 5s like the rest).
            try:
                STATE["position_rsi"] = float(compute_rsi(df_live).iloc[-1])
            except Exception:
                STATE.setdefault("position_rsi", 50.0)
            try:
                _, _, macd_hist = compute_macd(df_live)
                STATE["position_macd_hist"] = float(macd_hist.iloc[-1])
            except Exception:
                STATE.setdefault("position_macd_hist", 0.0)
            try:
                STATE["position_vol_state"] = classify_volume(df_live)
            except Exception:
                STATE.setdefault("position_vol_state", "NORMAL")
            try:
                vw = vwap_features(df_live)
                STATE["position_vwap_dist"] = float(vw.get("distance", 0.0))
                STATE["position_vwap_slope"] = float(vw.get("slope", 0.0))
            except Exception:
                STATE.setdefault("position_vwap_dist", 0.0)
                STATE.setdefault("position_vwap_slope", 0.0)
            try:
                zmap = get_smart_zones(STATE["current_symbol"], df_live, None)
                zstock = (zmap.get("sell_zones") if side == "SELL" else zmap.get("buy_zones")) or []
                if zstock and float(zstock[0].get("strength", 0) or 0) > 0:
                    STATE["zone_strength_score"] = float(min(100.0, max(0.0, float(zstock[0]["strength"]) * 10.0)))
            except Exception:
                STATE.setdefault("zone_strength_score", 0.0)
            # IFVG warning payload for the live position (warning-only engine).
            STATE["ifvg_state"] = ifvg_warning_payload(side, df_live, atr, mark_price)

            thesis_dict = STATE.get("trade_thesis", {})
            cont_pressure_score, cont_pressure_reasons = self.continuation_pressure_engine.calculate_pressure(df_live, side, entry, atr, STATE.get("entry_time", time.time()))
            market_state["continuation_pressure"] = cont_pressure_score
            continuation_eval = _continuation_engine.evaluate(side, df_live, market_state, thesis_dict)
            STATE["continuation_probability"] = continuation_eval.continuation_probability
            STATE["hold_quality"] = continuation_eval.hold_quality
            STATE["counter_pressure"] = continuation_eval.counter_pressure
            STATE["reclaim_risk"] = continuation_eval.reclaim_risk
            STATE["trend_strength"] = continuation_eval.trend_strength
            STATE["continuation_reasons"] = continuation_eval.reasons
            STATE["continuation_pressure"] = cont_pressure_score

            failed, failure_reasons, failure_score = self.thesis_failure_engine.evaluate_failure(thesis_dict, market_state, mark_price, entry, side)
            STATE["thesis_failure_score"] = failure_score
            if failed:
                log_execution(f"[THESIS_FAILURE] Thesis failed for {symbol}: {failure_reasons}", "WARN")
                if roe > 0:
                    close_partial(0.5)
                    STATE["synthetic_sl"] = entry
                else:
                    close_position_full()
                    self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
                    DASHBOARD_STATE["live_trade_mode"] = False
                    return

            old_conf = STATE.get("current_confidence", 50.0)
            di_spread_change = (plus_di - minus_di) - STATE.get("prev_di_spread", 0)
            new_conf = self.confidence_engine.update_live_confidence(old_conf, cont_pressure_score, failure_score, adx_slope, di_spread_change)
            new_conf = ConfidenceEngine.apply_institutional_modifiers(new_conf, smart_money, momentum, continuation_eval.continuation_probability * 100)
            STATE["current_confidence"] = new_conf
            STATE["prev_di_spread"] = plus_di - minus_di

            rejection_bull, _ = RejectionIntelligence.is_bullish_rejection(df_live, atr)
            rejection_bear, _ = RejectionIntelligence.is_bearish_rejection(df_live, atr)
            rejection_detected = (side == "BUY" and rejection_bull) or (side == "SELL" and rejection_bear)
            failed_breakout = (trade_state == "FAKE_BREAKOUT") or (abs(continuation_eval.continuation_probability - 0.5) < 0.1 and roe < 2)

            tp1_hold_score = self._compute_tp1_hold_score(
                smart_money, momentum, adx_now, adx_slope, trade_state,
                continuation_eval, smart_money.get("distribution_risk", 0),
                rejection_detected, failed_breakout, roe
            )
            STATE["tp1_hold_score"] = tp1_hold_score

            exit_warning = self._compute_institutional_exit_warning(
                smart_money, momentum, smart_money.get("distribution_risk", 0),
                continuation_eval.continuation_probability, rejection_detected,
                adx_slope, di_spread_change
            )
            STATE["exit_warning"] = exit_warning

            # Persist advisory inputs so the non-heavy path can reuse them too.
            STATE["advisory_trend_health"] = trend_health
            STATE["advisory_struct_shift"] = struct_shift
            STATE["advisory_structure_aligned"] = structure_aligned

            self.last_heavy_calc_ts = now
        else:
            adx_now = STATE.get("adx_live", 20.0)
            plus_di = STATE.get("di_plus_live", 20.0)
            minus_di = STATE.get("di_minus_live", 20.0)
            smart_money = STATE.get("smart_money", {})
            momentum = STATE.get("momentum_flow", {})
            trade_state = STATE.get("trade_state", "RANGE_CHOP")
            trend_health = STATE.get("advisory_trend_health", 5.0)
            struct_shift = STATE.get("advisory_struct_shift", None)
            structure_aligned = STATE.get("advisory_structure_aligned", False)
            continuation_eval = ContinuationEvaluation(
                continuation_probability=STATE.get("continuation_probability", 0.5),
                trend_strength=STATE.get("trend_strength", 0.5),
                exhaustion_probability=0.0,
                reclaim_risk=STATE.get("reclaim_risk", 0.0),
                counter_pressure=STATE.get("counter_pressure", 0.0),
                confidence=0.5,
                reasons=STATE.get("continuation_reasons", []),
                should_hold=STATE.get("continuation_probability", 0.5) >= 0.62,
                hold_quality=STATE.get("hold_quality", "UNKNOWN")
            )
            tp1_hold_score = STATE.get("tp1_hold_score", 10)
            exit_warning = STATE.get("exit_warning", 0)

        # Trade Management Board: one structured observer of the live thesis.
        # It classifies pullback/micro-pullback/retest/continuation/distribution
        # and trade style without becoming a second execution authority.
        try:
            if self.trade_board is None and TRADE_INTELLIGENCE_AVAILABLE:
                self.trade_board = TradeManagementBoard()
            if self.trade_board is not None and TRADE_INTELLIGENCE_AVAILABLE:
                board_snapshot = analyze_setup(df_live, side, entry, atr, symbol)
                board = self.trade_board.evaluate(
                    board_snapshot, roe=roe,
                    thesis_failure=float(STATE.get("thesis_failure_score", 0) or 0),
                    continuation_probability=float(continuation_eval.continuation_probability),
                    distribution_risk=float(smart_money.get("distribution_risk", 0) or 0),
                    drawdown_from_peak=float(STATE.get("drawdown_from_peak", 0) or 0),
                )
                STATE["trade_board"] = board
                STATE["trade_intelligence"] = board_snapshot
                STATE["trade_style"] = board_snapshot.get("trade_style", STATE.get("trade_style", "SCALP"))
                STATE["entry_timing"] = board_snapshot.get("timing", STATE.get("entry_timing", "WAIT_RETEST"))
                STATE["market_phase"] = board_snapshot.get("phase", STATE.get("market_phase", "UNKNOWN"))
                STATE["zone_behaviour"] = board_snapshot.get("behaviour", STATE.get("zone_behaviour", "NEUTRAL"))
                log_execution(
                    f"[TRADE_BOARD] {symbol} {side} | STYLE={STATE['trade_style']} "
                    f"STAGE={board.get('stage')} VERDICT={board.get('verdict')} "
                    f"TIMING={STATE['entry_timing']} PHASE={STATE['market_phase']} "
                    f"ZONE={STATE['zone_behaviour']}",
                    "INFO", debounce_key=f"board_{symbol}", debounce_sec=10,
                )
        except Exception as _board_err:
            log_execution(f"[TRADE_BOARD] intelligence error: {_board_err}", "WARN")

        # Position Management Engine (Phase 1) - ADVISORY health + classification.
        # Computes and stores PositionHealthScore + dynamic trade_type and emits
        # the rich [POSITION] log, WITHOUT changing entry/exit behaviour.
        try:
            STATE["advisory_news_ctx"] = _advisory_news_context(symbol, side)
            self._run_advisory_health(
                symbol=symbol, mark_price=mark_price, atr=atr, side=side, entry=entry,
                roe=roe, smart_money=smart_money, momentum=momentum,
                trade_state=trade_state, regime=STATE.get("market_regime", "UNKNOWN"),
                continuation_eval=continuation_eval, trend_health=trend_health,
                struct_shift=struct_shift, structure_aligned=structure_aligned,
                tp1_hold_score=tp1_hold_score, exit_warning=exit_warning,
                news_state=_get_advisory_news_state(symbol)
            )
        except Exception as _e:
            log_execution(f"[POSITION] advisory health calculation error: {_e}", "WARN")

        if now - self.last_live_debug_ts >= 5:
            self.last_live_debug_ts = now
            log_execution(
                f"[LIVE_DEBUG] {symbol} | ADX={adx_now:.1f} | DI+={plus_di:.1f} | DI-={minus_di:.1f} | "
                f"ContProb={continuation_eval.continuation_probability:.2f} | MomHealth={momentum.get('momentum_health', 50):.1f} | "
                f"DistRisk={smart_money.get('distribution_risk', 0):.1f} | TradeState={trade_state} | "
                f"TrailMult={self.brain.get_trail_multiplier():.2f} | TP1Delay={self.brain.should_delay_tp1()} | "
                f"TrailActive={STATE.get('trail_activated', False)} | SyntheticSL={STATE.get('synthetic_sl', 0):.4f} | ROE={roe:.2f}% | "
                f"TP1HoldScore={tp1_hold_score} | ExitWarning={exit_warning}",
                "INFO"
            )

        if self.brain.should_aggressive_profit_lock() and not STATE.get("profit_lock_activated", False):
            log_execution(f"[PROFIT_LOCK] Aggressive profit lock triggered (state={trade_state})", "WARN")
            if not STATE.get("tp1_hit", False):
                close_partial(0.5)
                STATE["tp1_hit"] = True
                STATE["runner_mode"] = True
            STATE["profit_lock_activated"] = True

        if self.brain.should_hard_exit():
            log_execution(f"[HARD_EXIT] Hard exit triggered (state={trade_state})", "ERROR")
            close_position_full()
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
            DASHBOARD_STATE["live_trade_mode"] = False
            return

        entry_atr = STATE.get("entry_atr", atr)
        base_sl_mult = 1.6
        if smart_money.get("distribution_risk", 0) > 45:
            base_sl_mult = 1.2
        elif smart_money.get("distribution_risk", 0) > 65:
            base_sl_mult = 0.8
        if side == "BUY":
            synthetic_sl = entry - entry_atr * base_sl_mult
            if STATE.get("tp1_hit", False):
                synthetic_sl = max(synthetic_sl, entry)
        else:
            synthetic_sl = entry + entry_atr * base_sl_mult
            if STATE.get("tp1_hit", False):
                synthetic_sl = min(synthetic_sl, entry)
        STATE["synthetic_sl"] = synthetic_sl

        if (side == "BUY" and mark_price <= synthetic_sl) or (side == "SELL" and mark_price >= synthetic_sl):
            log_execution(f"[SYNTHETIC_SL] Hit at {mark_price:.4f} (SL={synthetic_sl:.4f})", "WARN")
            close_position_full()
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
            DASHBOARD_STATE["live_trade_mode"] = False
            return

        if not STATE.get("tp1_hit", False):
            if tp1_hold_score >= 8:
                log_execution(f"[TP1_DELAY] Hold score {tp1_hold_score} >= 8, delaying TP1", "INFO")
                # G1: prefer the persisted ATR-dynamic TP1 target (set at entry by
                # execute_entry) over the legacy fixed 4% reference.
                tp1_price = STATE.get("dynamic_tp1", entry * (1 + 0.04) if side == "BUY" else entry * (1 - 0.04))
                STATE["synthetic_tp1"] = tp1_price
            else:
                log_execution(f"[TP1_EXECUTE] Hold score {tp1_hold_score} < 8, executing TP1 partial close", "SUCCESS")
                close_partial(0.5)
                STATE["tp1_hit"] = True
                STATE["synthetic_sl"] = entry
                tg_tp_hit(symbol, 1, roe)
                STATE["runner_mode"] = True
                STATE["trail_activated"] = True
                self._update_peak_profit(roe, mark_price)

        if STATE.get("tp1_hit", False):
            peak_roe = STATE.get("peak_roe", roe)
            drawdown = STATE.get("drawdown_from_peak", 0.0)
            base_trail_mult = self.brain.get_trail_multiplier()
            adjusted_mult = self._apply_runner_defense(roe, peak_roe, drawdown, exit_warning,
                                                        continuation_eval.continuation_probability,
                                                        base_trail_mult, trade_state)
            STATE["smart_trail_mult"] = adjusted_mult

            tp2_pct = 0.05
            if continuation_eval.continuation_probability > 0.8:
                tp2_pct = 0.08
            # G1: ATR-aware TP2 — asset profile tp2_atr scales (never tightens)
            # the hard-coded percent target with the real volatility at entry.
            _tp2_cfg = AssetBehaviorProfile.get(STATE.get("position_asset_class") or "CRYPTO")
            tp2_roe_target = float(_tp2_cfg.get("tp2_atr", 4.0)) * (atr / entry) * 100.0
            if tp2_roe_target > 0:
                tp2_pct = max(tp2_pct, min(0.15, tp2_roe_target))
            tp2_price = entry * (1 + tp2_pct) if side == "BUY" else entry * (1 - tp2_pct)
            STATE["tp2_price"] = tp2_price
            if not STATE.get("tp2_hit", False):
                if (side == "BUY" and mark_price >= tp2_price) or (side == "SELL" and mark_price <= tp2_price):
                    log_execution(f"[SYNTHETIC_TP2] Hit at {mark_price:.4f}", "SUCCESS")
                    close_position_full()
                    STATE["tp2_hit"] = True
                    self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
                    DASHBOARD_STATE["live_trade_mode"] = False
                    return

        trail_mult = STATE.get("smart_trail_mult", 1.5)
        if smart_money.get("distribution_risk", 0) > 45:
            trail_mult *= 0.7
        if momentum.get("momentum_health", 50) < 30:
            trail_mult *= 0.8
        if continuation_eval.continuation_probability > 0.8:
            trail_mult *= 1.2

        # ---- Phase-3 (G3): runner_bias activation ----
        # The asset-class runner bias (defined but previously unused) tunes how
        # much room a runner keeps while in runner mode: high-bias assets
        # (CRYPTO 1.0) keep a wider trail to let the move run; low-bias assets
        # (GOLD/OIL 0.6, NEWS 0.5) bank profit sooner with a tighter trail.
        asset_cfg_run = AssetBehaviorProfile.get(STATE.get("position_asset_class") or "CRYPTO")
        runner_bias = float(asset_cfg_run.get("runner_bias", 1.0))
        STATE["position_runner_bias"] = runner_bias
        if STATE.get("runner_mode", False) and runner_bias:
            trail_mult = min(4.5, trail_mult * (0.9 + 0.2 * runner_bias))

        trail_mult = max(0.5, min(4.5, trail_mult))

        # ---- Dynamic trailing (Phase 2): asset-class + trade_type aware ------
        # The trailing stop no longer activates at a fixed 1.5% ROE. It uses the
        # asset-class activation threshold (GOLD/OIL activate earlier ~0.8%;
        # CRYPTO keeps the legacy 1.5%) and REVERSAL trades trail even earlier
        # since they must be captured quickly. The base multiplier also blends
        # the asset-class behaviour (tight assets get a tighter trail).
        asset_cfg = AssetBehaviorProfile.get(STATE.get("position_asset_class") or "CRYPTO")
        trade_type_cur = STATE.get("position_trade_type") or (self.position_profile.trade_type if self.position_profile else "TREND")
        trail_activate_roe = float(asset_cfg.get("roe_trail_activate", 1.5))
        if trade_type_cur == "REVERSAL":
            trail_activate_roe = min(trail_activate_roe, float(asset_cfg.get("roe_trail_activate", 1.5)) * 0.8)
        asset_trail_base = float(asset_cfg.get("trail_mult", 3.0))
        # Tight assets trail closer (smaller ATR multiple); never loosen beyond base.
        trail_mult = trail_mult * max(0.55, min(1.0, asset_trail_base / 3.0))

        if roe > trail_activate_roe:
            if not STATE.get("trail_activated", False):
                STATE["trail_activated"] = True
                STATE["trail_stop"] = synthetic_sl
                log_execution(f"[TRAIL] Activated (roe={roe:.2f}% >= {trail_activate_roe:.2f}%, {trade_type_cur}/{STATE.get('position_asset_class')}) with multiplier {trail_mult}", "INFO")
            if side == "BUY":
                new_trail = mark_price - trail_mult * atr
                if new_trail > STATE.get("trail_stop", 0):
                    STATE["trail_stop"] = new_trail
            else:
                new_trail = mark_price + trail_mult * atr
                if new_trail < STATE.get("trail_stop", float('inf')):
                    STATE["trail_stop"] = new_trail
            if (side == "BUY" and mark_price <= STATE.get("trail_stop", 0)) or (side == "SELL" and mark_price >= STATE.get("trail_stop", float('inf'))):
                log_execution(f"[TRAIL] Stop hit at {mark_price:.4f} (trail={STATE['trail_stop']:.4f})", "WARN")
                close_position_full()
                self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
                DASHBOARD_STATE["live_trade_mode"] = False
                return
        else:
            pass

        if USE_PPE:
            state_ppe = {
                "symbol": symbol,
                "trail_active": STATE.get("trail_activated", False),
                "tp1_done": STATE.get("tp1_hit", False),
                "tp1_hit": STATE.get("tp1_hit", False),
                "runner_mode": STATE.get("runner_mode", False),
                "max_price": STATE.get("max_price", entry),
                "min_price": STATE.get("min_price", entry),
                "sl": STATE.get("synthetic_sl", 0.0),
                "trail_stop": STATE.get("trail_stop", 0.0),
                "remaining_qty": STATE.get("remaining_qty", STATE["qty"]),
                "smart_trail_mult": trail_mult,
                "smart_money": smart_money,
                "momentum_flow": momentum,
                "profit_lock_activated": STATE.get("profit_lock_activated", False),
                "trail_tightened": STATE.get("trail_tightened", False)
            }
            idx = len(df_live) - 1
            action, new_sl, new_trail = apply_50_50_profit_engine(
                df_live, idx, mark_price, atr, side, entry, state_ppe, roe,
                trade_state=trade_state,
                continuation_probability=continuation_eval.continuation_probability
            )
            if action == "EXIT" and state_ppe.get("smart_money", {}).get("distribution_risk", 0) > 65 and momentum.get("momentum_decay", False):
                log_execution("[PPE] Institutional exit signal – closing position", "WARN")
                close_position_full()
                self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
                DASHBOARD_STATE["live_trade_mode"] = False
                return

        # ---- Phase 2: enforceable dynamic profit/reversal/exhaustion rules ----
        # Runs after every existing guard so it only ADDS behaviour (dynamic
        # reversal exit, exhaustion protection, early asset-class profit-taking).
        try:
            if self._apply_dynamic_profit_and_exit_rules(
                symbol=symbol, mark_price=mark_price, atr=atr, side=side, entry=entry,
                roe=roe, trade_state=trade_state, smart_money=smart_money,
                momentum=momentum, continuation_eval=continuation_eval,
                structure_aligned=structure_aligned,
            ):
                self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
                DASHBOARD_STATE["live_trade_mode"] = False
                return
        except Exception as _e:
            log_execution(f"[DYNAMIC] rules error: {_e}", "WARN")

        if (side == "BUY" and mark_price <= STATE.get("synthetic_sl", 0)) or (side == "SELL" and mark_price >= STATE.get("synthetic_sl", 0)):
            close_position_full()
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
            DASHBOARD_STATE["live_trade_mode"] = False
            return

_event_bus = EventBus()
_exchange_sync = ExchangeSyncService(_event_bus)
_recovery_guard = RecoveryGuard(_event_bus, _exchange_sync)
_live_manager = LiveTradeManager(_event_bus, _exchange_sync, _recovery_guard)

def sync_position_state(symbol=None):
    if PAPER_MODE:
        if STATE.get("open"):
            price = get_ticker_safe(STATE["current_symbol"])
            if price:
                raw_pnl = (price - STATE["entry"])/STATE["entry"]*100 if STATE["side"]=="BUY" else (STATE["entry"]-price)/STATE["entry"]*100
                roe_pct = raw_pnl * LEVERAGE
                STATE["roe_pct"] = roe_pct
                STATE["mark_price"] = price
                STATE["unrealized_pnl_usdt"] = (price - STATE["entry"]) * STATE["qty"] if STATE["side"]=="BUY" else (STATE["entry"] - price) * STATE["qty"]
                return price, 0.0, 0.0, roe_pct
        return None, None, None, None

    if not symbol and STATE.get("open"):
        symbol = STATE["current_symbol"]
    if not symbol:
        return None, None, None, None

    snap = _exchange_sync.fetch_live_snapshot(symbol)
    if snap is None:
        if STATE.get("open"):
            log_execution(f"[POS_SYNC] Position closed externally on {symbol}, cleaning state", "WARN")
            with _TRADE_LOCK:
                STATE["open"] = False
                TRADE_STATE["in_position"] = False
                _live_manager.lifecycle_state = TradeLifecycleState.CLOSED
                DASHBOARD_STATE["live_trade_mode"] = False
        return None, None, None, None

    with _TRADE_LOCK:
        if not STATE.get("open"):
            STATE["open"] = True
            STATE["side"] = snap.side
            STATE["entry"] = snap.entry_price
            STATE["qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["current_symbol"] = symbol
            STATE["entry_time"] = time.time()
            TRADE_STATE.update({
                "in_position": True,
                "symbol": symbol,
                "side": snap.side,
                "entry": snap.entry_price,
                "qty": snap.qty,
                "last_update_ts": time.time()
            })
            _live_manager.start_trade(symbol, snap.side, snap.entry_price, snap.qty, 0.0, 0.0, 0.0)
        else:
            STATE["entry"] = snap.entry_price
            STATE["qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["side"] = snap.side
            TRADE_STATE.update({
                "entry": snap.entry_price,
                "qty": snap.qty,
                "side": snap.side
            })

        STATE["margin"] = snap.margin
        STATE["unrealized_pnl_usdt"] = snap.unrealized_pnl
        STATE["roe_pct"] = snap.roe_pct
        STATE["leverage"] = snap.leverage
        STATE["mark_price"] = snap.mark_price
        STATE["liquidation_price"] = snap.liquidation_price

    return snap.mark_price, snap.unrealized_pnl, snap.margin, snap.roe_pct

def get_realized_pnl_for_symbol(symbol, lookback_seconds=30):
    if PAPER_MODE:
        return 0.0, 0.0
    try:
        sym = normalize_symbol(symbol)
        since = int((time.time() - lookback_seconds) * 1000)
        trades = safe_api_call(ex.fetch_my_trades, sym, limit=100, params={'since': since})
        if not trades:
            return 0.0, 0.0
        pnl_usdt = 0.0
        for trade in trades:
            side = trade['side'].lower()
            qty = trade['amount']
            price = trade['price']
            cost = qty * price
            if side == 'buy':
                pnl_usdt -= cost
            else:
                pnl_usdt += cost
        balance = get_balance_safe()
        pnl_pct = (pnl_usdt / balance * 100) if balance > 0 else 0.0
        return pnl_usdt, pnl_pct
    except Exception as e:
        log_execution(f"[REALIZED_PNL] Error: {e}", "WARN")
        return 0.0, 0.0

# ========== INDICATORS ==========
def rma(series, period):
    return series.ewm(alpha=1/period, adjust=False).mean()

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_atr(df, period=14):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < period+1:
        return pd.Series([0.0]*len(df)) if df is not None and hasattr(df, '__len__') else pd.Series([0.0])
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = rma(tr, period)
    atr = atr.bfill().ffill().fillna(tr.mean())
    atr = atr.clip(lower=1e-8)
    return atr

def compute_adx(df, period=14, return_di=False):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < period*2:
        empty = pd.Series([0.0]*len(df)) if df is not None and hasattr(df, '__len__') else pd.Series([0.0])
        if return_di:
            return empty, empty, empty
        return empty
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = rma(tr, period) + 1e-9
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)
    plus_di = 100 * rma(plus_dm, period) / (atr + 1e-9)
    minus_di = 100 * rma(minus_dm, period) / (atr + 1e-9)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
    adx = rma(dx, period)
    adx = adx.bfill().ffill().fillna(0).clip(0, 100)
    if return_di:
        return adx, plus_di, minus_di
    return adx

def compute_rsi(df, period=14):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < period+1:
        return pd.Series([50.0]*len(df)) if df is not None and hasattr(df, '__len__') else pd.Series([50.0])
    close = df['close']
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain, period) + 1e-9
    avg_loss = rma(loss, period) + 1e-9
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.bfill().ffill().fillna(50).clip(0, 100)
    return rsi

def compute_macd(df, fast=12, slow=26, signal=9):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series([0.0]), pd.Series([0.0]), pd.Series([0.0])
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def macd_first_flip(hist):
    if hist is None or len(hist) < 2:
        return False
    try:
        return hist.iloc[-2] < 0 and hist.iloc[-1] > 0
    except:
        return False

def volume_pressure_real(df, window=20, threshold=1.2):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < window + 1:
        return False
    vol = df['volume']
    mean = vol.rolling(window).mean().iloc[-1]
    std = vol.rolling(window).std().iloc[-1]
    if std == 0:
        return False
    z = (vol.iloc[-1] - mean) / std
    return z > threshold

def flow_engine(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
        return "neutral"
    last = df.iloc[-1]
    body = last['close'] - last['open']
    vol = last['volume']
    avg_vol = df['volume'].rolling(20).mean().iloc[-1] if len(df) >= 20 else vol
    if vol > avg_vol * 1.5:
        if body > 0:
            return "aggressive_buy"
        else:
            return "aggressive_sell"
    if vol > avg_vol and abs(body) < (last['high'] - last['low']) * 0.3:
        return "absorption"
    return "neutral"

def orderbook_imbalance(ob, depth=10):
    if not ob or 'bids' not in ob or 'asks' not in ob:
        return 0.0
    bids_sum = sum([b[1] for b in ob['bids'][:depth]]) if ob['bids'] else 0
    asks_sum = sum([a[1] for a in ob['asks'][:depth]]) if ob['asks'] else 0
    total = bids_sum + asks_sum
    if total == 0:
        return 0.0
    return (bids_sum - asks_sum) / total

def detect_walls(ob, depth=10, threshold=3.0):
    if not ob or 'bids' not in ob or 'asks' not in ob:
        return False, False
    bid_sizes = [b[1] for b in ob['bids'][:depth]]
    ask_sizes = [a[1] for a in ob['asks'][:depth]]
    if bid_sizes:
        avg_bid = sum(bid_sizes) / len(bid_sizes)
        bid_wall = any(s > avg_bid * threshold for s in bid_sizes)
    else:
        bid_wall = False
    if ask_sizes:
        avg_ask = sum(ask_sizes) / len(ask_sizes)
        ask_wall = any(s > avg_ask * threshold for s in ask_sizes)
    else:
        ask_wall = False
    return bid_wall, ask_wall

def is_late_move(df, atr, multiplier=1.5):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 1 or atr <= 0:
        return False
    last = df.iloc[-1]
    candle_range = last['high'] - last['low']
    return candle_range > multiplier * atr

def early_score(df, ob, atr, side):
    score = 0
    reasons = []
    macd, signal, hist = compute_macd(df)
    if macd_first_flip(hist):
        score += 2
        reasons.append("macd_flip")
    if volume_pressure_real(df):
        score += 2
        reasons.append("volume_pressure")
    flow = flow_engine(df)
    if side == "BUY" and flow == "aggressive_buy":
        score += 2
        reasons.append("flow_buy")
    elif side == "SELL" and flow == "aggressive_sell":
        score += 2
        reasons.append("flow_sell")
    elif flow == "absorption":
        reasons.append("absorption")
    obi = orderbook_imbalance(ob, depth=10)
    if side == "BUY" and obi > 0.2:
        score += 2
        reasons.append(f"obi_bullish_{obi:.2f}")
    elif side == "SELL" and obi < -0.2:
        score += 2
        reasons.append(f"obi_bearish_{obi:.2f}")
    bid_wall, ask_wall = detect_walls(ob, depth=10, threshold=3.0)
    if side == "BUY" and bid_wall:
        score += 1
        reasons.append("bid_wall")
    elif side == "SELL" and ask_wall:
        score += 1
        reasons.append("ask_wall")
    if is_late_move(df, atr, multiplier=1.5):
        score -= 3
        reasons.append("late_move_penalty")
    return score, reasons

# ========== RF ENGINE ==========
class RFEngine:
    def __init__(self, period=20, multiplier=3.5):
        self.period = period
        self.multiplier = multiplier

    def ema(self, s, length):
        return s.ewm(span=length, adjust=False).mean()

    def rng_size(self, x):
        n = self.period
        qty = self.multiplier
        wper = (n * 2) - 1
        avrng = self.ema((x - x.shift(1)).abs(), n)
        return self.ema(avrng, wper) * qty

    def rng_filt(self, x, rng):
        filt = np.zeros(len(x))
        hi = np.zeros(len(x))
        lo = np.zeros(len(x))
        for i in range(len(x)):
            if i == 0:
                filt[i] = x.iloc[i]
            else:
                prev = filt[i - 1]
                r = rng.iloc[i]
                if x.iloc[i] - r > prev:
                    filt[i] = x.iloc[i] - r
                elif x.iloc[i] + r < prev:
                    filt[i] = x.iloc[i] + r
                else:
                    filt[i] = prev
            hi[i] = filt[i] + rng.iloc[i]
            lo[i] = filt[i] - rng.iloc[i]
        return pd.Series(hi, index=x.index), pd.Series(lo, index=x.index), pd.Series(filt, index=x.index)

    def compute(self, df, src="close"):
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return {"signal": None, "triggered": False, "filt": 0, "h_band": 0, "l_band": 0, "distance": 0}
        x = df[src]
        rng = self.rng_size(x)
        h, l, filt = self.rng_filt(x, rng)
        fdir = np.zeros(len(filt))
        for i in range(1, len(filt)):
            if filt.iloc[i] > filt.iloc[i - 1]:
                fdir[i] = 1
            elif filt.iloc[i] < filt.iloc[i - 1]:
                fdir[i] = -1
            else:
                fdir[i] = fdir[i - 1]
        longCond = (x > filt) & (pd.Series(fdir) == 1)
        shortCond = (x < filt) & (pd.Series(fdir) == -1)
        CondIni = np.zeros(len(x))
        for i in range(1, len(x)):
            if longCond.iloc[i]:
                CondIni[i] = 1
            elif shortCond.iloc[i]:
                CondIni[i] = -1
            else:
                CondIni[i] = CondIni[i - 1]
        longSignal = longCond & (pd.Series(CondIni).shift(1) == -1)
        shortSignal = shortCond & (pd.Series(CondIni).shift(1) == 1)
        signal = None
        if longSignal.iloc[-1]:
            signal = "BUY"
        elif shortSignal.iloc[-1]:
            signal = "SELL"
        triggered = bool(longSignal.iloc[-1] or shortSignal.iloc[-1])
        distance = (x.iloc[-1] - filt.iloc[-1]) / x.iloc[-1] if x.iloc[-1] != 0 else 0
        return {
            "signal": signal,
            "triggered": triggered,
            "filt": filt.iloc[-1],
            "h_band": h.iloc[-1],
            "l_band": l.iloc[-1],
            "distance": distance
        }

# ========== ADVANCED CANDLE INTELLIGENCE ==========
def candle_metrics(candle):
    body = abs(candle['close'] - candle['open'])
    range_ = candle['high'] - candle['low']
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    return body, range_, upper_wick, lower_wick

def is_pinbar(candle, atr, side, body_atr_min=0.5, wick_body_ratio=2.5, wick_range_ratio=0.6):
    body, range_, upper_wick, lower_wick = candle_metrics(candle)
    if range_ == 0 or atr <= 0:
        return False
    if side == "BUY":
        return (lower_wick >= wick_body_ratio * body and
                lower_wick / range_ >= wick_range_ratio and
                body / atr >= body_atr_min)
    else:
        return (upper_wick >= wick_body_ratio * body and
                upper_wick / range_ >= wick_range_ratio and
                body / atr >= body_atr_min)

def classify_volume(df, period=20, expansion_threshold=1.8, normal_threshold=1.3, exhaustion_threshold=0.7):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < period + 1:
        return "neutral"
    vol = df['volume']
    avg_vol = vol.rolling(period).mean().iloc[-1]
    if avg_vol == 0:
        return "neutral"
    ratio = vol.iloc[-1] / avg_vol
    if ratio > expansion_threshold:
        return "expansion"
    elif ratio > normal_threshold:
        return "normal"
    elif ratio < exhaustion_threshold:
        return "exhaustion"
    else:
        return "neutral"

def detect_displacement(df, side, atr, volume_state, body_atr_threshold=0.8, volume_expansion_required=False):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
        return False
    last = df.iloc[-1]
    body, range_, _, _ = candle_metrics(last)
    if body / atr < body_atr_threshold:
        return False
    if side == "BUY" and last['close'] <= last['open']:
        return False
    if side == "SELL" and last['close'] >= last['open']:
        return False
    if volume_expansion_required and volume_state != "expansion":
        return False
    return True

def detect_location(df, price, supports, resistances, threshold=0.003):
    near_support = False
    near_resistance = False
    if supports:
        min_dist_sup = min(abs(price - s) / price for s in supports)
        if min_dist_sup < threshold:
            near_support = True
    if resistances:
        min_dist_res = min(abs(price - r) / price for r in resistances)
        if min_dist_res < threshold:
            near_resistance = True
    if near_support and not near_resistance:
        return "LOW"
    elif near_resistance and not near_support:
        return "HIGH"
    else:
        return "MID"

def get_liquidity_sweep_for_side(df, side, lookback=5):
    ctx = detect_liquidity_context(df, lookback=lookback)
    if side == "BUY" and ctx == "sell_side_taken":
        return True
    if side == "SELL" and ctx == "buy_side_taken":
        return True
    return False

def get_rejection_pinbar(df, side, atr):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 1:
        return False
    candle = df.iloc[-1]
    return is_pinbar(candle, atr, side)

def advanced_detect_scenario(df, side, atr, volume_state):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 3:
        return "NONE"
    sweep = get_liquidity_sweep_for_side(df, side)
    rejection = get_rejection_pinbar(df, side, atr)
    displacement = detect_displacement(df, side, atr, volume_state, body_atr_threshold=0.8, volume_expansion_required=False)
    if sweep and rejection:
        return "TRAP_REVERSAL"
    elif displacement and not rejection:
        return "TREND_CONTINUATION"
    else:
        return "NONE"

def advanced_decision_engine(scenario, adx, volume_state, location):
    if scenario == "NONE":
        return "SKIP", None
    if volume_state == "exhaustion":
        log_execution(f"[ADV_DECISION] Volume exhaustion -> SKIP", "INFO", debounce_key=f"adv_vol_exhaustion", debounce_sec=120)
        return "SKIP", None
    adx = float(adx) if adx is not None else 20.0
    if adx < 18:
        log_execution(f"[ADV_DECISION] ADX too low ({adx:.1f}) -> SKIP", "INFO", debounce_key=f"adv_adx_low", debounce_sec=120)
        return "SKIP", None
    if scenario == "TRAP_REVERSAL":
        if adx < 35:
            return "ENTER", "STRONG"
        else:
            log_execution(f"[ADV_DECISION] TRAP_REVERSAL but ADX >=35 ({adx:.1f}) -> SKIP", "INFO", debounce_key=f"adv_trap_adx_high", debounce_sec=120)
            return "SKIP", None
    elif scenario == "TREND_CONTINUATION":
        if 20 < adx < 45:
            return "ENTER", "MEDIUM"
        else:
            log_execution(f"[ADV_DECISION] TREND_CONTINUATION but ADX out of range (20-45) -> {adx:.1f} SKIP", "INFO", debounce_key=f"adv_trend_adx_range", debounce_sec=120)
            return "SKIP", None
    return "SKIP", None

# ========== LEGACY SMC FUNCTIONS ==========
def detect_bos(df, lookback=5):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback+2:
        return False, False
    recent_high = df['high'].iloc[-lookback-1:-1].max()
    recent_low = df['low'].iloc[-lookback-1:-1].min()
    current_close = df['close'].iloc[-1]
    bos_up = current_close > recent_high
    bos_down = current_close < recent_low
    return bos_up, bos_down

def detect_scenario(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 30:
        return "NONE"
    row = df.iloc[-1]
    liquidity_ctx = detect_liquidity_context(df, lookback=10)
    sweep_up = (liquidity_ctx == "buy_side_taken")
    sweep_down = (liquidity_ctx == "sell_side_taken")
    bos_up, bos_down = detect_bos(df)
    vol_spike_flag = volume_spike(df)
    vol_sma = df['volume'].iloc[-21:-1].mean() if len(df) >= 21 else df['volume'].mean()
    volume_ok = row['volume'] > 1.5 * vol_sma if vol_sma > 0 else False
    range_ = row['high'] - row['low']
    if range_ == 0:
        rejection_buy = False
        rejection_sell = False
    else:
        body = abs(row['close'] - row['open'])
        lower_wick = min(row['open'], row['close']) - row['low']
        upper_wick = row['high'] - max(row['open'], row['close'])
        rejection_buy = (lower_wick > 2 * body + 1e-9) or (row['close'] > row['open'] and body/range_ > 0.5)
        rejection_sell = (upper_wick > 2 * body + 1e-9) or (row['close'] < row['open'] and body/range_ > 0.5)
    if sweep_down and rejection_buy:
        return "REVERSAL_BUY"
    if sweep_up and rejection_sell:
        return "REVERSAL_SELL"
    if bos_up and volume_ok:
        return "TREND_BUY"
    if bos_down and volume_ok:
        return "TREND_SELL"
    if sweep_down and not rejection_buy:
        return "TRAP_SELL"
    if sweep_up and not rejection_sell:
        return "TRAP_BUY"
    return "NONE"

def decision_engine(scenario, rf_signal, adx):
    if scenario == "NONE":
        return "SKIP"
    if scenario == "REVERSAL_BUY" and rf_signal == "BUY":
        if adx < 35:
            return "STRONG"
        else:
            return "SKIP"
    if scenario == "REVERSAL_SELL" and rf_signal == "SELL":
        if adx < 35:
            return "STRONG"
        else:
            return "SKIP"
    if scenario == "TREND_BUY" and rf_signal == "BUY":
        if 20 < adx < 45:
            return "MEDIUM"
        else:
            return "SKIP"
    if scenario == "TREND_SELL" and rf_signal == "SELL":
        if 20 < adx < 45:
            return "MEDIUM"
        else:
            return "SKIP"
    if "TRAP" in scenario:
        return "STRONG"
    return "SKIP"

def apply_profit_engine(symbol, current_price, df, idx, position_state):
    if not position_state["open"]:
        return "HOLD"
    side = position_state["side"]
    entry = position_state["entry"]
    remaining = position_state["remaining_qty"]
    pnl_pct = (current_price - entry) / entry * 100 if side == "BUY" else (entry - current_price) / entry * 100
    if not position_state.get("tp1_hit", False) and pnl_pct >= 0.5:
        close_ratio = 0.3
        close_qty = remaining * close_ratio
        if close_qty > 0:
            if PAPER_MODE:
                position_state["remaining_qty"] -= close_qty
                TRADE_STATE["qty"] = position_state["remaining_qty"]
            else:
                close_partial(close_ratio)
            log_execution(f"TP1 hit at {pnl_pct:.2f}% - closed {close_ratio*100:.0f}%", "SUCCESS")
            tg_tp_hit(symbol, 1, pnl_pct)
        position_state["tp1_hit"] = True
        position_state["sl"] = entry
        position_state["trail_activated"] = True
        atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else current_price * 0.01
        if side == "BUY":
            position_state["trail_stop"] = current_price - TRAIL_ATR_MULT * atr_val
        else:
            position_state["trail_stop"] = current_price + TRAIL_ATR_MULT * atr_val
        return "TP1"
    if position_state.get("tp1_hit", False) and not position_state.get("tp2_hit", False) and pnl_pct >= 1.2:
        close_ratio = 0.3
        close_qty = position_state["remaining_qty"] * close_ratio
        if close_qty > 0:
            if PAPER_MODE:
                position_state["remaining_qty"] -= close_qty
                TRADE_STATE["qty"] = position_state["remaining_qty"]
            else:
                close_partial(close_ratio)
            log_execution(f"TP2 hit at {pnl_pct:.2f}% - closed {close_ratio*100:.0f}%", "SUCCESS")
            tg_tp_hit(symbol, 2, pnl_pct)
        position_state["tp2_hit"] = True
        return "TP2"
    if len(df) >= 2 and idx >= 1:
        adx_series = compute_adx(df)
        if adx_series is not None and len(adx_series) > idx:
            adx_now = adx_series.iloc[idx]
            adx_prev = adx_series.iloc[idx-1]
            if adx_now < adx_prev:
                candle_close = df['close'].iloc[idx]
                candle_open = df['open'].iloc[idx]
                if side == "BUY" and candle_close < candle_open:
                    log_execution(f"Exhaustion exit: ADX weakening and bearish candle", "WARN")
                    close_position_full()
                    return "EXIT"
                if side == "SELL" and candle_close > candle_open:
                    log_execution(f"Exhaustion exit: ADX weakening and bullish candle", "WARN")
                    close_position_full()
                    return "EXIT"
    if position_state.get("trail_activated", False) and position_state.get("trail_stop", 0) > 0:
        if side == "BUY" and current_price <= position_state["trail_stop"]:
            log_execution(f"Trailing stop hit at {current_price:.4f}", "WARN")
            close_position_full()
            return "EXIT"
        if side == "SELL" and current_price >= position_state["trail_stop"]:
            log_execution(f"Trailing stop hit at {current_price:.4f}", "WARN")
            close_position_full()
            return "EXIT"
        atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else current_price * 0.01
        if side == "BUY":
            new_stop = current_price - TRAIL_ATR_MULT * atr_val
            if new_stop > position_state["trail_stop"]:
                position_state["trail_stop"] = new_stop
        else:
            new_stop = current_price + TRAIL_ATR_MULT * atr_val
            if new_stop < position_state["trail_stop"]:
                position_state["trail_stop"] = new_stop
    return "HOLD"

def detect_liquidity_context(df, lookback=10):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback:
        return None
    sweeps = []
    for i in range(-lookback, 0):
        if i == -1:
            continue
        prev_low = df['low'].iloc[i-1]
        curr_low = df['low'].iloc[i]
        lower_wick = min(df['open'].iloc[i], df['close'].iloc[i]) - curr_low
        if curr_low < prev_low and lower_wick > 0.0001:
            sweeps.append("sell_side_taken")
        prev_high = df['high'].iloc[i-1]
        curr_high = df['high'].iloc[i]
        upper_wick = curr_high - max(df['open'].iloc[i], df['close'].iloc[i])
        if curr_high > prev_high and upper_wick > 0.0001:
            sweeps.append("buy_side_taken")
    if len(sweeps) == 0:
        return None
    return sweeps[-1]

def detect_zone_context(price, supports, resistances, threshold=0.003):
    near_support = min([abs(price - s)/price for s in supports]) < threshold if supports else False
    near_resistance = min([abs(price - r)/price for r in resistances]) < threshold if resistances else False
    return {"near_support": near_support, "near_resistance": near_resistance}

def detect_structure_shift(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 10:
        return None
    last_high = df['high'].iloc[-3]
    prev_high = df['high'].iloc[-6]
    last_low = df['low'].iloc[-3]
    prev_low = df['low'].iloc[-6]
    if last_high > prev_high and last_low > prev_low:
        return "bullish_shift"
    elif last_high < prev_high and last_low < prev_low:
        return "bearish_shift"
    return None

def get_clustered_zones(df, lookback=120, cluster_pct=0.002):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback:
        return [], []
    highs = df['high'].values[-lookback:]
    lows = df['low'].values[-lookback:]
    swing_highs = []
    for i in range(2, len(highs)-2):
        if highs[i] == max(highs[i-2:i+3]):
            swing_highs.append(highs[i])
    swing_lows = []
    for i in range(2, len(lows)-2):
        if lows[i] == min(lows[i-2:i+3]):
            swing_lows.append(lows[i])
    def cluster(points, pct):
        if not points:
            return []
        points = sorted(points)
        clusters = []
        current = [points[0]]
        for p in points[1:]:
            if abs(p - current[-1]) / p < pct:
                current.append(p)
            else:
                clusters.append(sum(current)/len(current))
                current = [p]
        clusters.append(sum(current)/len(current))
        return clusters
    res_levels = cluster(swing_highs, cluster_pct)
    sup_levels = cluster(swing_lows, cluster_pct)
    return sup_levels, res_levels

def detect_liquidity_cluster(df, lb=20, tol=0.001):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lb:
        return False, False
    highs = df['high'].iloc[-lb:]
    lows  = df['low'].iloc[-lb:]
    if highs.max() == highs.min() or lows.max() == lows.min():
        return False, False
    eqh = (highs.max() - highs.min()) / highs.mean() < tol
    eql = (lows.max() - lows.min()) / lows.mean() < tol
    return eqh, eql

def detect_sweep_v2(df, side):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 22:
        return False, False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    eqh, eql = detect_liquidity_cluster(df, lb=20, tol=0.001)
    range_ = last['high'] - last['low']
    if range_ == 0:
        return False, False
    if side == "BUY":
        lower_wick = min(last['open'], last['close']) - last['low']
        wick_ratio = lower_wick / range_
        low_cluster_min = df['low'].iloc[-21:-1].min()
        sweep_broke = eql and (last['low'] < low_cluster_min)
        reclaim = last['close'] > last['low']
        valid = (wick_ratio > 0.6 and reclaim)
        return sweep_broke, valid
    else:
        upper_wick = last['high'] - max(last['open'], last['close'])
        wick_ratio = upper_wick / range_
        high_cluster_max = df['high'].iloc[-21:-1].max()
        sweep_broke = eqh and (last['high'] > high_cluster_max)
        reclaim = last['close'] < last['high']
        valid = (wick_ratio > 0.6 and reclaim)
        return sweep_broke, valid

def candle_rejection(df, side):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 1:
        return False
    last = df.iloc[-1]
    range_ = last['high'] - last['low']
    if range_ == 0:
        return False
    body = abs(last['close'] - last['open'])
    if side == "BUY":
        lower_wick = min(last['open'], last['close']) - last['low']
        return (lower_wick > 1.5 * body) or (last['close'] > last['open'] and body/range_ > 0.5)
    else:
        upper_wick = last['high'] - max(last['open'], last['close'])
        return (upper_wick > 1.5 * body) or (last['close'] < last['open'] and body/range_ > 0.5)

def volume_spike(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 21:
        return False
    avg_vol = df['volume'].iloc[-21:-1].mean()
    last_vol = df['volume'].iloc[-1]
    return last_vol >= 1.5 * avg_vol

def is_late_entry(df, side):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 6:
        return False
    last5_move = abs(df['close'].iloc[-1] - df['close'].iloc[-6]) / df['close'].iloc[-6]
    if last5_move > 0.008:
        if side == "BUY":
            recent_high = df['high'].iloc[-5:].max()
            pullback = (recent_high - df['close'].iloc[-1]) / (recent_high - df['close'].iloc[-6]) if (recent_high - df['close'].iloc[-6]) != 0 else 0
            if pullback < 0.3:
                return True
        else:
            recent_low = df['low'].iloc[-5:].min()
            pullback = (df['close'].iloc[-1] - recent_low) / (df['close'].iloc[-6] - recent_low) if (df['close'].iloc[-6] - recent_low) != 0 else 0
            if pullback < 0.3:
                return True
    return False

def compute_location(df, price, side):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 50:
        return "mid"
    low50 = df['low'].iloc[-50:].min()
    high50 = df['high'].iloc[-50:].max()
    if high50 == low50:
        return "mid"
    relative = (price - low50) / (high50 - low50)
    if side == "BUY":
        if relative <= 0.3:
            return "discount"
        elif relative >= 0.7:
            return "premium"
        else:
            return "mid"
    else:
        if relative >= 0.7:
            return "premium"
        elif relative <= 0.3:
            return "discount"
        else:
            return "mid"

def swing_points(df, lb=5):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lb*2:
        return [], []
    highs = df['high'].values
    lows = df['low'].values
    swing_highs = []
    swing_lows = []
    for i in range(lb, len(df)-lb):
        if highs[i] == max(highs[i-lb:i+lb+1]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-lb:i+lb+1]):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows

def equal_levels(points, tolerance=0.0015):
    if len(points) < 2:
        return False
    avg = sum(points)/len(points)
    return all(abs(p - avg) / avg < tolerance for p in points)

def build_liquidity_pools(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 10:
        return {"high_pools": [], "low_pools": []}
    sh, sl = swing_points(df, lb=5)
    recent_highs = [p[1] for p in sh[-3:]] if len(sh) >= 3 else [sh[-1][1]] if sh else []
    if len(recent_highs) >= 2 and equal_levels(recent_highs):
        pools_high = recent_highs
    else:
        pools_high = [sh[-1][1]] if sh else []
    recent_lows = [p[1] for p in sl[-3:]] if len(sl) >= 3 else [sl[-1][1]] if sl else []
    if len(recent_lows) >= 2 and equal_levels(recent_lows):
        pools_low = recent_lows
    else:
        pools_low = [sl[-1][1]] if sl else []
    return {"high_pools": pools_high, "low_pools": pools_low}

def detect_sweep(df, pools):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2 or pools is None:
        return False, False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    swept_high = False
    swept_low = False
    for h in pools["high_pools"]:
        if last['high'] > h and prev['high'] <= h and last['close'] < last['high']:
            swept_high = True
            break
    for l in pools["low_pools"]:
        if last['low'] < l and prev['low'] >= l and last['close'] > last['low']:
            swept_low = True
            break
    return swept_high, swept_low

def classify_sweep(df, side):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
        return "fake", -2
    last = df.iloc[-1]
    range_ = last['high'] - last['low']
    if range_ == 0:
        return "weak", 1
    if side == "BUY":
        lower_wick = min(last['open'], last['close']) - last['low']
        wick_ratio = lower_wick / range_
        reclaimed = last['close'] > last['low']
        if wick_ratio > 0.6 and reclaimed:
            return "strong", 3
        elif wick_ratio > 0.3:
            return "weak", 1.5
        else:
            return "fake", -2
    else:
        upper_wick = last['high'] - max(last['open'], last['close'])
        wick_ratio = upper_wick / range_
        reclaimed = last['close'] < last['high']
        if wick_ratio > 0.6 and reclaimed:
            return "strong", 3
        elif wick_ratio > 0.3:
            return "weak", 1.5
        else:
            return "fake", -2

SWEEP_AUTHENTICITY = os.getenv("SWEEP_AUTHENTICITY", "true").strip().lower() in {"1", "true", "yes", "on"}


def get_sweep_authenticity(df, side, lookback=10):
    """Grade the DIRECTIONAL sweep candle that actually fired (not just the
    last closed candle): same detection as detect_liquidity_context, so the
    entry gate and the queue agree on WHICH candle swept, then applied the
    strong/weak/fake wick-reclaim test of classify_sweep to THAT candle.
    A 'fake' flag means the low/high was breached with no counter-wick and no
    reclaim -- the classic trap/fill-through that a frenzy BUY must never chase.
    Returns (grade, bar_offset) with bar_offset < 0 (negative index into df).
    SWEEP_AUTHENTICITY=False keeps the legacy binary behaviour."""
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback:
        return "unknown", -1
    side = str(side).upper()
    events = []
    for i in range(-lookback, 0):
        if i == -1:
            continue
        prev_low = df['low'].iloc[i - 1]
        curr_low = df['low'].iloc[i]
        lower_wick = min(df['open'].iloc[i], df['close'].iloc[i]) - curr_low
        if curr_low < prev_low and lower_wick > 0.0001:
            events.append(("sell_side_taken", i))
        prev_high = df['high'].iloc[i - 1]
        curr_high = df['high'].iloc[i]
        upper_wick = curr_high - max(df['open'].iloc[i], df['close'].iloc[i])
        if curr_high > prev_high and upper_wick > 0.0001:
            events.append(("buy_side_taken", i))
    if not events:
        return "unknown", -1
    tag, bar = events[-1]
    if tag != ("sell_side_taken" if side == "BUY" else "buy_side_taken"):
        return "unknown", -1
    c = df.iloc[bar]
    rng = max(float(c['high']) - float(c['low']), 1e-12)
    if rng < 1e-9:
        return "weak", bar
    if side == "BUY":
        wick_ratio = (min(float(c['open']), float(c['close'])) - float(c['low'])) / rng
        reclaimed = float(c['close']) > float(c['low'])
    else:
        wick_ratio = (float(c['high']) - max(float(c['open']), float(c['close']))) / rng
        reclaimed = float(c['close']) < float(c['high'])
    if wick_ratio > 0.3 and reclaimed:
        grade = "strong" if wick_ratio > 0.6 else "weak"
    else:
        grade = "fake"
    return grade, bar

def volume_engine(df):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return "normal", 0
    avg_vol = df['volume'].iloc[-20:].mean() if len(df) >= 20 else df['volume'].mean()
    last_vol = df['volume'].iloc[-1]
    if last_vol >= 1.5 * avg_vol:
        return "spike", 2
    elif last_vol < 0.7 * avg_vol:
        return "exhaustion", -1
    else:
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ > 0 and body / range_ < 0.4 and last_vol > avg_vol:
            return "absorption", 1
        else:
            return "normal", 0

def structure_engine(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 10:
        return False, False
    sh, sl = swing_points(df, lb=5)
    last_close = df['close'].iloc[-1]
    bos = False
    choch = False
    if len(sh) >= 2:
        if last_close > sh[-2][1]:
            bos = True
    if len(sl) >= 2:
        if last_close < sl[-2][1]:
            bos = True
    if len(sh) >= 2 and len(sl) >= 2:
        if sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]:
            choch = True
        elif sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]:
            choch = True
    return bos, choch

def pre_rf_context_boost(df, side):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 3:
        return 0, []
    last2 = df.iloc[-3:-1]
    boost = 0
    reasons = []
    if side == "BUY":
        if last2['close'].iloc[-2] < last2['close'].iloc[-1] and last2['close'].iloc[-1] < df['close'].iloc[-1]:
            boost += 1
            reasons.append("consecutive_bullish")
        if (last2['close'].iloc[-1] - last2['low'].iloc[-1]) / (last2['high'].iloc[-1] - last2['low'].iloc[-1] + 1e-9) > 0.7:
            boost += 1
            reasons.append("strong_bullish_candle")
    else:
        if last2['close'].iloc[-2] > last2['close'].iloc[-1] and last2['close'].iloc[-1] > df['close'].iloc[-1]:
            boost += 1
            reasons.append("consecutive_bearish")
        if (last2['high'].iloc[-1] - last2['close'].iloc[-1]) / (last2['high'].iloc[-1] - last2['low'].iloc[-1] + 1e-9) > 0.7:
            boost += 1
            reasons.append("strong_bearish_candle")
    return min(boost, 2), reasons

def market_intent(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 20:
        return None, 0
    recent_range = df['high'].iloc[-10:].max() - df['low'].iloc[-10:].min()
    avg_range = (df['high'].rolling(20).max() - df['low'].rolling(20).min()).iloc[-1]
    absorption = (recent_range / avg_range) < 0.5 if avg_range > 0 else False
    vol_state, _ = volume_engine(df)
    if absorption and vol_state == "absorption":
        return "accumulation", 1
    last = df.iloc[-1]
    prev = df.iloc[-2]
    if vol_state == "spike" and last['close'] < prev['high'] and last['high'] > prev['high']:
        return "distribution", 1
    if len(df) >= 3:
        c1 = df.iloc[-3]
        c2 = df.iloc[-2]
        c3 = df.iloc[-1]
        if c2['high'] > c1['high'] and c3['close'] < c2['high'] and c3['close'] < c3['open']:
            return "trap", 2
        if c2['low'] < c1['low'] and c3['close'] > c2['low'] and c3['close'] > c3['open']:
            return "trap", 2
    return None, 0

def council_decision(context):
    score = 0
    reasons = []
    if context["location"] == "discount" and context["side"] == "BUY":
        score += 3
        reasons.append("discount_location")
    elif context["location"] == "premium" and context["side"] == "SELL":
        score += 3
        reasons.append("premium_location")
    else:
        score -= 4
        reasons.append("bad_location")
    if context["sweep"] == "strong":
        score += 3
        reasons.append("strong_sweep")
    elif context["sweep"] == "weak":
        score += 1
        reasons.append("weak_sweep")
    elif context["sweep"] == "fake":
        score -= 2
        reasons.append("fake_sweep")
    if context["in_zone"]:
        score += 2.5
        reasons.append("zone_hit")
    if context["volume"] == "spike":
        score += 2
        reasons.append("volume_spike")
    elif context["volume"] == "absorption":
        score += 1
        reasons.append("absorption")
    elif context["volume"] == "exhaustion":
        score -= 1
        reasons.append("exhaustion")
    if context["bos"] or context["choch"]:
        score += 2
        reasons.append("structure_shift")
    intent = context.get("intent")
    if intent == "trap":
        score += 2
        reasons.append("trap_intent")
    elif intent == "accumulation":
        score += 1
        reasons.append("accumulation")
    elif intent == "distribution":
        score += 1
        reasons.append("distribution")
    score += context.get("pre_rf_boost", 0)
    if context.get("pre_rf_reasons"):
        reasons.extend(context["pre_rf_reasons"])
    if context.get("distance_penalty", 0) == -100:
        return -100, ["far_from_zone_skip"]
    score += context.get("distance_penalty", 0)
    if context.get("distance_reasons"):
        reasons.extend(context["distance_reasons"])
    adx = context["adx"]
    if adx < 18:
        score -= 1
        reasons.append("weak_trend")
    elif 20 <= adx <= 30:
        score += 2
        reasons.append("ideal_trend_phase")
    elif 30 < adx <= 35:
        score += 0.5
        reasons.append("mid_trend")
    elif adx > 40:
        score -= 2
        reasons.append("late_trend_no_entry")
    final_score = max(0, min(12, score))
    return final_score, reasons

def compute_sl_tp(entry_price, side, classification, atr, df):
    if df is None or not isinstance(df, pd.DataFrame):
        sl = entry_price - atr * 1.6 if side == "BUY" else entry_price + atr * 1.6
        tp1 = entry_price * (1 + 0.008) if side == "BUY" else entry_price * (1 - 0.008)
        tp2 = entry_price * (1 + 0.02) if side == "BUY" else entry_price * (1 - 0.02)
        return sl, tp1, tp2
    if classification == "REVERSAL":
        pools = build_liquidity_pools(df)
        if side == "BUY":
            sl = min(pools["low_pools"]) - 0.5 * atr if pools["low_pools"] else entry_price - atr * 1.2
        else:
            sl = max(pools["high_pools"]) + 0.5 * atr if pools["high_pools"] else entry_price + atr * 1.2
        min_sl_dist = 1.2 * atr
        if abs(entry_price - sl) < min_sl_dist:
            sl = entry_price - min_sl_dist if side == "BUY" else entry_price + min_sl_dist
        tp1 = entry_price * (1 + 0.005) if side == "BUY" else entry_price * (1 - 0.005)
        tp2 = entry_price * (1 + 0.01) if side == "BUY" else entry_price * (1 - 0.01)
    elif classification == "EARLY_TREND":
        ema50 = ema(df['close'], 50).iloc[-1]
        sl = ema50 - atr * 1.2 if side == "BUY" else ema50 + atr * 1.2
        tp1 = entry_price * (1 + 0.008) if side == "BUY" else entry_price * (1 - 0.008)
        tp2 = entry_price * (1 + 0.02) if side == "BUY" else entry_price * (1 - 0.02)
    else:
        sl = entry_price - atr * 1.6 if side == "BUY" else entry_price + atr * 1.6
        tp1 = entry_price * (1 + 0.008) if side == "BUY" else entry_price * (1 - 0.008)
        tp2 = entry_price * (1 + 0.02) if side == "BUY" else entry_price * (1 - 0.02)
    sl, tp1 = PrecisionSafety.adjust_sl_tp(df.symbol if hasattr(df, 'symbol') else DEFAULT_SYMBOL, entry_price, sl, tp1, side, atr)
    return sl, tp1, tp2

# ========== EXECUTION LAYER ==========
STATE = {
    "open": False, "side": None, "entry": 0.0, "qty": 0.0, "remaining_qty": 0.0,
    "sl": 0.0, "tp1_done": False, "trail_activated": False, "trail_stop": 0.0,
    "peak": 0.0, "cooldown_until": None, "daily_trades": 0, "last_trade_day": None,
    "consecutive_losses": 0, "daily_peak_balance": None, "daily_loss_limit_hit": False,
    "current_symbol": None, "balance": 0.0, "atr": 0.0, "entry_time": None,
    "entry_reasons": [], "trade_score": 0, "partial_closed": False,
    "tp1_price": 0.0, "tp2_price": 0.0, "trade_type": None, "entry_type": None,
    "be_done": False, "classification": None, "location": None, "zone_info": None,
    "runner_active": False, "scale_ins": 0, "decision_log": [],
    "tp1_hit": False, "tp2_hit": False,
    "zone": {},
    "initial_margin": 0.0, "real_unrealized_pnl": 0.0, "roe_pct": 0.0, "leverage": LEVERAGE,
    "smart_tightened": False, "smart_partial_done": False, "smart_exit_triggered": False,
    "mark_price": 0.0, "unrealized_pnl_usdt": 0.0,
    "margin": 0.0, "liquidation_price": 0.0,
    "narrative_classification": None, "narrative_confidence": 0.0,
    "confidence_level": None,
    "continuation_probability": 0.5,
    "hold_quality": "UNKNOWN",
    "counter_pressure": 0.0,
    "reclaim_risk": 0.0,
    "trend_strength": 0.0,
    "continuation_reasons": [],
    "trade_thesis": None,
    "current_confidence": 50.0,
    "market_regime": "UNKNOWN",
    "continuation_pressure": 50,
    "thesis_failure_score": 0,
    "prev_di_spread": 0.0,
    "adx_live": 0.0,
    "di_plus_live": 0.0,
    "di_minus_live": 0.0,
    "trade_personality": "NEUTRAL",
    "institutional_flow": "NEUTRAL",
    "profit_lock_activated": False,
    "trail_tightened": False,
    "smart_money": {},
    "momentum_flow": {},
    "trade_state": "RANGE_CHOP",
    "delay_tp1": False,
    "smart_trail_mult": 1.5,
    "synthetic_sl": 0.0,
    "synthetic_tp1": 0.0,
    "max_price": 0.0,
    "min_price": 0.0,
    "peak_roe": 0.0,
    "peak_price": 0.0,
    "peak_unrealized_pnl": 0.0,
    "drawdown_from_peak": 0.0,
    "tp1_hold_score": 10,
    "exit_warning": 0,
    "runner_mode": False,
    "entry_atr": 0.0,
    "trade_style": "SCALP",
    "entry_timing": "WAIT_RETEST",
    "market_phase": "UNKNOWN",
    "zone_behaviour": "NEUTRAL",
    "trade_board": {},
    "trade_intelligence": {}
}
paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
_ACTIVE_TRADE = False
_closing_in_progress = False
_TRADE_LOCK = threading.RLock()

# ========== DASHBOARD STATE ==========
DASHBOARD_STATE = {
    "account": {"balance": 0.0, "free_balance": 0.0, "available_margin": 0.0, "mode": "PAPER"},
    "stats": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0},
    "position": None,
    "logs": [],
    "errors": [],
    "live_trade_mode": False,
    "lifecycle_state": "IDLE",
    "live_supervisor": {},
    "institutional_flow": {}
}

def publish_position_state(symbol, side, entry, qty, pnl=0.0):
    DASHBOARD_STATE["position"] = {
        "symbol": symbol,
        "side": side,
        "entry": round(entry, 4),
        "qty": qty,
        "pnl": round(pnl, 2),
        "sl": round(STATE.get("synthetic_sl", 0), 4),
        "tp1": round(STATE.get("synthetic_tp1", 0), 4),
        "tp2": round(STATE.get("tp2_price", 0), 4),
        "tp1_done": STATE.get("tp1_hit", False),
        "trailing_active": STATE.get("trail_activated", False),
        "regime": MEMORY.get("regime", "UNKNOWN"),
        "trade_type": STATE.get("trade_type", "N/A"),
        "entry_type": STATE.get("entry_type", "N/A"),
        "classification": STATE.get("classification", "N/A"),
        "location": STATE.get("location", "N/A"),
        "zone": STATE.get("zone_info", "N/A"),
        "score": STATE.get("trade_score", 0),
        "narrative_classification": STATE.get("narrative_classification", ""),
        "narrative_confidence": STATE.get("narrative_confidence", 0.0),
        "confidence_level": STATE.get("confidence_level", ""),
        "current_confidence": STATE.get("current_confidence", 50.0),
        "market_regime": STATE.get("market_regime", "UNKNOWN"),
        "continuation_pressure": STATE.get("continuation_pressure", 50),
        "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
        "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
        "delay_tp1": STATE.get("delay_tp1", False)
    }

def update_position_dashboard(symbol, side, entry, qty, pnl=0.0):
    publish_position_state(symbol, side, entry, qty, pnl)

def clear_position_dashboard():
    DASHBOARD_STATE["position"] = None

# ========== FULL log_execution (debounce + dashboard) ==========
def log_execution(msg, level="INFO", debounce_key=None, debounce_sec=60):
    # Never touch buffered stdout/stderr once interpreter shutdown has begun.
    if shutdown_requested():
        return
    if debounce_key:
        now = time.time()
        last = MEMORY.get("log_debounce", {}).get(debounce_key, 0)
        if now - last < debounce_sec:
            return
        MEMORY.setdefault("log_debounce", {})[debounce_key] = now
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if level == "INFO":
        colored = color_text(msg, CYAN)
    elif level == "SUCCESS":
        colored = color_text(msg, GREEN)
    elif level == "ERROR":
        colored = color_text(msg, RED)
    elif level == "WARN":
        colored = color_text(msg, YELLOW)
    else:
        colored = msg
    entry = f"[{ts}] {msg}"
    DASHBOARD_STATE["logs"].append(entry)
    if len(DASHBOARD_STATE["logs"]) > 200:
        DASHBOARD_STATE["logs"].pop(0)
    print(colored)
    if level == "ERROR":
        DASHBOARD_STATE["errors"].append(entry)
        if len(DASHBOARD_STATE["errors"]) > 50:
            DASHBOARD_STATE["errors"].pop(0)
        tg_error(msg, level)

def update_stats(pnl_pct):
    DASHBOARD_STATE["stats"]["trades"] += 1
    if pnl_pct >= 0:
        DASHBOARD_STATE["stats"]["wins"] += 1
    else:
        DASHBOARD_STATE["stats"]["losses"] += 1
    total = DASHBOARD_STATE["stats"]["trades"]
    DASHBOARD_STATE["stats"]["win_rate"] = (DASHBOARD_STATE["stats"]["wins"] / total * 100) if total else 0

# ========== ORDER MANAGER (NEW) ==========
class OrderManager:
    def __init__(self, exchange, max_retries=3, retry_delay=1.0, confirm_timeout=15.0):
        self.exchange = exchange
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.confirm_timeout = confirm_timeout
        self._pending_orders = {}
        self._lock = threading.RLock()
        self._id_lock = threading.Lock()
        self._seen_ids = set()
        self._id_last_ms = 0
        self._id_sequence = 0

    @staticmethod
    def _base36(value: int) -> str:
        """Compact non-negative integer encoding using BingX-safe characters."""
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        value = max(0, int(value))
        if value == 0:
            return "0"
        out = []
        while value:
            value, rem = divmod(value, 36)
            out.append(chars[rem])
        return "".join(reversed(out))

    def _generate_client_id(self, symbol, side):
        """Generate a deterministic-unique, BingX-safe client order ID.

        The old implementation used millisecond time + four random digits.
        Under burst generation that can collide (the Windows regression suite
        reproduced a 49/50 collision).  A per-manager monotonic sequence is
        now the uniqueness source, while the timestamp remains useful for
        operator tracing.  The resulting ID stays comfortably below BingX's
        40-character limit and contains only ASCII letters/digits/underscore.
        """
        sym = normalize_symbol(symbol)
        token = hashlib.md5(sym.encode("utf-8")).hexdigest()[:8]
        side_token = str(side).upper()[:4] or "ORD"
        with self._id_lock:
            now_ms = int(time.time() * 1000)
            if now_ms == self._id_last_ms:
                self._id_sequence += 1
            else:
                self._id_last_ms = now_ms
                self._id_sequence = 0
            seq = self._base36(self._id_sequence).rjust(3, "0")[-6:]
        # 8 + 1 + 4 + 1 + 13 + 1 + <=6 = <=34 chars.
        return f"{token}_{side_token}_{now_ms}_{seq}"

    def submit_order(self, symbol, side, amount, leverage, client_order_id=None):
        with self._lock:
            if client_order_id is None:
                client_order_id = self._generate_client_id(symbol, side)
            if client_order_id in self._seen_ids:
                raise ValueError(f"Duplicate order ID: {client_order_id}")
            self._seen_ids.add(client_order_id)

            sym = normalize_symbol(symbol)
            try:
                if len(client_order_id) > 40:
                    raise ValueError(
                        f"clientOrderId exceeds BingX 40-char limit: {len(client_order_id)} chars"
                    )
                order = self.exchange.create_order(
                    sym, "market", side.lower(), amount,
                    params={"leverage": leverage, "clientOrderId": client_order_id,
                            "positionSide": _hedge_position_side(side)}
                )
                self._pending_orders[client_order_id] = {
                    "symbol": sym,
                    "order": order,
                    "status": "PENDING",
                    "attempts": 0,
                    "filled": 0,
                    "timestamp": time.time()
                }
                return order, client_order_id
            except Exception as e:
                self._pending_orders[client_order_id] = {
                    "symbol": sym,
                    "error": str(e),
                    "status": "FAILED",
                    "attempts": 1,
                    "timestamp": time.time()
                }
                raise e

    def confirm_order(self, client_order_id, symbol):
        with self._lock:
            if client_order_id not in self._pending_orders:
                return None, "UNKNOWN"
            data = self._pending_orders[client_order_id]
            if data["status"] in ("FILLED", "REJECTED", "CANCELED", "EXPIRED"):
                return data.get("order"), data["status"]

        start = time.time()
        attempts = 0
        while time.time() - start < self.confirm_timeout:
            attempts += 1
            try:
                sym = normalize_symbol(symbol)
                order = self.exchange.fetch_order(client_order_id, sym)
                status = order.get('status', '').lower()
                filled = float(order.get('filled', 0))
                if status == 'closed':
                    with self._lock:
                        self._pending_orders[client_order_id]["status"] = "FILLED"
                        self._pending_orders[client_order_id]["order"] = order
                        self._pending_orders[client_order_id]["filled"] = filled
                    return order, "FILLED"
                elif status in ('canceled', 'expired', 'rejected'):
                    with self._lock:
                        self._pending_orders[client_order_id]["status"] = status.upper()
                    return order, status.upper()
                elif status == 'partial':
                    with self._lock:
                        self._pending_orders[client_order_id]["status"] = "PARTIALLY_FILLED"
                        self._pending_orders[client_order_id]["filled"] = filled
                time.sleep(0.5)
            except Exception as e:
                if attempts > 3:
                    break
                time.sleep(0.5)
        with self._lock:
            self._pending_orders[client_order_id]["status"] = "TIMEOUT"
        return None, "TIMEOUT"

    def get_order_status(self, client_order_id):
        with self._lock:
            data = self._pending_orders.get(client_order_id)
            if data:
                return data.get("status", "UNKNOWN")
            return "UNKNOWN"

    def mark_recovered(self, client_order_id, order):
        """After a confirm TIMEOUT, reconciliation with the exchange proved the
        original order actually executed. Reconcile the local order book to
        FILLED so the filled position is never left as UNKNOWN/FAILED locally."""
        with self._lock:
            data = self._pending_orders.get(client_order_id)
            filled = float(order.get("filled", 0.0) or 0.0)
            if data:
                data["status"] = "FILLED"
                data["order"] = order
                data["filled"] = filled
                data["recovered"] = True
            else:
                self._pending_orders[client_order_id] = {
                    "symbol": normalize_symbol(order.get("symbol", "")),
                    "order": order,
                    "status": "FILLED",
                    "attempts": 0,
                    "filled": filled,
                    "timestamp": time.time(),
                    "recovered": True,
                }

    def is_duplicate(self, client_order_id):
        return client_order_id in self._seen_ids

    def reset(self):
        with self._lock:
            self._pending_orders.clear()
            self._seen_ids.clear()

_order_manager = OrderManager(ex, max_retries=3, retry_delay=1.0, confirm_timeout=15.0)


def reconcile_open_after_timeout(symbol, direction, client_order_id, retries=2):
    """Timeout-reconciliation for an OPEN order (BingX Hedge Mode).

    A create_order that may have executed but whose confirmation fetch timed out
    must NEVER be treated as a rejection and NEVER be blindly retried. The
    exchange's real position is the source of truth: fetch_positions is matched
    against the requested hedge side (LONG for BUY, SHORT for SELL — NEVER BOTH
    and never one-way forced). If a matching position with nonzero contracts
    exists, the original order is treated as FILLED/ACCEPTED and a synthetic
    filled-order record is returned so the caller converges into the exact same
    post-fill lifecycle as a normally confirmed order. Returns None when no
    matching position exists (order stays UNKNOWN; the open may only be retried
    after reconciliation proves no fill).
    """
    desired = "long" if str(direction).upper() in ("BUY", "LONG") else "short"
    sym = normalize_symbol(symbol)
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            positions = None
            if hasattr(ex, "fetch_positions"):
                positions = safe_api_call(ex.fetch_positions, [sym])
            if positions is None and hasattr(ex, "fetch_open_positions"):
                positions = safe_api_call(ex.fetch_open_positions, [sym])
            if positions:
                for pos in positions:
                    pos_sym = pos.get("symbol", "")
                    if sym not in pos_sym:
                        continue
                    if str(pos.get("side", "")).lower() != desired:
                        continue
                    contracts = float(pos.get("contracts", 0) or 0)
                    if contracts <= 0:
                        continue
                    entry_price = float(pos.get("entryPrice", 0) or 0)
                    avg_price = entry_price if entry_price > 0 else 0.0
                    leverage = float(pos.get("leverage", 0) or 0)
                    return {
                        "id": pos.get("positionId") or pos.get("id") or client_order_id,
                        "clientOrderId": client_order_id,
                        "symbol": sym,
                        "side": str(direction).lower(),
                        "type": "market",
                        "status": "closed",
                        "filled": contracts,
                        "amount": contracts,
                        "average": avg_price,
                        "price": avg_price,
                        "entryPrice": entry_price,
                        "timestamp": int(time.time() * 1000),
                        "leverage": leverage,
                        "recovered": True,
                        "positionSide": _hedge_position_side(direction),
                    }
            return None
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5)
    if last_err is not None:
        log_execution(
            f"[OPEN_RECOVERY] symbol={sym} side={direction} "
            f"reconciliation error: {last_err}", "ERROR")
    return None

# ========== MODIFIED open_position() ==========
def open_position(side, amount, symbol, client_order_id=None):
    global _ACTIVE_TRADE
    sym = normalize_symbol(symbol)
    with _TRADE_LOCK:
        if _ACTIVE_TRADE:
            log_execution("[OPEN] Another trade already in progress, skipping", "WARN")
            return None
        _ACTIVE_TRADE = True
    try:
        set_leverage(symbol, LEVERAGE)
        bal = safe_api_call(ex.fetch_balance)
        if bal is None:
            log_execution("[OPEN] Failed to fetch balance", "ERROR")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return None
        usdt = bal.get("free", {}).get("USDT", 0.0)
        ticker = safe_api_call(ex.fetch_ticker, sym)
        if ticker is None:
            log_execution("[OPEN] Failed to fetch ticker", "ERROR")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return None
        price = ticker["last"]
        required_margin = (amount * price) / LEVERAGE
        if usdt < required_margin * 1.01:
            log_execution(f"[OPEN] Insufficient margin: need {required_margin:.2f}, have {usdt:.2f}", "ERROR")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            global INSUFFICIENT_MARGIN_COOLDOWN_UNTIL
            INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = time.time() + INSUFFICIENT_MARGIN_COOLDOWN_SEC
            return None
        max_spread = dynamic_spread_tolerance(symbol)
        spread = get_spread_bps(symbol)
        if spread > max_spread:
            log_execution(f"[OPEN] Spread {spread:.2f}% > {max_spread}%", "WARN")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return None

        amount = float(ex.amount_to_precision(sym, amount))
        order, cid = _order_manager.submit_order(symbol, side, amount, LEVERAGE, client_order_id)
        if order is None:
            log_execution("[OPEN] Order submission failed", "ERROR")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return None

        filled_order, status = _order_manager.confirm_order(cid, symbol)
        if status == "FILLED":
            log_execution(f"[OPEN] Order filled: {side} {amount} {symbol} @ {price}", "SUCCESS")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return filled_order
        elif status == "TIMEOUT":
            # The venue may still have executed the order: the confirm fetch
            # timed out, so the order outcome is UNKNOWN (never assume a
            # rejection). Reconcile against the real exchange position BEFORE any
            # retry is even considered, and adopt the real position if it exists.
            log_execution(
                f"[OPEN_RECOVERY] symbol={symbol} side={side} status=UNKNOWN "
                f"(confirm TIMEOUT) - reconciling with BingX, no blind retry",
                "WARN")
            recovered = reconcile_open_after_timeout(symbol, side, cid)
            if recovered is not None:
                _order_manager.mark_recovered(cid, recovered)
                log_execution(
                    f"[OPEN_RECOVERY] symbol={symbol} side={side} "
                    f"status=FILLED_AFTER_TIMEOUT qty={recovered['filled']} "
                    f"entry={recovered['average']} order_id={recovered['id']}",
                    "SUCCESS")
                log_execution(
                    f"[POSITION_ADOPTED] symbol={symbol} side={side} "
                    f"qty={recovered['filled']} entry={recovered['average']}",
                    "SUCCESS")
                with _TRADE_LOCK:
                    _ACTIVE_TRADE = False
                return recovered
            log_execution(
                f"[OPEN_RECOVERY] symbol={symbol} side={side} status=NOT_FILLED - "
                f"no matching position on BingX, order left unresolved (an open may "
                f"only be tried again after reconciliation proves no fill)",
                "WARN")
            with _TRADE_LOCK:
                _ACTIVE_TRADE = False
            return None
        elif status in ("PARTIALLY_FILLED", "PENDING"):
            log_execution(f"[OPEN] Partial fill or still pending: {status}", "WARN")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return None
        else:
            log_execution(f"[OPEN] Order failed: {status}", "ERROR")
            with _TRADE_LOCK: _ACTIVE_TRADE = False
            return None
    except Exception as e:
        log_execution(f"[OPEN] Open position error: {traceback.format_exc()}", "ERROR")
    with _TRADE_LOCK:
        _ACTIVE_TRADE = False
    return None

def close_position(amount, symbol):
    global _ACTIVE_TRADE
    sym = normalize_symbol(symbol)
    side = STATE["side"]
    close_side = "sell" if side == "BUY" else "buy"
    try:
        amount = float(ex.amount_to_precision(sym, amount))
        order = safe_api_call(ex.create_order, sym, "market", close_side, amount, params={"reduceOnly": True, "positionSide": _hedge_position_side(side)})
        with _TRADE_LOCK:
            _ACTIVE_TRADE = False
        log_execution(f"[CLOSE] Closed {amount} {symbol} (reduceOnly)", "SUCCESS")
        return order
    except Exception as e:
        log_execution(f"[CLOSE] Close position error: {traceback.format_exc()}", "ERROR")
        with _TRADE_LOCK:
            _ACTIVE_TRADE = False
        return None

def finalize_trade_with_reality(symbol):
    mark_price, unrealized, initial_margin, roe = sync_position_state(symbol)
    if mark_price is None and not PAPER_MODE:
        mark_price = get_ticker_safe(symbol)
    pnl_usdt = 0.0
    pnl_pct = 0.0
    if PAPER_MODE:
        entry = STATE["entry"]
        side = STATE["side"]
        mark_price = mark_price if mark_price is not None else STATE.get("mark_price", entry)
        if side == "BUY":
            pnl_pct = (mark_price - entry) / entry * 100
        else:
            pnl_pct = (entry - mark_price) / entry * 100
        pnl_usdt = pnl_pct / 100 * entry * STATE["qty"]
        if paper.get("committed_margin", 0) >= STATE["margin"]:
            paper["balance"] = paper.get("balance", 10000.0) + STATE["margin"] + pnl_usdt
            paper["committed_margin"] = paper.get("committed_margin", 0.0) - STATE["margin"]
            log_execution(f"[PAPER MARGIN] released {STATE['margin']:.2f} USDT + PnL {pnl_usdt:+.2f} -> free={paper['balance']:.2f}, total_committed={paper['committed_margin']:.2f}", "INFO")
        else:
            paper["balance"] = paper.get("balance", 10000.0) + pnl_usdt
            log_execution(f"[PAPER MARGIN] WARN: restoring {STATE['margin']:.2f} exceeded committed {paper.get('committed_margin',0):.2f}; only PnL added", "WARN")
    else:
        realized_usdt, realized_pct = get_realized_pnl_for_symbol(symbol, lookback_seconds=30)
        if realized_usdt != 0.0:
            pnl_usdt = realized_usdt
            pnl_pct = realized_pct
        else:
            if roe is not None:
                pnl_pct = roe
                if STATE.get("margin", 0) > 0:
                    pnl_usdt = STATE["margin"] * (roe / 100)
                else:
                    pnl_usdt = (pnl_pct / 100) * STATE["entry"] * STATE["qty"]
    PERF["total_pnl_pct"] += pnl_pct
    PERF["total_pnl_usdt"] += pnl_usdt
    PERF["trades"] += 1
    if pnl_pct >= 0:
        PERF["wins"] += 1
        result = "WIN"
    else:
        PERF["losses"] += 1
        result = "LOSS"
    PERF["last_trade"] = {"result": result, "pnl_pct": pnl_pct}
    TRADE_STATE.update({
        "in_position": False,
        "symbol": None,
        "side": None,
        "entry": 0.0,
        "qty": 0.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "trail_on": False,
        "zone": None,
        "location": None,
        "reason": []
    })
    DASHBOARD_STATE["live_trade_mode"] = False
    log_execution(f"Trade closed: {result} {pnl_pct:.2f}% | USDT: {pnl_usdt:+.2f}", "SUCCESS" if pnl_pct>=0 else "ERROR")
    entry_time = STATE.get("entry_time", time.time())
    tg_close(STATE.get("current_symbol") or "UNKNOWN", pnl_pct,
             max(0.0, (time.time() - entry_time)) / 60.0, STATE.get("side"))
    with _TRADE_LOCK:
        STATE["open"] = False
        STATE["side"] = None
        STATE["current_symbol"] = None
        STATE["tp1_hit"] = False
        STATE["tp2_hit"] = False
        STATE["trail_activated"] = False
        STATE["profit_lock_activated"] = False
        STATE["runner_mode"] = False
        STATE["trail_tightened"] = False
        STATE["partial_closed"] = False
        STATE["scale_ins"] = 0
        # Position Management Engine cleanup on full close
        STATE["position_profile"] = None
        STATE["position_health"] = 0.0
        STATE["position_action"] = None
        STATE["position_trade_type"] = None
        STATE["position_asset_class"] = None
        STATE["trade_style"] = "SCALP"
        STATE["entry_timing"] = "WAIT_RETEST"
        STATE["market_phase"] = "UNKNOWN"
        STATE["zone_behaviour"] = "NEUTRAL"
        STATE["trade_board"] = {}
        STATE["trade_intelligence"] = {}
        STATE["dynamic_reversal_exit_done"] = False
        STATE["dynamic_exhaustion_protect_done"] = False
        STATE["dynamic_partial_done"] = False
        if _live_manager is not None:
            _live_manager.position_profile = None
            _live_manager._last_position_action = None
            _live_manager._last_position_trade_type = None
            _live_manager.trade_board = TradeManagementBoard() if TRADE_INTELLIGENCE_AVAILABLE else None
    return pnl_usdt, pnl_pct

def dynamic_spread_tolerance(symbol):
    df = get_ohlcv_safe(symbol, 50)
    if df is None:
        result = MAX_SPREAD_PERCENT_DEFAULT
    else:
        atr = compute_atr(df).iloc[-1]
        price = df['close'].iloc[-1]
        atr_pct = (atr/price)*100 if price>0 else 0.5
        result = MAX_SPREAD_PERCENT_VOLATILE if atr_pct > 2.0 else MAX_SPREAD_PERCENT_DEFAULT
    cfg = AssetBehaviorProfile.ob_config(AssetBehaviorProfile.resolve_asset_class(symbol or ""))
    cap = cfg.get("ob_spread_cap_pct")
    if cap is not None:
        result = cap
    return result

# ============================================================
# STRATEGY FUNCTIONS REPLACED WITH THOSE FROM test cv new.py
# ============================================================

# === STRATEGY FROM test cv new.py ===
def get_vwap_narrative(df):
    """VWAP narrative extraction."""
    vwap = compute_vwap(df)
    price = df['close'].iloc[-1]
    vwap_last = vwap.iloc[-1]
    vwap_prev = vwap.iloc[-2] if len(vwap) > 1 else vwap_last
    distance = (price - vwap_last) / vwap_last if vwap_last != 0 else 0.0
    above = price > vwap_last
    below = price < vwap_last
    prev_above = df['close'].iloc[-2] > vwap_prev if len(df) > 1 else above
    reclaim = (not prev_above) and above
    reject = prev_above and (not above)
    return {
        "vwap": vwap_last,
        "distance": distance,
        "above": above,
        "below": below,
        "reclaim": reclaim,
        "reject": reject,
        "slope": vwap_last - vwap_prev
    }

def compute_enhanced_zone_strength(df, level, zone_type, atr, ob, sweep_detected=False):
    """Enhanced zone strength calculation."""
    price = df['close'].iloc[-1]
    touches = 0
    rejection_count = 0
    volume_at_touches = []
    for i in range(max(0, len(df)-60), len(df)):
        candle_high = df['high'].iloc[i]
        candle_low = df['low'].iloc[i]
        if zone_type == "support":
            if abs(candle_low - level) < atr * 0.5:
                touches += 1
                if i < len(df)-1:
                    next_close = df['close'].iloc[i+1]
                    if next_close > df['close'].iloc[i]:
                        rejection_count += 1
                        volume_at_touches.append(df['volume'].iloc[i])
        else:
            if abs(candle_high - level) < atr * 0.5:
                touches += 1
                if i < len(df)-1:
                    next_close = df['close'].iloc[i+1]
                    if next_close < df['close'].iloc[i]:
                        rejection_count += 1
                        volume_at_touches.append(df['volume'].iloc[i])
    vol_score = 0.0
    if volume_at_touches:
        avg_vol_touch = sum(volume_at_touches) / len(volume_at_touches)
        avg_vol_overall = df['volume'].iloc[-60:].mean()
        if avg_vol_overall > 0:
            vol_score = min(3.0, avg_vol_touch / avg_vol_overall)
    strength = touches * 1.5 + rejection_count * 2.0 + vol_score
    if sweep_detected:
        strength += 2.0
    last = df.iloc[-1]
    body, range_, upper_wick, lower_wick = candle_metrics(last)
    if zone_type == "support" and lower_wick > body * 1.5 and abs(last['low'] - level) < atr:
        strength += 2.0
    elif zone_type == "resistance" and upper_wick > body * 1.5 and abs(last['high'] - level) < atr:
        strength += 2.0
    return min(10.0, strength)

def get_trend_direction(df):
    """Determine trend direction."""
    try:
        plus_di, minus_di, _, _ = get_di_components(df)
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1] if len(df)>=50 else ema20
        price = df['close'].iloc[-1]
        struct = detect_structure_shift(df)
        if (plus_di > minus_di and ema20 > ema50 and price > ema20) or struct == "bullish_shift":
            return "BULLISH"
        elif (minus_di > plus_di and ema20 < ema50 and price < ema20) or struct == "bearish_shift":
            return "BEARISH"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def detect_market_regime(df):
    """Market regime classifier."""
    if len(df) < 50:
        return "RANGE"
    try:
        adx = compute_adx(df).iloc[-1]
        plus_di, minus_di, _, _ = get_di_components(df)
        vwap_n = get_vwap_narrative(df)
        atr = compute_atr(df).iloc[-1]
        atr_avg = compute_atr(df).rolling(20).mean().iloc[-1] if len(compute_atr(df))>=20 else atr
        atr_ratio = atr / atr_avg if atr_avg else 1.0
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1] if len(df)>=50 else ema20
        price = df['close'].iloc[-1]
        di_delta = abs(plus_di - minus_di)
        if adx < 18 and di_delta < 6:
            return "CHOP"
        if adx > 20 and di_delta > 5:
            struct = detect_structure_shift(df)
            bullish_aligned = plus_di > minus_di and ema20 > ema50 and price > ema20
            bearish_aligned = minus_di > plus_di and ema20 < ema50 and price < ema20
            if bullish_aligned or bearish_aligned:
                return "TREND"
            if struct == "bullish_shift" and plus_di > minus_di:
                return "TREND"
            if struct == "bearish_shift" and minus_di > plus_di:
                return "TREND"
        if adx > 20 and atr_ratio > 1.4:
            return "EXPANSION"
        if atr_ratio < 0.7 and adx < 25:
            return "COMPRESSION"
        return "RANGE"
    except:
        return "RANGE"

def classify_market_narrative(df, ob, atr, side, rf_signal):
    """Classify market narrative with scoring."""
    reasons = []
    score = 0.0
    plus_di, minus_di, adx, adx_slope = get_di_components(df)
    if plus_di is not None:
        if side == "BUY" and plus_di > minus_di:
            score += 2.0
            reasons.append("DI+ dominance")
        elif side == "SELL" and minus_di > plus_di:
            score += 2.0
            reasons.append("DI- dominance")
        elif abs(plus_di - minus_di) < 5:
            reasons.append("DI tangled")
    if adx_slope > 1.5:
        score += 1.5
        reasons.append(f"ADX rising ({adx_slope:.1f})")
    elif adx_slope < -1.5:
        score -= 1.0
        reasons.append("ADX falling")
    vwap_n = get_vwap_narrative(df)
    if side == "BUY":
        if vwap_n["above"]:
            score += 1.5
            reasons.append("VWAP above")
        elif vwap_n["reclaim"]:
            score += 2.0
            reasons.append("VWAP reclaim")
    else:
        if vwap_n["below"]:
            score += 1.5
            reasons.append("VWAP below")
        elif vwap_n["reject"]:
            score += 2.0
            reasons.append("VWAP reject")
    pools = build_liquidity_pools(df)
    swept_h, swept_l = detect_sweep(df, pools)
    sweep_detected = (side == "BUY" and swept_l) or (side == "SELL" and swept_h)
    if sweep_detected:
        score += 2.5
        reasons.append("Liquidity sweep")
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    zone_strength = 0.0
    if side == "BUY" and supports:
        nearest_sup = max([s for s in supports if s <= df['close'].iloc[-1]], default=None)
        if nearest_sup:
            zone_strength = compute_enhanced_zone_strength(df, nearest_sup, "support", atr, ob, sweep_detected)
            score += zone_strength * 0.5
            reasons.append(f"Zone strength {zone_strength:.1f}")
    elif side == "SELL" and resistances:
        nearest_res = min([r for r in resistances if r >= df['close'].iloc[-1]], default=None)
        if nearest_res:
            zone_strength = compute_enhanced_zone_strength(df, nearest_res, "resistance", atr, ob, sweep_detected)
            score += zone_strength * 0.5
            reasons.append(f"Zone strength {zone_strength:.1f}")
    bos_up, bos_down = detect_bos(df)
    struct_shift = detect_structure_shift(df)
    if (side == "BUY" and (bos_up or struct_shift == "bullish_shift")):
        score += 2.0
        reasons.append("Bullish structure")
    elif (side == "SELL" and (bos_down or struct_shift == "bearish_shift")):
        score += 2.0
        reasons.append("Bearish structure")
    vol_state = classify_volume(df)
    if vol_state in ("expansion", "spike"):
        score += 1.5
        reasons.append("Volume expansion")
    elif vol_state == "exhaustion":
        score -= 1.0
        reasons.append("Volume exhaustion")
    if candle_rejection(df, side):
        score += 1.5
        reasons.append("Rejection candle")
    if detect_displacement(df, side, atr, vol_state, body_atr_threshold=0.8, volume_expansion_required=False):
        score += 1.5
        reasons.append("Displacement")
    if rf_signal == side:
        score += 1.5
        reasons.append("RF aligned")
    if adx is not None and adx < 18 and plus_di is not None and abs(plus_di - minus_di) < 6:
        score = 0
        reasons = ["CHOP market (ADX<18 + DI tangled)"]
    if score >= 9.0:
        classification = "REVERSAL_SNIPER" if (sweep_detected or zone_strength > 5) else "TREND_CONTINUATION"
        confidence = "HIGH"
    elif score >= 7.0:
        classification = "TREND_CONTINUATION" if (bos_up or bos_down or struct_shift) else "ACCUMULATION_LONG" if side == "BUY" else "DISTRIBUTION_SHORT"
        confidence = "MEDIUM"
    elif score >= 5.0:
        classification = "FAKE_BREAKOUT" if not sweep_detected else "LOW_CONFIDENCE"
        confidence = "LOW"
    else:
        classification = "CHOP_NO_TRADE"
        confidence = "NO_TRADE"
    return {
        "classification": classification,
        "confidence": confidence,
        "narrative_score": round(score, 2),
        "reasons": reasons,
        "sweep": sweep_detected,
        "zone_strength": zone_strength,
        "di_dominance": ("BUY" if plus_di > minus_di else "SELL") if plus_di is not None else "NEUTRAL",
        "adx_slope": adx_slope,
        "vwap_reclaim": vwap_n["reclaim"],
        "vwap_reject": vwap_n["reject"]
    }

def adjust_narrative_confidence(narrative, regime, side, trend_direction):
    """Adjust narrative confidence based on regime and trend alignment."""
    orig_conf = narrative["confidence"]
    score = narrative["narrative_score"]
    side_aligned = False
    if (trend_direction == "BULLISH" and side == "BUY") or (trend_direction == "BEARISH" and side == "SELL"):
        side_aligned = True
    final_conf = orig_conf
    final_class = narrative["classification"]
    if regime == "CHOP":
        return "NO_TRADE", "CHOP_NO_TRADE"
    if orig_conf == "NO_TRADE" or score < 5.0:
        return "NO_TRADE", "CHOP_NO_TRADE"
    if regime == "TREND":
        if side_aligned:
            if orig_conf == "HIGH":
                final_conf = "HIGH"
                final_class = "SNIPER"
            elif orig_conf == "MEDIUM":
                final_conf = "MEDIUM"
                final_class = "TREND"
            elif orig_conf == "LOW":
                if score >= 5.0:
                    final_conf = "MEDIUM"
                    final_class = "TREND"
                else:
                    final_conf = "NO_TRADE"
                    final_class = "NO_TRADE"
        else:
            if orig_conf == "HIGH":
                final_conf = "HIGH"
                final_class = "SNIPER"
            else:
                final_conf = "NO_TRADE"
                final_class = "NO_TRADE"
    elif regime in ("EXPANSION", "COMPRESSION"):
        if orig_conf == "HIGH":
            final_conf = "HIGH"
            final_class = "SNIPER"
        else:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
    else:
        if orig_conf == "HIGH":
            final_conf = "HIGH"
            final_class = "SNIPER"
        elif orig_conf == "MEDIUM" and side_aligned:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
        else:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
    return final_conf, final_class

def evaluate_with_narrative(symbol, side, price, atr_val, df, ob, rf_signal, existing_score=0):
    """Evaluate entry with narrative confidence."""
    regime = detect_market_regime(df)
    trend_dir = get_trend_direction(df)
    narrative = classify_market_narrative(df, ob, atr_val, side, rf_signal)
    final_conf, final_class = adjust_narrative_confidence(narrative, regime, side, trend_dir)
    narrative["confidence"] = final_conf
    narrative["classification"] = final_class
    narrative["regime"] = regime
    MEMORY[f"last_narrative_{symbol}"] = {**narrative, "timestamp": time.time(), "side": side}
    should_enter = final_conf in ("HIGH", "MEDIUM")
    if not should_enter:
        reason = f"{final_class} ({final_conf}) Regime={regime} Score={narrative['narrative_score']:.1f}"
        MEMORY.setdefault("no_entry_feed", []).append({
            "time": time.time(),
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "score": narrative["narrative_score"]
        })
        if len(MEMORY["no_entry_feed"]) > 20:
            MEMORY["no_entry_feed"] = MEMORY["no_entry_feed"][-20:]
        return False, None, narrative
    STATE["narrative_classification"] = final_class
    STATE["narrative_confidence"] = narrative["narrative_score"]
    STATE["confidence_level"] = final_conf
    return True, final_class, narrative

def check_institutional_entry(symbol, side, df, ob, atr, price):
    """Institutional entry gate with a canonical evidence fallback.

    Primary path: TradeIntelligence provides the richer sequence/timing/score.
    Fallback path: the engine's canonical liquidity/zone/structure evidence is
    used when the enrichment layer cannot validate a synthetic/sparse frame.
    The fallback still requires the institutional core:
      liquidity sweep -> MSS/BOS -> causal OB/FVG/zone -> displacement/rejection
      -> ADX -> anti-chase geometry.
    RF/volume remain supporting evidence rather than universal hard blockers.
    """
    if not is_valid_dataframe(df) or len(df) < 60:
        return False, None, "Insufficient structural history"

    side = str(side).upper()
    asset_class = AssetBehaviorProfile.resolve_asset_class(symbol)
    cfg = AssetBehaviorProfile.entry_config(asset_class)
    reasons = []
    ti = None
    ti_reason = ""

    try:
        if TRADE_INTELLIGENCE_AVAILABLE:
            ti = analyze_setup(df, side, price, atr, symbol)
            if ti and not ti.get("valid", False):
                ti_reason = str(ti.get("reason", "Trade intelligence did not validate"))
                # Do not discard the institutional core merely because the
                # enrichment layer cannot reconstruct one field on sparse data.
                ti = None
    except Exception as exc:
        ti_reason = str(exc)
        ti = None

    ev = (ti or {}).get("evidence", {}) or {}
    sweep = bool(ev.get("liquidity_sweep", False))
    structure = bool(ev.get("structure_bos", False) or ev.get("structure_mss", False))
    zone = ev.get("zone", {}) or {}
    fvg = bool(ev.get("fvg", False))
    disp = bool(ev.get("displacement", False))
    retest = str(ev.get("retest", "NONE"))

    # Canonical zone fallback. A strong, fresh engine-owned zone or an FVG is
    # treated as causal zone evidence; proximity keeps the entry local.
    zone_valid = float(zone.get("score", 0) or 0) >= 50 or fvg
    fallback_zone_price = None
    if not zone_valid:
        try:
            zones = get_smart_zones(symbol, df, ob)
            if side == "BUY":
                candidates = zones.get("buy_zones") or []
                if candidates:
                    z = candidates[0]
                    fallback_zone_price = float(z.get("price", 0) or 0)
                    zone_valid = (
                        float(z.get("strength", 0) or 0) >= 5
                        and fallback_zone_price > 0
                        and abs(price - fallback_zone_price) / max(abs(price), 1e-12) < 0.003
                    )
            else:
                candidates = zones.get("sell_zones") or []
                if candidates:
                    z = candidates[0]
                    fallback_zone_price = float(z.get("price", 0) or 0)
                    zone_valid = (
                        float(z.get("strength", 0) or 0) >= 5
                        and fallback_zone_price > 0
                        and abs(price - fallback_zone_price) / max(abs(price), 1e-12) < 0.003
                    )
        except Exception:
            pass

    if not zone_valid:
        try:
            fv = detect_fvg(df)
            if side == "BUY" and fv and fv[0] == "bullish":
                zone_valid = float(fv[1]) <= float(price) <= float(fv[2])
            elif side == "SELL" and fv and fv[0] == "bearish":
                zone_valid = float(fv[1]) <= float(price) <= float(fv[2])
        except Exception:
            pass

    if not zone_valid:
        try:
            ob_level = detect_order_block(df, side)
            if ob_level:
                edge = float(ob_level["low"] if side == "BUY" else ob_level["high"])
                zone_valid = abs(price - edge) / max(abs(price), 1e-12) < 0.003
                if zone_valid:
                    fallback_zone_price = edge
        except Exception:
            pass

    if sweep:
        reasons.append("LIQUIDITY_SWEEP")
    if structure:
        reasons.append("MSS_BOS")
    if zone_valid:
        reasons.append("CAUSAL_ZONE")
    if disp:
        reasons.append("DISPLACEMENT")
    if retest in ("RETEST_CONFIRMED", "MICRO_PULLBACK"):
        reasons.append(retest)

    # Fallback to the canonical engine evidence where the richer layer is
    # unavailable or cannot reconstruct sparse/synthetic structure.
    if not sweep:
        ctx = detect_liquidity_context(df, lookback=int(cfg["sweep_bars"]))
        sweep = (side == "BUY" and ctx == "sell_side_taken") or (
            side == "SELL" and ctx == "buy_side_taken"
        )
        if sweep:
            reasons.append("LIQUIDITY_SWEEP")

    if not structure:
        shift = detect_structure_shift(df)
        bos_up, bos_down = detect_bos(df)
        structure = (
            (side == "BUY" and (shift == "bullish_shift" or bos_up))
            or (side == "SELL" and (shift == "bearish_shift" or bos_down))
        )
        if structure:
            reasons.append("MSS_BOS")

    if not sweep:
        return False, None, "No recent directional liquidity sweep"
    if not structure:
        return False, None, "No MSS/BOS after liquidity event"
    if not zone_valid:
        return False, None, "No causal OB/zone/FVG"

    if not disp:
        try:
            vol_state_for_disp = classify_volume(df)
            disp = detect_displacement(
                df, side, atr, vol_state_for_disp,
                body_atr_threshold=0.8,
                volume_expansion_required=False,
            )
        except Exception:
            disp = False

    rejection = candle_rejection(df, side)
    if not (disp or rejection):
        return False, None, "No displacement or rejection confirmation"
    if disp:
        reasons.append("DISPLACEMENT")
    if rejection:
        reasons.append("REJECTION")

    adx_series = compute_adx(df)
    adx_now = float(adx_series.iloc[-1]) if adx_series is not None and len(adx_series) else 0.0
    if adx_now < float(cfg["min_adx"]) or adx_now > float(cfg["max_adx"]):
        return False, None, f"ADX {adx_now:.1f} outside [{cfg['min_adx']},{cfg['max_adx']}]"
    reasons.append(f"ADX={adx_now:.1f}")

    vol_state = classify_volume(df)
    reasons.append(
        f"VOLUME_{vol_state.upper()}" if vol_state in ("expansion", "spike")
        else f"VOLUME_{vol_state.upper()}_SUPPORT"
    )

    rf = RFEngine(20, 3.5).compute(df)
    reasons.append("RF_ALIGNED" if rf.get("signal") == side else "RF_COUNTER_OR_NEUTRAL")

    # Anti-chase protection applies to both primary and fallback paths.
    if ti:
        timing = str(ti.get("timing", "WAIT_RETEST"))
        distance_atr = float(ev.get("distance_atr", 999) or 999)
        if timing == "LATE_OR_DISTRIBUTION" or distance_atr > 2.5:
            return False, None, "Entry is late / distribution-prone"
        if timing == "WAIT_RETEST" and not zone.get("in_zone", False) and distance_atr > 1.25:
            return False, None, "Waiting for zone mitigation/retest"
        score = float(ti.get("score", 0) or 0)
        if score < float(cfg["ready_score"]):
            core = sum((
                bool(sweep), bool(structure), bool(zone_valid), bool(disp),
                retest in ("RETEST_CONFIRMED", "MICRO_PULLBACK"),
            ))
            if not (core >= 4 and score >= float(cfg["ready_score"]) - 8):
                return False, None, f"Institutional score {score:.1f} below {cfg['ready_score']}"
    else:
        # When enrichment is unavailable, retain the canonical anti-chase rule
        # rather than inventing a score. This is still a strict institutional
        # sequence, just without the optional composite enrichment.
        if fallback_zone_price:
            move_from_zone_pct = abs(price - fallback_zone_price) / max(abs(fallback_zone_price), 1e-12) * 100.0
            if move_from_zone_pct > 0.5:
                return False, None, f"Price moved {move_from_zone_pct:.2f}% from zone, too late"
        try:
            last_candle = df.iloc[-1]
            candle_range_pct = (
                (float(last_candle["high"]) - float(last_candle["low"]))
                / max(float(last_candle["close"]), 1e-12) * 100.0
            )
            if candle_range_pct > 1.5 * (float(atr) / max(abs(price), 1e-12) * 100.0):
                return False, None, "Large displacement candle already occurred, too late"
        except Exception:
            pass
        if ti_reason:
            reasons.append("INTEL_FALLBACK")

    return True, "INSTITUTIONAL_SNIPER", " | ".join(dict.fromkeys(reasons))

# ========== SMART OPPORTUNITY SELECTION (REPLACED) ==========
def smart_opportunity_selection():
    """Select the best opportunity using the new strategy."""
    candidates = []
    for c in MEMORY.get("scanner_v2_buy", [])[:5]:
        candidates.append({"symbol": c["symbol"], "side": "BUY", "score": c["score"], "source": "v2"})
    for c in MEMORY.get("scanner_v2_sell", [])[:5]:
        candidates.append({"symbol": c["symbol"], "side": "SELL", "score": c["score"], "source": "v2"})
    for c in MEMORY.get("rf_watchlist", [])[:10]:
        if c.get("rf_signal") in ("BUY", "SELL"):
            candidates.append({"symbol": c["symbol"], "side": c["rf_signal"], "score": c["score"], "source": "rf"})
    seen = {}
    for cand in candidates:
        sym = cand["symbol"]
        if sym not in seen or cand["score"] > seen[sym]["score"]:
            seen[sym] = cand
    candidates = list(seen.values())
    best_setup = None
    best_score = -1
    for cand in candidates[:15]:
        try:
            sym = cand["symbol"]
            side = cand["side"]
            df = get_ohlcv_safe(sym, 100)
            if df is None or not validate_dataframe(df, 80):
                continue
            df.symbol = sym
            ob = get_orderbook_cached(sym, limit=10)
            atr = compute_atr(df).iloc[-1] if len(df) > 14 else df['close'].iloc[-1] * 0.01
            price = df['close'].iloc[-1]
            # Check institutional entry
            should_enter, classification, reason_str = check_institutional_entry(sym, side, df, ob, atr, price)
            if not should_enter:
                continue
            # Narrative evaluation
            should_enter_narr, final_class, narrative = evaluate_with_narrative(sym, side, price, atr, df, ob, side)
            if not should_enter_narr:
                continue
            # Compute total score
            total = narrative.get('narrative_score', 0) + (2 if classification == "INSTITUTIONAL_SNIPER" else 0)
            if total > best_score:
                best_score = total
                best_setup = (sym, side, total, narrative, df, ob, atr, classification, reason_str)
        except Exception:
            continue
    if best_setup and best_score >= 7:  # threshold from test cv new.py
        sym, side, score, narrative, df, ob, atr, classification, reason_str = best_setup
        price = df['close'].iloc[-1]
        sl, tp1, tp2 = compute_sl_tp(price, side, "REVERSAL", atr, df)
        reason_final = f"{reason_str} | NARR={narrative['classification']} | score={score:.1f}"
        ok = execute_entry(side, sym, price, sl, tp1, tp2, score, reason_final, atr,
                           trade_type="INSTITUTIONAL", entry_type="NARRATIVE", classification=classification)
        if ok:
            return True
    return False

# ========== MODIFIED EXECUTE_ENTRY ==========
def apply_atr_tp_floor(side, price, tp1, tp2, atr, asset_class="CRYPTO"):
    """Phase-3 (G1): anti-scalp ATR TP floor.

    Fixed-percentage TP1/TP2 from queued/institutional callers can sit a few
    pips away on wide-ATR assets (sure-fire scalp fills that undermine the
    thesis). Enforce an asset-class ATR floor so a target must be a real move
    away: BUY -> max(fixed, entry + tp_x_atr*ATR), SELL -> min(fixed, entry - tp_x_atr*ATR).
    The floor can only WIDEN the target (never tighten it).
    Returns (floor_tp1, floor_tp2).
    """
    cfg = AssetBehaviorProfile.get(asset_class)
    tp1_pts = float(cfg.get("tp1_atr", 2.5)) * float(atr)
    tp2_pts = float(cfg.get("tp2_atr", 4.0)) * float(atr)
    if str(side).upper() == "BUY":
        return max(float(tp1), float(price) + tp1_pts), max(float(tp2), float(price) + tp2_pts)
    return min(float(tp1), float(price) - tp1_pts), min(float(tp2), float(price) - tp2_pts)


def execute_entry(side, symbol, price, sl, tp1, tp2, score, reason, atr_val, trade_type, entry_type, classification):
    """Final execution gate. Strategy intelligence decides *whether* the setup
    is institutionally mature; this function remains the sole order-entry
    authority and preserves the exchange/portfolio safety boundary."""
    df = get_ohlcv_safe(symbol, INSTITUTIONAL_OHLCV_DEPTH)
    # Execution only needs the canonical OHLCV columns. Timestamp is required
    # by exchange-fetch validation, but it is not an execution prerequisite
    # (and deterministic portfolio simulators intentionally omit it). Keep the
    # strict timestamp validation at the data-ingestion boundary; do not reject
    # an otherwise valid OHLCV frame here.
    def _execution_ohlcv_ok(frame):
        return (isinstance(frame, pd.DataFrame) and len(frame) >= 10 and
                all(c in frame.columns for c in ("open", "high", "low", "close", "volume")))
    if not _execution_ohlcv_ok(df):
        df = get_ohlcv_safe(symbol, 100)
    if not _execution_ohlcv_ok(df):
        log_execution(f"[ENTRY] No valid OHLCV for {symbol}; refusing entry", "WARN")
        return False

    asset_class = AssetBehaviorProfile.resolve_asset_class(symbol)
    cfg = AssetBehaviorProfile.entry_config(asset_class)

    # Session-aware execution: understand the liquidity window of the actual
    # instrument (e.g. USD/CAD) without confusing an underlying market session
    # with BingX contract availability. Hard blocking is opt-in; by default the
    # session state is evidence/timing only.
    if session_allows_entry is not None:
        try:
            _session_ok, _session_ctx = session_allows_entry(symbol)
            STATE["market_session"] = _session_ctx
            if not _session_ok:
                log_execution(
                    f"[ENTRY] {symbol} rejected by configured session gate: "
                    f"{_session_ctx.get('state')} pair={_session_ctx.get('pair')}", "WARN")
                return False
            if _session_ctx.get("state") == "QUIET":
                log_execution(
                    f"[SESSION] {symbol} outside preferred liquidity window; "
                    f"continuing only because hard gate is OFF", "INFO",
                    debounce_key=f"session_quiet_{symbol}", debounce_sec=300)
        except Exception as _session_err:
            log_execution(
                f"[SESSION] context error for {symbol}: {_session_err}", "WARN",
                debounce_key=f"session_ctx_{symbol}", debounce_sec=300)
    try:
        adx_series = compute_adx(df)
        adx_val = float(adx_series.iloc[-1]) if adx_series is not None and len(adx_series) else 0.0
    except Exception:
        adx_val = 0.0
    if not (float(cfg["min_adx"]) <= adx_val <= float(cfg["max_adx"])):
        log_execution(f"[ENTRY] {asset_class} ADX {adx_val:.1f} outside [{cfg['min_adx']},{cfg['max_adx']}]", "WARN")
        return False

    # Execution-layer safety gate.
    # IMPORTANT ARCHITECTURAL CONTRACT:
    # The institutional sequence is decided upstream by the discovery/decision
    # layer (check_institutional_entry / scanner / radar / queue). execute_entry
    # remains the sole order-entry authority, but it must not re-run the full
    # mutable institutional reconstruction on every execution call. The old
    # execution contract intentionally used only ADX + directional liquidity
    # authenticity here. Re-running the full gate caused valid portfolio/slot
    # lifecycle fixtures to be rejected after the decision had already been made.
    # This preserves the institutional gate upstream while keeping execution
    # deterministic and backward-compatible.
    is_news = str(trade_type).upper() == "NEWS" and str(classification).upper() == "NEWS"
    if not is_news:
        try:
            liquidity_ctx = detect_liquidity_context(df, lookback=int(cfg.get("sweep_bars", 10)))
        except Exception:
            liquidity_ctx = None
        expected_ctx = "sell_side_taken" if side == "BUY" else "buy_side_taken"
        if liquidity_ctx != expected_ctx:
            log_execution(
                f"[ENTRY] {side} requires {expected_ctx}, got {liquidity_ctx} – execution blocked",
                "WARN",
            )
            return False
        if SWEEP_AUTHENTICITY:
            try:
                sweep_grad, sweep_bar = get_sweep_authenticity(df, side, lookback=int(cfg.get("sweep_bars", 10)))
                if sweep_grad == "fake":
                    log_execution(
                        f"[ENTRY] {side} sweep on bar {sweep_bar} is FAKE – execution blocked",
                        "WARN",
                    )
                    return False
            except Exception:
                pass
    else:
        # News trades still require live price direction; news alone never
        # becomes an order.
        last = df.iloc[-1]
        body = abs(float(last['close']) - float(last['open']))
        directional = (side == "BUY" and float(last['close']) > float(last['open'])) or (side == "SELL" and float(last['close']) < float(last['open']))
        if not directional and body < float(atr_val or 0) * 0.25:
            log_execution(f"[NEWS_ENTRY] {symbol} lacks immediate price-direction confirmation", "WARN")
            return False

    # Rich entry snapshot: style + phase + zone behaviour + pullback/retest are
    # captured at the exact entry decision for the Trade Management Board.
    try:
        intel = analyze_setup(df, side, price, atr_val, symbol) if TRADE_INTELLIGENCE_AVAILABLE else {}
    except Exception:
        intel = {}
    if intel:
        STATE["trade_intelligence"] = intel
        STATE["trade_style"] = intel.get("trade_style", "SCALP")
        STATE["entry_timing"] = intel.get("timing", "WAIT_RETEST")
        STATE["market_phase"] = intel.get("phase", "UNKNOWN")
        STATE["zone_behaviour"] = intel.get("behaviour", "NEUTRAL")
        log_execution(
            f"[ENTRY_INTEL] {symbol} {side} | STYLE={STATE['trade_style']} "
            f"TIMING={STATE['entry_timing']} PHASE={STATE['market_phase']} "
            f"ZONE={STATE['zone_behaviour']} SCORE={intel.get('score',0):.1f}",
            "INFO"
        )

    # ---- Final entry-quality authority (env-gated, default OFF) ----
    # entry_quality_assessment is the documented "final authority" (header
    # section SURGICAL ENTRY QUALITY ENHANCEMENTS). It is stricter than the
    # base pipeline (trap risk, liquidity authenticity, opposing OB, early
    # expansion state), so enabling it can reject frames the legacy path
    # accepted. Kept behind ENTRY_QUALITY_AUTHORITY for a safe reversible
    # rollout; when ON, only an explicit REJECT blocks here.
    if os.getenv("ENTRY_QUALITY_AUTHORITY", "false").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            atr_local_gate = atr_val if atr_val and atr_val > 0 else float(compute_atr(df).iloc[-1])
            ob_gate = get_orderbook_cached(symbol, limit=10)
            qa = entry_quality_assessment(
                symbol, side, price, df, ob_gate, atr_local_gate,
                existing_score=float(score), classification=str(classification),
                trade_type=str(trade_type), entry_type=str(entry_type),
            )
            if qa.get("decision") == "REJECT":
                log_execution(
                    f"[ENTRY_QUALITY_AUTHORITY] {symbol} {side} REJECTED: "
                    f"{qa.get('reason', '')} (quality={qa.get('quality_score', 0):.0f})", "WARN")
                return False
        except Exception as gate_err:
            log_execution(
                f"[ENTRY_QUALITY_AUTHORITY] {symbol} {side} gate error, "
                f"falling through: {gate_err}", "WARN")

    # ---- Position sizing: every trade = 10% of AVAILABLE FREE BALANCE ----
    # Uniform across all classifications so each open position commits the
    # same 10% slice of the free balance (matches POSITION_MARGIN_PCT=0.10
    # in the portfolio risk model). The free balance is reduced on open and
    # restored (+ PnL) on close, so availability truly reflects committed margin.
    free_bal = get_free_balance_safe() if not PAPER_MODE else paper["balance"]
    usable_balance = free_bal * BALANCE_SAFETY_FACTOR
    balance = free_bal

    margin_percent = POSITION_MARGIN_PCT
    if classification in ("SNIPER", "INSTITUTIONAL_SNIPER"):
        trade_type_label = "STRONG"
    elif classification == "LOW":
        trade_type_label = "LOW_CONF"
    else:
        trade_type_label = "NORMAL"

    margin = balance * margin_percent
    # ---- REAL portfolio margin-cap enforcement (paper ledger) ----
    # Blocks ANY new entry whose projected committed exposure would breach the
    # portfolio cap, regardless of how the entry is initiated (manager path,
    # scanner path, manual override). Mirrors portfolio/risk.py so the 6th
    # position fits under the cap and nothing beyond it can commit.
    if PAPER_MODE and isinstance(paper, dict):
        committed = float(paper.get("committed_margin", 0.0))
        equity = float(paper.get("balance", 0.0)) + committed
        projected = committed + margin
        if equity > 0 and projected > equity * PORTFOLIO_MARGIN_CAP_PCT + 1e-9:
            log_execution(
                f"[MARGIN_CAP] {symbol} {side} rejected: projected exposure "
                f"{projected:.2f} > {PORTFOLIO_MARGIN_CAP_PCT:.0%} of equity {equity:.2f} "
                f"(committed {committed:.2f} + {margin:.2f})",
                "WARN",
            )
            return False
    notional = margin * LEVERAGE
    qty = notional / price
    log_execution(f"[SIZING]\nFree USDT: {free_bal:.2f}\nUsable (x{BALANCE_SAFETY_FACTOR}): {balance:.2f}\nType: {trade_type_label}\nMargin: {margin:.2f}\nLeverage: {LEVERAGE}X\nNotional: {notional:.2f}\nFinal Qty: {qty:.6f}", "INFO")

    # ---- Build thesis and confidence (same as before but with new checks) ----
    df_local = get_ohlcv_safe(symbol, 100)  # already fetched above
    ob_local = get_orderbook_cached(symbol, limit=10)
    atr_local = atr_val  # already computed

    # ---- Phase-3 (G1): anti-scalp ATR TP floor ----
    # Persisted separately (dynamic_tp1/dynamic_tp2) so legacy STATE values stay
    # intact; the management layer prefers these targets (TP1_DELAY reads
    # dynamic_tp1, the TP2 percent is widened by tp2_atr).
    dyn_tp1, dyn_tp2 = apply_atr_tp_floor(
        side, price, tp1, tp2, atr_local,
        asset_class=AssetBehaviorProfile.resolve_asset_class(symbol),
    )
    STATE["dynamic_tp1"] = dyn_tp1
    STATE["dynamic_tp2"] = dyn_tp2
    if dyn_tp1 != float(tp1) or dyn_tp2 != float(tp2):
        log_execution(
            f"[TP_FLOOR] {symbol} {side} TP widened by ATR floor: "
            f"tp1 {tp1:.4f}->{dyn_tp1:.4f}, tp2 {tp2:.4f}->{dyn_tp2:.4f}",
            "INFO",
        )

    # Build market state (similar to original execute_entry)
    plus_di, minus_di, adx_now, adx_slope = get_di_components(df_local) if df_local is not None else (None, None, None, None)
    di_dominance = False
    if plus_di is not None and minus_di is not None:
        di_dominance = (side == "BUY" and plus_di > minus_di) or (side == "SELL" and minus_di > plus_di)
    weak_pullback = False
    if df_local is not None:
        last = df_local.iloc[-1]
        if side == "BUY":
            if last['close'] < last['open'] and abs(last['close'] - last['open']) < atr_local * 0.3:
                weak_pullback = True
        else:
            if last['close'] > last['open'] and abs(last['close'] - last['open']) < atr_local * 0.3:
                weak_pullback = True
    structure_aligned = False
    struct_shift = detect_structure_shift(df_local) if df_local is not None else None
    if side == "BUY" and struct_shift == "bullish_shift":
        structure_aligned = True
    elif side == "SELL" and struct_shift == "bearish_shift":
        structure_aligned = True
    counter_displacement = 0.0
    if df_local is not None:
        last = df_local.iloc[-1]
        if side == "SELL" and last['close'] > last['open']:
            body = abs(last['close'] - last['open'])
            if body > atr_local * 0.6:
                counter_displacement = body / atr_local
        elif side == "BUY" and last['close'] < last['open']:
            body = abs(last['close'] - last['open'])
            if body > atr_local * 0.6:
                counter_displacement = body / atr_local
    market_state = {
        "adx": adx_now if adx_now is not None else 20.0,
        "regime": MEMORY.get("regime", "UNKNOWN"),
        "di_dominance": di_dominance,
        "weak_pullback": weak_pullback,
        "structure_aligned": structure_aligned,
        "counter_displacement": counter_displacement,
        "trend_health": trend_engine.get_trend_health(df_local, side) if df_local is not None else 5
    }
    narrative = {"classification": classification}
    entry_context = {"price": price, "atr": atr_local}
    thesis = _thesis_engine.build_thesis(symbol, side, trade_type, market_state, narrative, entry_context)
    STATE["trade_thesis"] = thesis.__dict__

    regime_class = MarketRegimeClassifier.classify(df_local) if df_local is not None else "UNKNOWN"
    di_spread = abs(plus_di - minus_di) if plus_di is not None else 0
    location_quality = "mid"
    initial_conf = ConfidenceEngine.calculate_initial_confidence(score, narrative.get("narrative_score", 0), regime_class, market_state["adx"], di_spread, location_quality)

    if df_local is not None:
        smart_money = SmartMoneyEngine.analyze_smart_money(df_local)
        momentum = MomentumFlowEngine.analyze_momentum_flow(df_local)
        dominance_weight = 0.7 if smart_money["smart_money_dominant"] else 0.3
        initial_conf += (dominance_weight - 0.5) * 12
        if momentum["trend_expansion"]:
            initial_conf += 8
        if momentum["momentum_decay"]:
            initial_conf -= 12
        dist_risk = smart_money["distribution_risk"] / 100.0
        initial_conf -= dist_risk * 15
        if smart_money["retail_euphoria"]:
            initial_conf -= 10
        continuation_strength = momentum.get("continuation_strength", 50)
        initial_conf = ConfidenceEngine.apply_institutional_modifiers(initial_conf, smart_money, momentum, continuation_strength)
        initial_conf = max(0, min(95, initial_conf))

    # Add narrative confidence boost
    intent_info = MEMORY.get(f"intent_{symbol}", {})
    intent_score = intent_info.get("score", 0)
    if intent_score >= 85:
        initial_conf += 15
    elif intent_score >= 75:
        initial_conf += 10
    initial_conf = min(100, initial_conf)

    STATE["current_confidence"] = initial_conf
    STATE["market_regime"] = regime_class

    # ---- Execute trade ----
    if PAPER_MODE:
        paper["position"] = {"side": side, "entry": price, "qty": qty, "remaining_qty": qty}
        STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty, "remaining_qty": qty,
            "sl": sl, "current_symbol": symbol, "tp1_done": False, "trail_activated": False,
            "peak": 0.0, "atr": atr_local, "entry_time": time.time(), "entry_reasons": [reason],
            "trade_score": score, "partial_closed": False, "tp1_price": tp1, "tp2_price": tp2,
            "trade_type": trade_type, "entry_type": entry_type, "be_done": False,
            "tp1_hit": False, "tp2_hit": False, "trail_stop": 0.0,
            "smart_tightened": False, "smart_partial_done": False, "smart_exit_triggered": False,
            "roe_pct": 0.0, "mark_price": price,
            "narrative_classification": STATE.get("narrative_classification", ""),
            "narrative_confidence": STATE.get("narrative_confidence", 0.0),
            "confidence_level": STATE.get("confidence_level", ""),
            "trade_thesis": thesis.__dict__,
            "current_confidence": initial_conf,
            "market_regime": regime_class,
            "adx_live": market_state["adx"],
            "di_plus_live": plus_di if plus_di else 0,
            "di_minus_live": minus_di if minus_di else 0,
            "trade_personality": "NEUTRAL",
            "institutional_flow": "NEUTRAL",
            "synthetic_sl": sl,
            "synthetic_tp1": tp1,
            "max_price": price,
            "min_price": price,
            "peak_roe": 0.0,
            "peak_price": price,
            "peak_unrealized_pnl": 0.0,
            "drawdown_from_peak": 0.0,
            "tp1_hold_score": 10,
            "exit_warning": 0,
            "runner_mode": False,
            "entry_atr": atr_local
        })
        TRADE_STATE.update({
            "in_position": True, "symbol": symbol, "side": side, "entry": price, "qty": qty,
            "tp1_hit": False, "trail_on": False, "last_update_ts": time.time()
        })
        _live_manager.start_trade(symbol, side, price, qty, sl, tp1, tp2)
        _live_manager.set_entry_atr(atr_local)
        _live_manager._ensure_position_profile(symbol, price, side, atr_local,
                                               classification=classification,
                                               trade_type=trade_type)
        STATE["dynamic_manager"] = DynamicTradeManager(
            symbol, side, price, qty, atr_local, sl, tp1, tp2
        )
        STATE["margin"] = margin
        STATE["qty"] = qty
        if paper["balance"] >= margin:
            paper["balance"] -= margin
            paper["committed_margin"] = paper.get("committed_margin", 0.0) + margin
            log_execution(f"[PAPER MARGIN] committed {margin:.2f} USDT (10% of free), free={paper['balance']:.2f}, total_committed={paper['committed_margin']:.2f}", "INFO")
        else:
            paper["balance"] = 0.0
            log_execution(f"[PAPER MARGIN] WARN: free balance {free_bal:.2f} below required margin {margin:.2f} — committed what was available", "WARN")
        update_position_dashboard(symbol, side, price, qty)
        log_execution(f"PAPER {entry_type} {side} {qty:.6f} @ {price} | {trade_type_label} | {reason}", "SUCCESS")
        tg_entry(side, symbol, price, sl, tp1, score, reason, entry_type)
        log_execution(f"[EXECUTION] {symbol} {side} executed (paper) at {price:.4f}", "SUCCESS")
        return True

    sym = normalize_symbol(symbol)
    market = ex.market(sym)
    min_qty = market['limits']['amount']['min']
    if qty < min_qty:
        log_execution(f"SKIP: computed qty {qty:.6f} below minimum {min_qty}", "WARN")
        return False
    precision = market['precision']['amount']
    qty = math.floor(qty / precision) * precision
    if qty <= 0:
        log_execution(f"SKIP: qty rounded to zero", "WARN")
        return False
    log_execution(f"Position sizing final: free_balance={free_bal:.2f}, usable={balance:.2f}, classification={classification}, margin_percent={margin_percent*100:.0f}%, margin={margin:.2f}, notional={notional:.2f}, qty={qty:.6f}", "INFO")
    order = open_position(side, qty, symbol, client_order_id=None)
    if order:
        STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty, "remaining_qty": qty,
            "sl": sl, "current_symbol": symbol, "tp1_done": False, "trail_activated": False,
            "peak": 0.0, "atr": atr_local, "entry_time": time.time(), "entry_reasons": [reason],
            "trade_score": score, "partial_closed": False, "tp1_price": tp1, "tp2_price": tp2,
            "trade_type": trade_type, "entry_type": entry_type, "be_done": False,
            "tp1_hit": False, "tp2_hit": False, "trail_stop": 0.0,
            "smart_tightened": False, "smart_partial_done": False, "smart_exit_triggered": False,
            "roe_pct": 0.0, "mark_price": price,
            "narrative_classification": STATE.get("narrative_classification", ""),
            "narrative_confidence": STATE.get("narrative_confidence", 0.0),
            "confidence_level": STATE.get("confidence_level", ""),
            "trade_thesis": thesis.__dict__,
            "current_confidence": initial_conf,
            "market_regime": regime_class,
            "adx_live": market_state["adx"],
            "di_plus_live": plus_di if plus_di else 0,
            "di_minus_live": minus_di if minus_di else 0,
            "trade_personality": "NEUTRAL",
            "institutional_flow": "NEUTRAL",
            "synthetic_sl": sl,
            "synthetic_tp1": tp1,
            "max_price": price,
            "min_price": price,
            "peak_roe": 0.0,
            "peak_price": price,
            "peak_unrealized_pnl": 0.0,
            "drawdown_from_peak": 0.0,
            "tp1_hold_score": 10,
            "exit_warning": 0,
            "runner_mode": False,
            "entry_atr": atr_local
        })
        TRADE_STATE.update({
            "in_position": True, "symbol": symbol, "side": side, "entry": price, "qty": qty,
            "tp1_hit": False, "trail_on": False, "last_update_ts": time.time()
        })
        _live_manager.start_trade(symbol, side, price, qty, sl, tp1, tp2)
        _live_manager.set_entry_atr(atr_local)
        _live_manager._ensure_position_profile(symbol, price, side, atr_local,
                                               classification=classification,
                                               trade_type=trade_type)
        STATE["dynamic_manager"] = DynamicTradeManager(
            symbol, side, price, qty, atr_local, sl, tp1, tp2
        )
        if order.get("recovered"):
            # Order TIMEOUT -> exchange position adopted -> management now on.
            log_execution(
                f"[TRADE_MANAGEMENT] symbol={symbol} side={side} "
                f"status=ACTIVE management_initialized=true", "SUCCESS")
        update_position_dashboard(symbol, side, price, qty)
        log_execution(f"LIVE {entry_type} {side} {qty:.6f} @ {price} | {trade_type_label} | {reason}", "SUCCESS")
        tg_entry(side, symbol, price, sl, tp1, score, reason, entry_type)
        log_execution(f"[EXECUTION] {symbol} {side} executed at {price:.4f}", "SUCCESS")
        time.sleep(1)
        sync_position_state(symbol)
        return True
    else:
        return False

# ========== INSTITUTIONAL LIQUIDITY NARRATIVE ENGINE ==========
def equal_levels_points(highs, lows, tolerance=0.002):
    def cluster(points, tol):
        if not points:
            return []
        points = sorted(points)
        clusters = []
        current = [points[0]]
        for p in points[1:]:
            if abs(p - current[-1]) / current[-1] < tol:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)
        return clusters
    high_clusters = cluster(highs, tolerance)
    low_clusters = cluster(lows, tolerance)
    eq_highs = []
    eq_lows = []
    for cl in high_clusters:
        if len(cl) >= 2:
            eq_highs.extend([sum(cl)/len(cl)])
    for cl in low_clusters:
        if len(cl) >= 2:
            eq_lows.extend([sum(cl)/len(cl)])
    return eq_highs, eq_lows

def find_swing_points(df, window=3):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < window*2:
        return [], []
    highs = df['high'].values
    lows = df['low'].values
    swing_highs = []
    swing_lows = []
    for i in range(window, len(df)-window):
        if highs[i] == max(highs[i-window:i+window+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-window:i+window+1]):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows

def detect_equal_highs_lows(df, lookback=50):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback:
        return False, False
    sub = df.iloc[-lookback:]
    sh, sl = find_swing_points(sub, window=2)
    return equal_levels_points(sh, sl)

def detect_order_block(df, side, lookback=4):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < lookback+2:
        return None
    atr = compute_atr(df).iloc[-1]
    move = abs(df['close'].iloc[-1] - df['close'].iloc[-2])
    if move < atr * 1.2:
        return None
    for i in range(2, lookback+2):
        if i >= len(df):
            break
        candle = df.iloc[-i]
        if side == "BUY" and candle['close'] < candle['open']:
            return {"low": candle['low'], "high": candle['high'], "idx": -i}
        elif side == "SELL" and candle['close'] > candle['open']:
            return {"low": candle['low'], "high": candle['high'], "idx": -i}
    return None

def detect_fvg(df, threshold=0.001):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if curr['low'] > prev['high'] * (1+threshold):
        return ("bullish", prev['high'], curr['low'])
    elif curr['high'] < prev['low'] * (1-threshold):
        return ("bearish", curr['high'], prev['low'])
    return None

# ============================================================================
# IFVG (INVERSE FAIR VALUE GAP) ENGINE  -  warning only, never an order signal
# ----------------------------------------------------------------------------
# Tracks the full fair-value-gap lifecycle for a candidate/position:
#     NORMAL FVG -> MITIGATED FVG -> INVALIDATED FVG -> INVERSE FVG
# An inverse FVG is a zone whose original polarity was exhausted; price now
# treats it as the OPPOSITE magnet (support that flipped into supply, or
# resistance that flipped into demand). The engine reports retest evidence
# (NONE/PROBING/SWEEP/REJECTED/BROKEN) so callers can distinguish a real
# rejection from a temporary breakout and, inside a live position, decide
# between HOLD and reversal handling instead of a blind exit.
#
# Guarantees (IFVG_WARNING phase-3 manual, section 11):
#   - never books an entry/exit by itself;
#   - blocking = inverse zone ahead within block distance AND not consumed;
#   - a BROKEN retest (price closed through the whole flipped zone) consumes
#     the inversion -> warning is neutralized (penalty = 0);
#   - directional: only zones ahead of the reference price matter per side;
#   - penalty scales with proximity and retest confirmation, capped by
#     IFVG_PENALTY_MAX (default 0.30).
# ============================================================================

IFVG_ENABLED = os.getenv("IFVG_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
IFVG_BLOCK_DISTANCE_ATR = float(os.getenv("IFVG_BLOCK_DISTANCE_ATR", "1.5"))
IFVG_PENALTY_MAX = float(os.getenv("IFVG_PENALTY_MAX", "0.30"))
IFVG_LOOKBACK = int(os.getenv("IFVG_LOOKBACK", "60"))
IFVG_MIN_WIDTH_ATR = float(os.getenv("IFVG_MIN_WIDTH_ATR", "0.5"))

_IFVG_STATE_NORMAL = "NORMAL"
_IFVG_STATE_MITIGATED = "MITIGATED"
_IFVG_STATE_INVALIDATED = "INVALIDATED"
_IFVG_STATE_INVERSE = "INVERSE"


def _ifvg_atr(df, period=14):
    try:
        return float(compute_atr(df, period).iloc[-1])
    except Exception:
        return 0.0


def _ifvg_retest(df, start, n, side, bottom, top, window=5):
    """Retest semantics on the FLIPPED (inverse) side of the zone.

    Bull-born gap -> supply above: a probe comes from below; a close back
        below `bottom` is a real REJECTION, a wick above `top` that closes
        back inside is a SWEEP (false breakout), a close above `top` means the
        inversion was consumed -> BROKEN.
    Bear-born gap -> demand below: REJECTED on close above `top`, SWEEP on a
        wick below `bottom` closing back inside, BROKEN on close below `bottom`.
    """
    end = min(n, start + int(window))
    seen_probe = False
    has_sweep = False
    has_break = False
    rejection = False
    for k in range(start, end):
        h = float(df["high"].iloc[k])
        lo = float(df["low"].iloc[k])
        cl = float(df["close"].iloc[k])
        probing = (h > bottom and lo < top)
        if side == "BULLISH":
            if cl > top and h >= bottom:
                has_break = True
            elif h > top and cl <= top:
                has_sweep = True
            if probing:
                seen_probe = True
                if cl < bottom:
                    rejection = True
        else:
            if cl < bottom and lo <= top:
                has_break = True
            elif lo < bottom and cl >= bottom:
                has_sweep = True
            if probing:
                seen_probe = True
                if cl > top:
                    rejection = True
    if has_break:
        return "BROKEN"
    if has_sweep:
        return "SWEEP"
    if rejection:
        return "REJECTED"
    if seen_probe:
        return "PROBING"
    return "NONE"


def _ifvg_state_and_retest(df, bar, side, bottom, top, n, retest_window=5):
    """Resolve lifecycle state + retest status for a FVG born at bar `bar`."""
    width = max(1e-9, top - bottom)
    touched = False
    deepest = None
    invalidated = False
    inval_bar = n - 1
    for k in range(bar + 1, n):
        h = float(df["high"].iloc[k])
        lo = float(df["low"].iloc[k])
        cl = float(df["close"].iloc[k])
        if side == "BULLISH":
            if lo < top and h > bottom:
                touched = True
                if deepest is None or lo < deepest:
                    deepest = lo
            if cl < bottom:
                invalidated = True
                inval_bar = k
                break
        else:
            if h > bottom and lo < top:
                touched = True
                if deepest is None or h > deepest:
                    deepest = h
            if cl > top:
                invalidated = True
                inval_bar = k
                break
    if touched and deepest is not None:
        if side == "BULLISH":
            mitigation_ratio = (top - deepest) / width
        else:
            mitigation_ratio = (deepest - bottom) / width
        mitigation_ratio = min(1.0, max(0.0, mitigation_ratio))
    else:
        mitigation_ratio = 1.0 if invalidated else 0.0

    inverted = bool(invalidated) or mitigation_ratio >= 0.75
    retest = "NONE"
    if inverted:
        retest = _ifvg_retest(df, bar + 1, n, side, bottom, top, retest_window)

    if retest != "NONE":
        state = _IFVG_STATE_INVERSE
    elif invalidated or mitigation_ratio >= 0.75:
        state = _IFVG_STATE_INVALIDATED
    elif touched:
        state = _IFVG_STATE_MITIGATED
    else:
        state = _IFVG_STATE_NORMAL
    return state, mitigation_ratio, inverted, retest, inval_bar


def detect_fvg_lifecycle(df, lookback=60, threshold=0.001, min_width_atr=None,
                         atr=None, retest_window=5):
    """Full-history FVG lifecycle detection (warning engine, never orders).

    Scans the last `lookback` bars for 3-candle fair value gaps and attaches
    the lifecycle state + retest status to each. Returns a list of zone dicts
    newest-first:
        {side, top, bottom, bar, age_bars, width, state, mitigation_ratio,
         inverted, retest}
    `state` is one of NORMAL / MITIGATED / INVALIDATED / INVERSE.
    """
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 6:
        return []
    n = len(df)
    atr_v = 0.0
    if atr and float(atr) == float(atr) and float(atr) > 0:
        atr_v = float(atr)
    else:
        atr_v = _ifvg_atr(df)
    min_w = 0.0
    if min_width_atr and atr_v > 0:
        min_w = float(min_width_atr) * atr_v
    zones = []
    lo = max(0, n - int(lookback))
    for i in range(n - 1, lo + 2, -1):
        a = df.iloc[i - 2]
        b = df.iloc[i - 1]
        c = df.iloc[i]
        if float(c["low"]) > float(a["high"]) * (1 + threshold):
            side, bottom, top = "BULLISH", float(a["high"]), float(c["low"])
        elif float(c["high"]) < float(a["low"]) * (1 - threshold):
            side, bottom, top = "BEARISH", float(c["high"]), float(a["low"])
        else:
            continue
        width = top - bottom
        if width <= 0 or (min_w > 0 and width < min_w):
            continue
        state, mit, inverted, retest, inval_bar = _ifvg_state_and_retest(
            df, i, side, bottom, top, n, retest_window
        )
        zones.append({
            "side": side,
            "top": top,
            "bottom": bottom,
            "bar": i,
            "age_bars": n - 1 - i,
            "width": width,
            "state": state,
            "mitigation_ratio": round(mit, 3),
            "inverted": bool(inverted),
            "retest": retest,
        })
    zones.sort(key=lambda z: z["bar"], reverse=True)
    return zones


def ifvg_warning_payload(side, df, atr=None, reference_price=None,
                         lookback=60, block_atr=None):
    """IFVG warning payload for a directional candidate/position.

    Returns:
        has_inverse:  any direction-relevant inverse/inverted zone
        blocking:     inverse zone ahead within block distance AND not consumed
        zones:        summarized inverse zones (newest first)
        closest:      nearest inverse zone dict (or None)
        distance_atr: midpoint distance to closest (in ATRs, or None)
        penalty:      proximity-scaled warning strength 0..IFVG_PENALTY_MAX
        reason:       human-readable summary for [IFVG] logs
    Warning only: this NEVER books an order.
    """
    out = {
        "has_inverse": False, "blocking": False, "zones": [],
        "closest": None, "distance_atr": None, "penalty": 0.0,
        "reason": "NO INVERSE FVG",
    }
    if not IFVG_ENABLED:
        out["reason"] = "IFVG DISABLED"
        return out
    if side not in ("BUY", "SELL") or df is None or not isinstance(df, pd.DataFrame):
        return out
    if len(df) < 8:
        return out
    price = float(reference_price)
    if reference_price is None or float(reference_price) != float(reference_price):
        price = float(df["close"].iloc[-1])
    atr_v = float(atr) if atr and float(atr) == float(atr) and float(atr) > 0 else _ifvg_atr(df)
    if atr_v <= 0:
        return out
    block_d = float(block_atr) if block_atr else IFVG_BLOCK_DISTANCE_ATR
    zones = detect_fvg_lifecycle(
        df, lookback=lookback, min_width_atr=IFVG_MIN_WIDTH_ATR, atr=atr_v
    )
    relevant = []
    for z in zones:
        if not z["inverted"]:
            continue
        mid = (z["bottom"] + z["top"]) / 2.0
        if side == "BUY":
            if z["top"] < price - 1e-9:
                continue
            dist = mid - price
            if dist < -1e-9:
                continue
        else:
            if z["bottom"] > price + 1e-9:
                continue
            dist = price - mid
            if dist < -1e-9:
                continue
        relevant.append((z, abs(dist)))
    if not relevant:
        return out
    relevant.sort(key=lambda t: t[1])
    closest, dist = relevant[0]
    dist_a = dist / atr_v
    consumed = bool(closest.get("retest") == "BROKEN")
    has_inverse = True
    blocking = bool((not consumed) and dist_a <= block_d)
    if consumed:
        penalty = 0.0
        reason = (f"INVERSE FVG consumed (retest BROKEN) zone@bar{closest['bar']} "
                  f"{closest['side']} dist={dist_a:.2f}ATR")
    elif not blocking:
        penalty = 0.0
        reason = (f"inverse {closest['side']} FVG ahead dist={dist_a:.2f}ATR "
                  f"zone@bar{closest['bar']} retest={closest['retest']}")
    else:
        prox = 1.0 - min(dist_a, block_d) / block_d
        strength = 1.0 if closest.get("retest") in ("REJECTED", "SWEEP") else 0.6
        penalty = min(IFVG_PENALTY_MAX, IFVG_PENALTY_MAX * prox * strength)
        reason = (f"IFVG_WARNING inverse {closest['state']} {closest['side']} "
                  f"zone@bar{closest['bar']} dist={dist_a:.2f}ATR "
                  f"retest={closest['retest']} penalty={penalty:.2f}")
    out.update({
        "has_inverse": True,
        "blocking": blocking,
        "zones": [z for z, _ in relevant][:5],
        "closest": closest,
        "distance_atr": round(dist_a, 3),
        "penalty": round(penalty, 3),
        "reason": reason,
    })
    return out


def compute_zone_strength(df, level, zone_type, atr, ob):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 30:
        return 0, {}
    price = df['close'].iloc[-1]
    touch_indices = []
    for i in range(max(0, len(df)-30), len(df)):
        candle_high = df['high'].iloc[i]
        candle_low = df['low'].iloc[i]
        if (zone_type == "support" and abs(candle_low - level) < atr) or \
           (zone_type == "resistance" and abs(candle_high - level) < atr):
            touch_indices.append(i)
    vol_strength = 0
    if touch_indices:
        volumes = df['volume'].iloc[touch_indices]
        avg_vol = volumes.mean()
        overall_avg = df['volume'].iloc[-30:].mean() if len(df) >= 30 else df['volume'].mean()
        vol_strength = min(3.0, avg_vol / overall_avg) if overall_avg > 0 else 0
    reaction_count = 0
    for idx in touch_indices:
        if idx < len(df)-1:
            next_close = df['close'].iloc[idx+1]
            if (zone_type == "support" and next_close > df['close'].iloc[idx]) or \
               (zone_type == "resistance" and next_close < df['close'].iloc[idx]):
                reaction_count += 1
    reaction_score = min(3.0, reaction_count)
    liquidity_score = 0
    if ob:
        obi = orderbook_imbalance(ob)
        if zone_type == "support" and obi > 0.1:
            liquidity_score = 2
        elif zone_type == "resistance" and obi < -0.1:
            liquidity_score = 2
        elif abs(obi) > 0.05:
            liquidity_score = 1
    inst_score = 0
    bos_up, bos_down = detect_bos(df, lookback=5)
    struct_shift = detect_structure_shift(df)
    if zone_type == "support" and (bos_up or struct_shift == "bullish_shift"):
        inst_score = 2
    elif zone_type == "resistance" and (bos_down or struct_shift == "bearish_shift"):
        inst_score = 2
    rejection_score = 0
    if len(df) >= 1:
        last = df.iloc[-1]
        body, range_, upper_wick, lower_wick = candle_metrics(last)
        if zone_type == "support" and lower_wick > body * 1.5 and abs(last['low'] - level) < atr:
            rejection_score = 2
        elif zone_type == "resistance" and upper_wick > body * 1.5 and abs(last['high'] - level) < atr:
            rejection_score = 2
    total = vol_strength + reaction_score + liquidity_score + inst_score + rejection_score
    strength = min(10.0, total * 10 / 10)
    return round(strength, 1), {
        "vol_strength": round(vol_strength, 1),
        "reaction_count": reaction_count,
        "liquidity_score": liquidity_score,
        "institutional_score": inst_score,
        "rejection_score": rejection_score
    }

def get_smart_zones(symbol, df, ob=None):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < 30:
        return {"buy_zones": [], "sell_zones": []}
    key = f"smart_zones_{symbol}"
    cached = MEMORY.get(key)
    if cached and time.time() - cached.get("ts", 0) < 90:
        return cached.get("data", {"buy_zones": [], "sell_zones": []})
    atr_series = compute_atr(df)
    if atr_series is None or len(atr_series) == 0:
        return {"buy_zones": [], "sell_zones": []}
    atr = float(atr_series.iloc[-1])
    supports, resistances = get_clustered_zones(df, lookback=min(120, len(df)), cluster_pct=0.002)
    buy_zones = []
    for sup in supports:
        strength, details = compute_zone_strength(df, sup, "support", atr, ob)
        buy_zones.append({"price": sup, "strength": strength, "details": details, "type": "support"})
    sell_zones = []
    for res in resistances:
        strength, details = compute_zone_strength(df, res, "resistance", atr, ob)
        sell_zones.append({"price": res, "strength": strength, "details": details, "type": "resistance"})
    buy_zones.sort(key=lambda x: x["strength"], reverse=True)
    sell_zones.sort(key=lambda x: x["strength"], reverse=True)
    data = {"buy_zones": buy_zones, "sell_zones": sell_zones}
    MEMORY[key] = {"data": data, "ts": time.time()}
    return data

def evaluate_liquidity_narrative(df, ob, atr, side):
    narrative = {"sweep": False, "choch_bos": False, "retest": False, "rejection": False,
                 "displacement": False, "rf_alignment": False, "volume_confirmation": False}
    price = df['close'].iloc[-1]
    pools = build_liquidity_pools(df)
    swept_h, swept_l = detect_sweep(df, pools)
    if side == "BUY" and swept_l:
        narrative["sweep"] = True
    elif side == "SELL" and swept_h:
        narrative["sweep"] = True
    bos_up, bos_down = detect_bos(df)
    struct_shift = detect_structure_shift(df)
    choch = struct_shift is not None
    if side == "BUY" and (bos_up or (choch and struct_shift == "bullish_shift")):
        narrative["choch_bos"] = True
    elif side == "SELL" and (bos_down or (choch and struct_shift == "bearish_shift")):
        narrative["choch_bos"] = True
    zones = get_smart_zones(df.symbol if hasattr(df, 'symbol') else "unknown", df, ob)
    required_zone = None
    if zones:
        if side == "BUY" and zones["buy_zones"]:
            required_zone = zones["buy_zones"][0]
        elif side == "SELL" and zones["sell_zones"]:
            required_zone = zones["sell_zones"][0]
    if required_zone:
        dist = abs(price - required_zone["price"]) / price
        if dist < 0.003:
            narrative["retest"] = True
    if candle_rejection(df, side):
        narrative["rejection"] = True
    vol_state = classify_volume(df)
    if detect_displacement(df, side, atr, vol_state):
        narrative["displacement"] = True
    if vol_state in ("expansion", "spike"):
        narrative["volume_confirmation"] = True
    rf = RFEngine(20, 3.5).compute(df)
    if rf["signal"] == side and abs(rf["distance"]) < 0.003:
        narrative["rf_alignment"] = True
    score = 0
    if narrative["sweep"]: score += 2
    if narrative["choch_bos"]: score += 2
    if narrative["retest"]: score += 2
    if narrative["rejection"]: score += 1.5
    if narrative["displacement"]: score += 1.5
    if narrative["volume_confirmation"]: score += 1
    if narrative["rf_alignment"]: score += 2
    return narrative, score

def record_watchlist_entry(symbol, side, narrative, score, smart_money=None, momentum=None):
    now = time.time()
    state = "DETECTED"
    if narrative.get("retest"):
        state = "RETEST"
    if narrative.get("rejection"):
        state = "REJECTION"
    if narrative.get("displacement"):
        state = "DISPLACEMENT"
    if narrative.get("sweep") and narrative.get("choch_bos") and narrative.get("retest") and narrative.get("rejection"):
        state = "CONFIRMED"
    reasons_list = []
    if narrative["sweep"]: reasons_list.append("Sweep")
    if narrative["choch_bos"]: reasons_list.append("CHoCH/BOS")
    if narrative["retest"]: reasons_list.append("ZONE_RETEST")
    if narrative["rejection"]: reasons_list.append("OB")
    if narrative["displacement"]: reasons_list.append("Displacement")
    if narrative["volume_confirmation"]: reasons_list.append("Volume")
    if narrative["rf_alignment"]: reasons_list.append("RF")
    trade_type = "REVERSAL" if (narrative["sweep"] or narrative["retest"]) else "TREND"
    strength = "WEAK"
    if score >= 7:
        strength = "STRONG"
    elif score >= 4:
        strength = "MEDIUM"
    entry = {
        "symbol": symbol,
        "side": side,
        "score": round(score, 2),
        "state": state,
        "reasons": reasons_list,
        "trade_type": trade_type,
        "strength": strength,
        "last_update": now,
        "watchlist_entry_time": now,
        "institutional_analysis_time": 0.0,
        "institutional_analysis_logged": False,
        "confirmation_logged": False,
        "expansion_logged": False,
    }
    if smart_money:
        entry["smart_money_bias"] = smart_money.get("institutional_bias", "NEUTRAL")
        entry["smart_money_bias_detailed"] = smart_money.get("institutional_bias_detailed", "NEUTRAL")
        entry["distribution_risk"] = round(smart_money.get("distribution_risk", 0), 1)
        entry["accumulation"] = round(smart_money.get("accumulation_strength", 0), 1)
    if momentum:
        entry["momentum_expansion"] = momentum.get("trend_expansion", False)
        entry["momentum_decay"] = momentum.get("momentum_decay", False)
        entry["exhaustion_risk"] = round(momentum.get("exhaustion_risk", 0), 1)
        entry["continuation_strength"] = round(momentum.get("continuation_strength", 0), 1)
    if "watchlist" not in MEMORY:
        MEMORY["watchlist"] = {}
    MEMORY["watchlist"][symbol] = entry
    log_execution(f"[WATCHLIST] {symbol} entered (side={side})", "INFO")

# ==================== VALIDATION HELPERS ====================
def is_valid_dataframe(df, required_cols=None):
    if df is None:
        return False
    if not isinstance(df, pd.DataFrame):
        return False
    if required_cols is None:
        required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    return all(col in df.columns for col in required_cols)

# ==================== WATCHLIST CLEANUP (STAGED EXPIRATION) ====================
def cleanup_watchlist(ttl=1800, max_size=60):
    now = time.time()
    if "watchlist" not in MEMORY:
        return
    quarantine = MEMORY.setdefault("watchlist_quarantine", [])
    to_remove = []
    for sym, v in list(MEMORY["watchlist"].items()):
        if not isinstance(v, dict) or not v.get("symbol"):
            to_remove.append(sym)
            quarantine.append({"key": sym, "reason": "MALFORMED_RECORD", "ts": now})
            continue

        last_update = v.get("last_update", v.get("updated_at", 0))
        last_analyzed = v.get("analysis", {}).get("last_analyzed", 0)
        last_activity = max(last_update, last_analyzed) if (last_update or last_analyzed) else 0
        try:
            last_activity = float(last_activity or 0)
        except (TypeError, ValueError):
            last_activity = 0.0

        if v.get("state") == "EXPIRED":
            expired_at = v.get("expired_at", 0)
            if expired_at and now - expired_at > ttl:
                to_remove.append(sym)
            continue

        if last_activity <= 0 or now - last_activity > ttl:
            v["state"] = "EXPIRED"
            v["expired_at"] = now
            v["status"] = "DATA_STALE"
            continue

    for sym in to_remove:
        MEMORY["watchlist"].pop(sym, None)

    MEMORY["watchlist_quarantine"] = quarantine[-50:]

    if len(MEMORY["watchlist"]) > max_size:
        sorted_items = sorted(
            MEMORY["watchlist"].items(),
            key=lambda x: x[1].get("priority", x[1].get("score", 0)),
            reverse=True
        )
        MEMORY["watchlist"] = dict(sorted_items[:max_size])

# ========== VWAP ENGINE ==========
def compute_vwap(df):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series([0.0])
    tp = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = df['volume'].cumsum()
    cum_tp_vol = (tp * df['volume']).cumsum()
    vwap = cum_tp_vol / cum_vol
    return vwap

def vwap_features(df):
    vwap = compute_vwap(df)
    price = df['close'].iloc[-1]
    distance = (price - vwap.iloc[-1]) / vwap.iloc[-1] if vwap.iloc[-1] != 0 else 0.0
    slope = vwap.iloc[-1] - vwap.iloc[-5] if len(vwap) >= 5 else 0.0
    return {"vwap": vwap.iloc[-1], "distance": distance, "slope": slope}

def detect_exhaustion_zone(df):
    atr = compute_atr(df).iloc[-1]
    rsi = compute_rsi(df).iloc[-1]
    vw = vwap_features(df)
    last = df.iloc[-1]
    impulse = (last['high'] - last['low']) >= 1.4 * atr if atr > 0 else False
    stretched = abs(vw["distance"]) >= 0.012
    rsi_extreme = rsi >= 70 or rsi <= 30
    if not (impulse and stretched and rsi_extreme):
        return False, None, None
    if rsi >= 70 and vw["distance"] > 0.012:
        return True, last['high'], "TOP"
    elif rsi <= 30 and vw["distance"] < -0.012:
        return True, last['low'], "BOTTOM"
    return False, None, None

def detect_reset(df, zone_price, zone_type):
    last_close = df['close'].iloc[-1]
    if zone_type == "TOP":
        drop = (zone_price - last_close) / zone_price
        return drop >= 0.003
    else:
        rise = (last_close - zone_price) / zone_price
        return rise >= 0.003

def confirm_reversal(df, ob, zone_type):
    last = df.iloc[-1]
    wick = (last['high'] - max(last['open'], last['close'])) if zone_type == "TOP" else (min(last['open'], last['close']) - last['low'])
    body = abs(last['close'] - last['open'])
    wick_reject = wick > body * 1.5 if body > 0 else False
    macd_hist = compute_macd(df)[2]
    macd_flip = macd_first_flip(macd_hist)
    flow = flow_engine(df)
    flow_agree = (zone_type == "TOP" and flow == "aggressive_sell") or (zone_type == "BOTTOM" and flow == "aggressive_buy")
    obi = orderbook_imbalance(ob)
    obi_agree = (zone_type == "TOP" and obi < -0.2) or (zone_type == "BOTTOM" and obi > 0.2)
    score = sum([wick_reject, macd_flip, flow_agree, obi_agree])
    return score >= 2

def detect_stop_hunt(df):
    pools = build_liquidity_pools(df)
    swept_high, swept_low = detect_sweep(df, pools)
    last = df.iloc[-1]
    inside = last['close'] < last['high'] and last['close'] > last['low']
    reclaim = (swept_high and last['close'] < last['high']) or (swept_low and last['close'] > last['low'])
    volume_ok = volume_pressure_real(df)
    if swept_high and reclaim and volume_ok:
        return True, "SELL"
    elif swept_low and reclaim and volume_ok:
        return True, "BUY"
    return False, None

def choose_mode(df):
    adx = compute_adx(df).iloc[-1] if len(df) >= 20 else 20
    return "TREND" if adx >= 20 else "RANGE"

def smart_decision(df, ob, symbol):
    mode = choose_mode(df)
    is_hunt, hunt_side = detect_stop_hunt(df)
    if is_hunt and hunt_side:
        return "STOP_HUNT", hunt_side, {"mode": mode}
    is_zone, zone_price, zone_type = detect_exhaustion_zone(df)
    if is_zone and zone_price is not None:
        STATE["zone"][symbol] = (zone_price, zone_type)
    if symbol in STATE["zone"]:
        zone_price, zone_type = STATE["zone"][symbol]
        if detect_reset(df, zone_price, zone_type) and confirm_reversal(df, ob, zone_type):
            side = "SELL" if zone_type == "TOP" else "BUY"
            return "EXHAUSTION_ENTRY", side, {"mode": mode, "zone": zone_price}
    return None, None, None

# ========== OPPOSING ZONE SMART EXIT ENGINE ==========
def find_nearest_opposing_zone(df, side):
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    price = df['close'].iloc[-1]
    if side == "BUY":
        valid = [r for r in resistances if r > price]
        if valid:
            nearest = min(valid, key=lambda x: x - price)
            return nearest, "RESISTANCE"
    else:
        valid = [s for s in supports if s < price]
        if valid:
            nearest = max(valid, key=lambda x: x)
            return nearest, "SUPPORT"
    return None, None

def compute_opposing_zone_strength(df, ob, atr, side, zone_price, zone_type):
    score = 0
    price = df['close'].iloc[-1]
    dist_pct = abs(price - zone_price) / price
    if dist_pct <= 0.002:
        score += 2
    elif dist_pct <= 0.005:
        score += 1
    last = df.iloc[-1]
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']
    if range_ > 0:
        if side == "BUY":
            upper_wick = last['high'] - max(last['open'], last['close'])
            if upper_wick > body * 1.5 and price >= zone_price - 0.002*price:
                score += 2
        else:
            lower_wick = min(last['open'], last['close']) - last['low']
            if lower_wick > body * 1.5 and price <= zone_price + 0.002*price:
                score += 2
    vol_state = classify_volume(df)
    if vol_state == "exhaustion":
        score += 1
    elif vol_state == "neutral" and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 0.8:
        score += 1
    if side == "BUY":
        if last['high'] > zone_price and last['close'] < zone_price:
            score += 2
    else:
        if last['low'] < zone_price and last['close'] > zone_price:
            score += 2
    obi = orderbook_imbalance(ob)
    if side == "BUY" and obi < -0.15:
        score += 2
    elif side == "SELL" and obi > 0.15:
        score += 2
    adx_series = compute_adx(df)
    if len(adx_series) >= 2:
        if adx_series.iloc[-1] < adx_series.iloc[-2]:
            score += 1
    return score

def opposing_zone_smart_exit(df, ob, atr, side, entry_price, current_price, state):
    zone_price, zone_type = find_nearest_opposing_zone(df, side)
    if zone_price is None:
        return "HOLD", None
    strength = compute_opposing_zone_strength(df, ob, atr, side, zone_price, zone_type)
    if strength >= 6:
        if not state.get("smart_exit_triggered"):
            return "EXIT", None
    elif strength >= 4:
        if not state.get("smart_partial_done") and not state.get("smart_exit_triggered"):
            return "PARTIAL", None
    elif strength >= 2:
        if not state.get("smart_tightened"):
            return "TIGHTEN", 0.8
    return "HOLD", None

# ========== NARRATIVE + CONTEXT ENGINE v1 ==========
def get_di_components(df, period=14):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < period*2:
        return None, None, None, 0.0
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = rma(tr, period)
    atr = atr.clip(lower=1e-9)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)
    plus_di = 100 * rma(plus_dm, period) / (atr + 1e-9)
    minus_di = 100 * rma(minus_dm, period) / (atr + 1e-9)
    adx_series = compute_adx(df, period)
    adx_current = adx_series.iloc[-1] if len(adx_series) > 0 else 20.0
    adx_prev = adx_series.iloc[-2] if len(adx_series) > 1 else adx_current
    adx_slope = adx_current - adx_prev
    return plus_di.iloc[-1], minus_di.iloc[-1], adx_current, adx_slope

def get_vwap_narrative(df):
    vwap = compute_vwap(df)
    price = df['close'].iloc[-1]
    vwap_last = vwap.iloc[-1]
    vwap_prev = vwap.iloc[-2] if len(vwap) > 1 else vwap_last
    distance = (price - vwap_last) / vwap_last if vwap_last != 0 else 0.0
    above = price > vwap_last
    below = price < vwap_last
    prev_above = df['close'].iloc[-2] > vwap_prev if len(df) > 1 else above
    reclaim = (not prev_above) and above
    reject = prev_above and (not above)
    return {
        "vwap": vwap_last,
        "distance": distance,
        "above": above,
        "below": below,
        "reclaim": reclaim,
        "reject": reject,
        "slope": vwap_last - vwap_prev
    }

def compute_enhanced_zone_strength(df, level, zone_type, atr, ob, sweep_detected=False):
    price = df['close'].iloc[-1]
    touches = 0
    rejection_count = 0
    volume_at_touches = []
    for i in range(max(0, len(df)-60), len(df)):
        candle_high = df['high'].iloc[i]
        candle_low = df['low'].iloc[i]
        if zone_type == "support":
            if abs(candle_low - level) < atr * 0.5:
                touches += 1
                if i < len(df)-1:
                    next_close = df['close'].iloc[i+1]
                    if next_close > df['close'].iloc[i]:
                        rejection_count += 1
                        volume_at_touches.append(df['volume'].iloc[i])
        else:
            if abs(candle_high - level) < atr * 0.5:
                touches += 1
                if i < len(df)-1:
                    next_close = df['close'].iloc[i+1]
                    if next_close < df['close'].iloc[i]:
                        rejection_count += 1
                        volume_at_touches.append(df['volume'].iloc[i])
    vol_score = 0.0
    if volume_at_touches:
        avg_vol_touch = sum(volume_at_touches) / len(volume_at_touches)
        avg_vol_overall = df['volume'].iloc[-60:].mean()
        if avg_vol_overall > 0:
            vol_score = min(3.0, avg_vol_touch / avg_vol_overall)
    strength = touches * 1.5 + rejection_count * 2.0 + vol_score
    if sweep_detected:
        strength += 2.0
    last = df.iloc[-1]
    body, range_, upper_wick, lower_wick = candle_metrics(last)
    if zone_type == "support" and lower_wick > body * 1.5 and abs(last['low'] - level) < atr:
        strength += 2.0
    elif zone_type == "resistance" and upper_wick > body * 1.5 and abs(last['high'] - level) < atr:
        strength += 2.0
    return min(10.0, strength)

def classify_market_narrative(df, ob, atr, side, rf_signal):
    reasons = []
    score = 0.0
    plus_di, minus_di, adx, adx_slope = get_di_components(df)
    if plus_di is not None:
        if side == "BUY" and plus_di > minus_di:
            score += 2.0
            reasons.append("DI+ dominance")
        elif side == "SELL" and minus_di > plus_di:
            score += 2.0
            reasons.append("DI- dominance")
        elif abs(plus_di - minus_di) < 5:
            reasons.append("DI tangled")
    if adx_slope > 1.5:
        score += 1.5
        reasons.append(f"ADX rising ({adx_slope:.1f})")
    elif adx_slope < -1.5:
        score -= 1.0
        reasons.append("ADX falling")
    vwap_n = get_vwap_narrative(df)
    if side == "BUY":
        if vwap_n["above"]:
            score += 1.5
            reasons.append("VWAP above")
        elif vwap_n["reclaim"]:
            score += 2.0
            reasons.append("VWAP reclaim")
    else:
        if vwap_n["below"]:
            score += 1.5
            reasons.append("VWAP below")
        elif vwap_n["reject"]:
            score += 2.0
            reasons.append("VWAP reject")
    pools = build_liquidity_pools(df)
    swept_h, swept_l = detect_sweep(df, pools)
    sweep_detected = (side == "BUY" and swept_l) or (side == "SELL" and swept_h)
    if sweep_detected:
        score += 2.5
        reasons.append("Liquidity sweep")
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    zone_strength = 0.0
    if side == "BUY" and supports:
        nearest_sup = max([s for s in supports if s <= df['close'].iloc[-1]], default=None)
        if nearest_sup:
            zone_strength = compute_enhanced_zone_strength(df, nearest_sup, "support", atr, ob, sweep_detected)
            score += zone_strength * 0.5
            reasons.append(f"Zone strength {zone_strength:.1f}")
    elif side == "SELL" and resistances:
        nearest_res = min([r for r in resistances if r >= df['close'].iloc[-1]], default=None)
        if nearest_res:
            zone_strength = compute_enhanced_zone_strength(df, nearest_res, "resistance", atr, ob, sweep_detected)
            score += zone_strength * 0.5
            reasons.append(f"Zone strength {zone_strength:.1f}")
    bos_up, bos_down = detect_bos(df)
    struct_shift = detect_structure_shift(df)
    if (side == "BUY" and (bos_up or struct_shift == "bullish_shift")):
        score += 2.0
        reasons.append("Bullish structure")
    elif (side == "SELL" and (bos_down or struct_shift == "bearish_shift")):
        score += 2.0
        reasons.append("Bearish structure")
    vol_state = classify_volume(df)
    if vol_state in ("expansion", "spike"):
        score += 1.5
        reasons.append("Volume expansion")
    elif vol_state == "exhaustion":
        score -= 1.0
        reasons.append("Volume exhaustion")
    if candle_rejection(df, side):
        score += 1.5
        reasons.append("Rejection candle")
    if detect_displacement(df, side, atr, vol_state, body_atr_threshold=0.8, volume_expansion_required=False):
        score += 1.5
        reasons.append("Displacement")
    if rf_signal == side:
        score += 1.5
        reasons.append("RF aligned")
    if adx is not None and adx < 18 and plus_di is not None and abs(plus_di - minus_di) < 6:
        score = 0
        reasons = ["CHOP market (ADX<18 + DI tangled)"]
    if score >= 9.0:
        classification = "REVERSAL_SNIPER" if (sweep_detected or zone_strength > 5) else "TREND_CONTINUATION"
        confidence = "HIGH"
    elif score >= 7.0:
        classification = "TREND_CONTINUATION" if (bos_up or bos_down or struct_shift) else "ACCUMULATION_LONG" if side == "BUY" else "DISTRIBUTION_SHORT"
        confidence = "MEDIUM"
    elif score >= 5.0:
        classification = "FAKE_BREAKOUT" if not sweep_detected else "LOW_CONFIDENCE"
        confidence = "LOW"
    else:
        classification = "CHOP_NO_TRADE"
        confidence = "NO_TRADE"
    return {
        "classification": classification,
        "confidence": confidence,
        "narrative_score": round(score, 2),
        "reasons": reasons,
        "sweep": sweep_detected,
        "zone_strength": zone_strength,
        "di_dominance": ("BUY" if plus_di > minus_di else "SELL") if plus_di is not None else "NEUTRAL",
        "adx_slope": adx_slope,
        "vwap_reclaim": vwap_n["reclaim"],
        "vwap_reject": vwap_n["reject"]
    }

def detect_market_regime(df):
    if len(df) < 50:
        return "RANGE"
    try:
        adx = compute_adx(df).iloc[-1]
        plus_di, minus_di, _, _ = get_di_components(df)
        vwap_n = get_vwap_narrative(df)
        atr = compute_atr(df).iloc[-1]
        atr_avg = compute_atr(df).rolling(20).mean().iloc[-1] if len(compute_atr(df))>=20 else atr
        atr_ratio = atr / atr_avg if atr_avg else 1.0
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1] if len(df)>=50 else ema20
        price = df['close'].iloc[-1]
        di_delta = abs(plus_di - minus_di)
        if adx < 18 and di_delta < 6:
            return "CHOP"
        if adx > 20 and di_delta > 5:
            struct = detect_structure_shift(df)
            bullish_aligned = plus_di > minus_di and ema20 > ema50 and price > ema20
            bearish_aligned = minus_di > plus_di and ema20 < ema50 and price < ema20
            if bullish_aligned or bearish_aligned:
                return "TREND"
            if struct == "bullish_shift" and plus_di > minus_di:
                return "TREND"
            if struct == "bearish_shift" and minus_di > plus_di:
                return "TREND"
        if adx > 20 and atr_ratio > 1.4:
            return "EXPANSION"
        if atr_ratio < 0.7 and adx < 25:
            return "COMPRESSION"
        return "RANGE"
    except:
        return "RANGE"

def get_trend_direction(df):
    try:
        plus_di, minus_di, _, _ = get_di_components(df)
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1] if len(df)>=50 else ema20
        price = df['close'].iloc[-1]
        struct = detect_structure_shift(df)
        if (plus_di > minus_di and ema20 > ema50 and price > ema20) or struct == "bullish_shift":
            return "BULLISH"
        elif (minus_di > plus_di and ema20 < ema50 and price < ema20) or struct == "bearish_shift":
            return "BEARISH"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def adjust_narrative_confidence(narrative, regime, side, trend_direction):
    orig_conf = narrative["confidence"]
    score = narrative["narrative_score"]
    side_aligned = False
    if (trend_direction == "BULLISH" and side == "BUY") or (trend_direction == "BEARISH" and side == "SELL"):
        side_aligned = True
    final_conf = orig_conf
    final_class = narrative["classification"]
    if regime == "CHOP":
        return "NO_TRADE", "CHOP_NO_TRADE"
    if orig_conf == "NO_TRADE" or score < 5.0:
        return "NO_TRADE", "CHOP_NO_TRADE"
    if regime == "TREND":
        if side_aligned:
            if orig_conf == "HIGH":
                final_conf = "HIGH"
                final_class = "SNIPER"
            elif orig_conf == "MEDIUM":
                final_conf = "MEDIUM"
                final_class = "TREND"
            elif orig_conf == "LOW":
                if score >= 5.0:
                    final_conf = "MEDIUM"
                    final_class = "TREND"
                else:
                    final_conf = "NO_TRADE"
                    final_class = "NO_TRADE"
        else:
            if orig_conf == "HIGH":
                final_conf = "HIGH"
                final_class = "SNIPER"
            else:
                final_conf = "NO_TRADE"
                final_class = "NO_TRADE"
    elif regime in ("EXPANSION", "COMPRESSION"):
        if orig_conf == "HIGH":
            final_conf = "HIGH"
            final_class = "SNIPER"
        else:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
    else:
        if orig_conf == "HIGH":
            final_conf = "HIGH"
            final_class = "SNIPER"
        elif orig_conf == "MEDIUM" and side_aligned:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
        else:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
    return final_conf, final_class

def evaluate_with_narrative(symbol, side, price, atr_val, df, ob, rf_signal, existing_score=0):
    regime = detect_market_regime(df)
    trend_dir = get_trend_direction(df)
    narrative = classify_market_narrative(df, ob, atr_val, side, rf_signal)
    final_conf, final_class = adjust_narrative_confidence(narrative, regime, side, trend_dir)
    narrative["confidence"] = final_conf
    narrative["classification"] = final_class
    narrative["regime"] = regime
    MEMORY[f"last_narrative_{symbol}"] = {**narrative, "timestamp": time.time(), "side": side}
    should_enter = final_conf in ("HIGH", "MEDIUM")
    if not should_enter:
        reason = f"{final_class} ({final_conf}) Regime={regime} Score={narrative['narrative_score']:.1f}"
        MEMORY.setdefault("no_entry_feed", []).append({
            "time": time.time(),
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "score": narrative["narrative_score"]
        })
        if len(MEMORY["no_entry_feed"]) > 20:
            MEMORY["no_entry_feed"] = MEMORY["no_entry_feed"][-20:]
        return False, None, narrative
    STATE["narrative_classification"] = final_class
    STATE["narrative_confidence"] = narrative["narrative_score"]
    STATE["confidence_level"] = final_conf
    return True, final_class, narrative

def narrative_debug():
    debug_data = []
    for key, val in MEMORY.items():
        if key.startswith("last_narrative_"):
            debug_data.append({
                "symbol": key.replace("last_narrative_", ""),
                "side": val.get("side"),
                "classification": val.get("classification"),
                "confidence": val.get("confidence"),
                "score": val.get("narrative_score"),
                "reasons": val.get("reasons"),
                "timestamp": val.get("timestamp")
            })
    radar_candidates = MEMORY.get("radar_top5", [])
    for cand in radar_candidates:
        sym = cand["symbol"]
        if not any(d["symbol"] == sym for d in debug_data):
            df = get_ohlcv_safe(sym, 100)
            if df is not None:
                ob = get_orderbook_cached(sym, 10)
                atr = compute_atr(df).iloc[-1] if len(df) > 14 else df['close'].iloc[-1] * 0.01
                side = "BUY"
                narrative = classify_market_narrative(df, ob, atr, side, None)
                debug_data.append({
                    "symbol": sym,
                    "side": "analysis",
                    "classification": narrative["classification"],
                    "confidence": narrative["confidence"],
                    "score": narrative["narrative_score"],
                    "reasons": narrative["reasons"][:5],
                    "timestamp": time.time()
                })
    return jsonify({"narrative_debug": debug_data})

# ========== MONITOR WATCHLIST ==========
def monitor_watchlist():
    watchlist = MEMORY.get("rf_watchlist", [])
    for c in watchlist:
        sym = c["symbol"]
        df = get_ohlcv_safe(sym, 150)
        if df is None or not validate_dataframe(df, 100):
            continue
        ob = get_orderbook_cached(sym, limit=10)
        if ob is not None:
            price = df['close'].iloc[-1]
            atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
            for side_try in ("BUY", "SELL"):
                should_enter, classification, reason_str = check_institutional_entry(sym, side_try, df, ob, atr_val, price)
                if should_enter:
                    should_enter_narr, final_class, narrative = evaluate_with_narrative(sym, side_try, price, atr_val, df, ob, side_try)
                    if not should_enter_narr:
                        continue
                    sl, tp1, tp2 = compute_sl_tp(price, side_try, "REVERSAL", atr_val, df)
                    ok = execute_entry(side_try, sym, price, sl, tp1, tp2, 85, reason_str, atr_val,
                                       trade_type="INSTITUTIONAL_V3", entry_type="SMART_EARLY", classification=classification)
                    if ok:
                        return True
            decision, dec_side, dec_info = smart_decision(df, ob, sym)
            if decision == "STOP_HUNT":
                price = df['close'].iloc[-1]
                atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
                should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
                if not should_enter:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
                reason_str = f"SMART_STOP_HUNT mode={dec_info.get('mode')} | NARR={narrative['classification']}"
                ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                                   trade_type="SMART", entry_type="STOP_HUNT", classification=classification)
                if ok:
                    return True
            elif decision == "EXHAUSTION_ENTRY":
                price = df['close'].iloc[-1]
                atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
                should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
                if not should_enter:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
                reason_str = f"SMART_EXHAUSTION zone={dec_info.get('zone')} mode={dec_info.get('mode')} | NARR={narrative['classification']}"
                ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                                   trade_type="SMART", entry_type="EXHAUSTION", classification=classification)
                if ok:
                    return True
        rf_engine = RFEngine(20, 3.5).compute(df)
        if not rf_engine["triggered"]:
            continue
        side = rf_engine["signal"]
        if side is None:
            continue
        price = df['close'].iloc[-1]
        atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
        adx_series = compute_adx(df)
        adx_val = adx_series.iloc[-1] if adx_series is not None else 20.0
        volume_state = classify_volume(df)
        should_enter, classification, narrative = evaluate_with_narrative(sym, side, price, atr_val, df, ob, side)
        if not should_enter:
            continue
        if is_late_entry(df, side):
            continue
        ob_v1 = get_orderbook_cached(sym, limit=10)
        if ob_v1 is not None:
            total_v1, scn_v1, dir_v1, reasons_v1 = decision_score_v1(df, ob_v1, atr_val, side)
            total_v1 = apply_overrides_v1(df, atr_val, total_v1)
            if dir_v1 and total_v1 >= 5:
                sl_v1, tp1_v1, tp2_v1 = compute_sl_tp(price, dir_v1,
                                                       "REVERSAL" if scn_v1 in ("TRAP","REVERSAL") else "EARLY_TREND",
                                                       atr_val, df)
                ok = decide_and_execute_v1(sym, dir_v1, total_v1, reasons_v1, price, sl_v1, tp1_v1, tp2_v1)
                if ok:
                    return True
        ob = get_orderbook_cached(sym, limit=10)
        if ob is not None:
            total_score, scenario_name, scenario_dir, all_reasons = decision_score(df, ob, atr_val, side)
            if total_score >= 7:
                sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "REVERSAL" if scenario_name=="REVERSAL" else "EARLY_TREND", atr_val, df)
                reason_str = f"UNIFIED_SNIPER ({scenario_name}) score={total_score} | NARR={narrative['classification']} | {'+'.join(all_reasons[:3])}"
                ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                                   trade_type="SCENARIO_ENGINE", entry_type="UNIFIED_SNIPER", classification=classification)
                if ok:
                    return True
            elif total_score >= 5:
                sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "EARLY_TREND", atr_val, df)
                reason_str = f"UNIFIED_EARLY ({scenario_name}) score={total_score} | NARR={narrative['classification']} | {'+'.join(all_reasons[:3])}"
                ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                                   trade_type="SCENARIO_ENGINE", entry_type="UNIFIED_EARLY", classification=classification)
                if ok:
                    return True
        ob = get_orderbook_cached(sym, limit=10)
        if ob is None:
            continue
        else:
            early_score_val, early_reasons = early_score(df, ob, atr_val, side)
            if early_score_val >= 6:
                sl, tp1, tp2 = compute_sl_tp(price, side, "EARLY_TREND", atr_val, df)
                reason_str = f"EARLY_SNIPER ({','.join(early_reasons)}) score={early_score_val} | NARR={narrative['classification']}"
                ok = execute_entry(side, sym, price, sl, tp1, tp2, early_score_val, reason_str, atr_val,
                                   trade_type="EARLY_ENGINE", entry_type="EARLY_SNIPER", classification=classification)
                if ok:
                    return True
            elif early_score_val >= 4:
                sl, tp1, tp2 = compute_sl_tp(price, side, "EARLY_TREND", atr_val, df)
                reason_str = f"EARLY_ENTRY ({','.join(early_reasons)}) score={early_score_val} | NARR={narrative['classification']}"
                ok = execute_entry(side, sym, price, sl, tp1, tp2, early_score_val, reason_str, atr_val,
                                   trade_type="EARLY_ENGINE", entry_type="EARLY_ENTRY", classification=classification)
                if ok:
                    return True
        supports, resistances = get_clustered_zones(df, lookback=120, cluster_pct=0.002)
        location = detect_location(df, price, supports, resistances, threshold=0.003)
        if side == "BUY" and location != "LOW":
            continue
        if side == "SELL" and location != "HIGH":
            continue
        scenario = advanced_detect_scenario(df, side, atr_val, volume_state)
        if scenario == "NONE":
            continue
        decision, adv_class = advanced_decision_engine(scenario, adx_val, volume_state, location)
        if decision != "ENTER":
            continue
        if scenario == "TRAP_REVERSAL":
            leg_class = "REVERSAL"
        elif scenario == "TREND_CONTINUATION":
            leg_class = "EARLY_TREND"
        else:
            leg_class = "TREND_CONTINUATION"
        sl, tp1, tp2 = compute_sl_tp(price, side, leg_class, atr_val, df)
        reason_str = f"ADV SMC {adv_class} | {scenario} | RF {side} | Loc {location} | NARR={narrative['classification']}"
        trade_type = "SMC_ADV"
        TRADE_STATE["zone"] = "support" if side=="BUY" else "resistance"
        TRADE_STATE["location"] = location
        TRADE_STATE["reason"] = [scenario, adv_class, location, narrative['classification']]
        ok = execute_entry(side, sym, price, sl, tp1, tp2, 0, reason_str, atr_val, trade_type, adv_class, classification)
        if ok:
            return True
    return False

# ========== TRADE MANAGEMENT ==========
UPDATE_INTERVAL_SEC = 5

def get_last_price(symbol):
    return get_ticker_safe(symbol)

def update_trailing_simple(current_price):
    if not TRADE_STATE["trail_on"]:
        return False
    entry = TRADE_STATE["entry"]
    side = TRADE_STATE["side"]
    if "trail_stop" not in TRADE_STATE:
        if side == "BUY":
            TRADE_STATE["trail_stop"] = entry * 1.005
        else:
            TRADE_STATE["trail_stop"] = entry * 0.995
    if side == "BUY":
        new_stop = current_price * 0.995
        if new_stop > TRADE_STATE["trail_stop"]:
            TRADE_STATE["trail_stop"] = new_stop
        if current_price <= TRADE_STATE["trail_stop"]:
            return True
    else:
        new_stop = current_price * 1.005
        if new_stop < TRADE_STATE["trail_stop"]:
            TRADE_STATE["trail_stop"] = new_stop
        if current_price >= TRADE_STATE["trail_stop"]:
            return True
    return False

def stop_hit(current_price):
    if not STATE["open"]:
        return False
    side = STATE["side"]
    sl = STATE.get("synthetic_sl", 0.0)
    if side == "BUY" and current_price <= sl:
        return True
    if side == "SELL" and current_price >= sl:
        return True
    return False

def update_stats(pnl_pct):
    DASHBOARD_STATE["stats"]["trades"] += 1
    if pnl_pct >= 0:
        DASHBOARD_STATE["stats"]["wins"] += 1
    else:
        DASHBOARD_STATE["stats"]["losses"] += 1
    total = DASHBOARD_STATE["stats"]["trades"]
    DASHBOARD_STATE["stats"]["win_rate"] = (DASHBOARD_STATE["stats"]["wins"] / total * 100) if total else 0

def get_dashboard_metrics():
    winrate = (PERF["wins"] / PERF["trades"] * 100) if PERF["trades"] else 0
    total_pnl = PERF["total_pnl_pct"] * 100
    last = PERF["last_trade"]
    last_txt = "N/A"
    if last:
        sign = "+" if last["pnl_pct"] >= 0 else ""
        last_txt = f'{last["result"]} ({sign}{last["pnl_pct"]:.2f}%)'
    return {
        "winrate": f"{winrate:.1f}%",
        "total_pnl": f"{total_pnl:+.2f}%",
        "total_pnl_usdt": PERF["total_pnl_usdt"],
        "last_trade": last_txt,
        "trades": PERF["trades"],
        "wins": PERF["wins"],
        "losses": PERF["losses"]
    }

def manage_take_profit(price, atr):
    return "HOLD"

def scaling_logic(symbol, df, ind):
    if not STATE["open"] or STATE.get("scale_ins", 0) >= MAX_SCALE_INS:
        return False
    pnl_pct = (df['close'].iloc[-1] - STATE["entry"])/STATE["entry"]*100 if STATE["side"]=="BUY" else (STATE["entry"]-df['close'].iloc[-1])/STATE["entry"]*100
    if pnl_pct < SCALE_IN_PROFIT_PCT:
        return False
    if abs(df['close'].iloc[-1] - STATE["entry"])/STATE["entry"] < 0.005:
        additional_qty = STATE["qty"] * SCALE_IN_SIZE_PCT
        sym_norm = normalize_symbol(symbol)
        try:
            market = ex.market(sym_norm)
            precision = market['precision']['amount']
            qty = math.floor(additional_qty / precision) * precision
        except:
            qty = additional_qty
        if qty > 0:
            order = open_position(STATE["side"], qty, symbol)
            if order:
                STATE["qty"] += qty
                STATE["remaining_qty"] += qty
                STATE["scale_ins"] = STATE.get("scale_ins", 0) + 1
                log_execution(f"Scaled in {qty:.6f} at {df['close'].iloc[-1]:.4f}", "SUCCESS")
                return True
    return False

def council_exit(df, price):
    adx_series = compute_adx(df)
    adx = adx_series.iloc[-1] if adx_series is not None else 0
    if adx < 18:
        log_execution(f"Exit: ADX dropped to {adx:.1f}", "WARN")
        close_position_full()
        return True
    if STATE["side"] == "BUY" and price < STATE.get("synthetic_sl", 0):
        log_execution(f"Stop loss hit at {price:.4f}", "WARN")
        tg_sl_hit(STATE["current_symbol"], (price - STATE["entry"])/STATE["entry"]*100 if STATE["side"]=="BUY" else (STATE["entry"]-price)/STATE["entry"]*100)
        close_position_full()
        return True
    elif STATE["side"] == "SELL" and price > STATE.get("synthetic_sl", 0):
        log_execution(f"Stop loss hit at {price:.4f}", "WARN")
        tg_sl_hit(STATE["current_symbol"], (STATE["entry"]-price)/STATE["entry"]*100)
        close_position_full()
        return True
    return False

def update_pnl_and_learning(pnl_pct):
    duration = (time.time() - STATE.get("entry_time", time.time())) / 60
    log_execution(f"CLOSE {STATE['current_symbol']} ({STATE['side']}) PnL: {pnl_pct:.2f}% in {duration:.1f} min",
                  "SUCCESS" if pnl_pct >= 0 else "ERROR")
    update_stats(pnl_pct)
    if pnl_pct < 0:
        STATE["consecutive_losses"] += 1
        cooldown = COOLDOWN_MINUTES_DRAWDOWN if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES else COOLDOWN_MINUTES_LOSS
        STATE["cooldown_until"] = datetime.now(timezone.utc) + timedelta(minutes=cooldown)
    else:
        STATE["consecutive_losses"] = 0

def cooldown_active():
    return STATE["cooldown_until"] and datetime.now(timezone.utc) < STATE["cooldown_until"]

def emergency_kill_switch_active():
    if STATE["daily_loss_limit_hit"]:
        return True
    bal = get_balance_safe()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if STATE["last_trade_day"] != today:
        STATE["daily_peak_balance"] = bal
        STATE["daily_loss_limit_hit"] = False
        STATE["last_trade_day"] = today
    else:
        if STATE["daily_peak_balance"] is None:
            STATE["daily_peak_balance"] = bal
        else:
            if bal > STATE["daily_peak_balance"]:
                STATE["daily_peak_balance"] = bal
            loss_pct = (STATE["daily_peak_balance"] - bal) / STATE["daily_peak_balance"] * 100
            if loss_pct >= MAX_DAILY_LOSS_PCT:
                STATE["daily_loss_limit_hit"] = True
                log_execution(f"Daily loss limit hit: {loss_pct:.1f}%", "ERROR")
                return True
    return False

def trailing_stop_new(price, atr):
    return False

# ========== SCANNER V2 FUNCTIONS ==========
def get_usdt_perp_symbols():
    try:
        ex.load_markets()
        markets = ex.markets
        symbols = []
        for s in markets:
            if "USDT" in s and markets[s].get('swap') and markets[s].get('active'):
                clean = s.replace(":USDT", "")
                symbols.append(clean)
        return symbols[:200]
    except Exception as e:
        log_execution(f"Failed to load markets: {e}", "ERROR")
        return [DEFAULT_SYMBOL]

def rf_proximity_score(rf, adx_val, vol_ok, rsi_val, atr_pct):
    dist = abs(rf["distance"]) if rf["distance"] else 1.0
    proximity = max(0.0, 1.0 - (dist / 0.015))
    if adx_val < 18:
        trend = 0.2
    elif 18 <= adx_val <= 30:
        trend = 1.0
    elif 30 < adx_val <= 40:
        trend = 0.6
    else:
        trend = 0.2
    if 30 <= rsi_val <= 70:
        rsi_score = 0.5
    elif 20 <= rsi_val < 30 or 70 < rsi_val <= 80:
        rsi_score = 0.3
    else:
        rsi_score = 0.0
    vol_score = 1.0 if vol_ok else 0.0
    vol_boost = 0.3 if 0.5 <= atr_pct <= 2.0 else 0.0
    trigger_boost = 1.2 if rf["triggered"] else 0.0
    score = (proximity * 0.35) + (trend * 0.25) + (vol_score * 0.15) + (rsi_score * 0.1) + (vol_boost * 0.05) + trigger_boost
    return float(score)

def scan_market_rf(top_n=40):
    symbols = get_usdt_perp_symbols()
    if not symbols:
        return []
    rf_engine = RFEngine(period=20, multiplier=3.5)
    results = []
    for sym in symbols[:150]:
        try:
            df = get_ohlcv_safe(sym, 120, htf=False)
            if df is None or not validate_dataframe(df, 100):
                continue
            try:
                atr_series = compute_atr(df, 14)
                adx_series = compute_adx(df, 14)
                rsi_series = compute_rsi(df, 14)
                atr_val = float(atr_series.iloc[-1])
                adx_val = float(adx_series.iloc[-1])
                rsi_val = float(rsi_series.iloc[-1])
                if rsi_val == 0 or rsi_val is None or math.isnan(rsi_val):
                    continue
                if atr_val == 0 or atr_val is None or math.isnan(atr_val):
                    continue
                if adx_val is None or math.isnan(adx_val):
                    adx_val = 20.0
                atr_pct = (atr_val / df['close'].iloc[-1]) * 100 if df['close'].iloc[-1] > 0 else 0
            except Exception:
                continue
            rf = rf_engine.compute(df)
            if rf["signal"] is None and abs(rf.get("distance", 1.0)) > 0.015:
                continue
            avg_vol = df['volume'].iloc[-20:].mean()
            vol_ok = df['volume'].iloc[-1] >= avg_vol * 0.7
            atr_pct = (atr_val / df['close'].iloc[-1]) * 100 if df['close'].iloc[-1] > 0 else 0
            score = rf_proximity_score(rf, adx_val, vol_ok, rsi_val, atr_pct)
            if score < 0.3:
                continue
            if rf["triggered"]:
                status = "TRIGGERED"
            elif score >= 0.6:
                status = "READY"
            else:
                status = "PROXIMITY"
            results.append({
                "symbol": sym,
                "score": round(score, 3),
                "rf_signal": rf["signal"],
                "rf_triggered": rf["triggered"],
                "rf_distance": round(rf.get("distance", 0), 4),
                "adx": round(adx_val, 1),
                "rsi": round(rsi_val, 1),
                "atrp": round(atr_pct, 2),
                "status": status
            })
        except Exception:
            continue
    results = sorted(results, key=lambda x: x["score"], reverse=True)
    return results[:top_n]

def smart_scanner_v2():
    symbols = get_usdt_perp_symbols()[:150]
    buy_candidates = []
    sell_candidates = []
    for sym in symbols:
        try:
            df = get_ohlcv_safe(sym, 150)
            if df is None or len(df) < 100:
                continue
            price = df['close'].iloc[-1]
            rf_engine = RFEngine(period=20, multiplier=3.5)
            rf = rf_engine.compute(df)
            if rf["distance"] is None:
                continue
            rf_prox = abs(rf["distance"])
            vol_ma = df['volume'].iloc[-21:-1].mean()
            if df['volume'].iloc[-1] < 0.5 * vol_ma:
                continue
            atr_val = compute_atr(df).iloc[-1]
            atr_pct = (atr_val / price) * 100 if price > 0 else 0
            if atr_pct < 0.2:
                continue
            liquidity_ctx = detect_liquidity_context(df, lookback=10)
            supports, resistances = get_clustered_zones(df, lookback=120, cluster_pct=0.002)
            zone_ctx = detect_zone_context(price, supports, resistances, threshold=0.003)
            structure_ctx = detect_structure_shift(df)
            rejection_buy = candle_rejection(df, "BUY")
            rejection_sell = candle_rejection(df, "SELL")
            vol_spike_flag = volume_spike(df)
            location = compute_location(df, price, "BUY")

            smart_money = SmartMoneyEngine.analyze_smart_money(df)
            momentum = MomentumFlowEngine.analyze_momentum_flow(df)

            score_mod_buy = 0
            score_mod_sell = 0

            if smart_money["smart_money_dominant"]:
                if smart_money["institutional_bias"] == "BUY":
                    score_mod_buy += 2.5
                elif smart_money["institutional_bias"] == "SELL":
                    score_mod_sell += 2.5
            if smart_money["distribution_risk"] > 70:
                score_mod_sell += 1.5
                score_mod_buy -= 2.0
            if smart_money["accumulation_strength"] > 60:
                score_mod_buy += 1.5
                score_mod_sell -= 2.0
            if smart_money["retail_euphoria"]:
                score_mod_buy -= 1.5
                score_mod_sell -= 1.5

            if momentum["trend_expansion"]:
                if momentum["flow_bias"] == "BUY":
                    score_mod_buy += 2.0
                elif momentum["flow_bias"] == "SELL":
                    score_mod_sell += 2.0
            if momentum["momentum_decay"]:
                score_mod_buy -= 1.5
                score_mod_sell -= 1.5
            if momentum["exhaustion_risk"] > 70:
                score_mod_buy -= 2.0
                score_mod_sell -= 2.0
            if momentum["climax_risk"] > 70:
                score_mod_buy -= 1.5
                score_mod_sell -= 1.5
            if momentum["greed_state"]:
                score_mod_buy -= 1.0
                score_mod_sell -= 1.0

            base_score_buy = 0
            if liquidity_ctx == "sell_side_taken":
                base_score_buy += 2
            if zone_ctx["near_support"]:
                base_score_buy += 2
            if structure_ctx == "bullish_shift":
                base_score_buy += 1.5
            if rf_prox < 0.0015:
                base_score_buy += 2
            elif rf_prox < 0.003:
                base_score_buy += 1
            if rejection_buy:
                base_score_buy += 1.5
            if vol_spike_flag:
                base_score_buy += 1

            base_score_sell = 0
            if liquidity_ctx == "buy_side_taken":
                base_score_sell += 2
            if zone_ctx["near_resistance"]:
                base_score_sell += 2
            if structure_ctx == "bearish_shift":
                base_score_sell += 1.5
            if rf_prox < 0.0015:
                base_score_sell += 2
            elif rf_prox < 0.003:
                base_score_sell += 1
            if rejection_sell:
                base_score_sell += 1.5
            if vol_spike_flag:
                base_score_sell += 1

            final_score_buy = base_score_buy + score_mod_buy
            final_score_sell = base_score_sell + score_mod_sell

            if final_score_buy >= 5:
                buy_candidates.append({
                    "symbol": sym,
                    "score": round(final_score_buy, 2),
                    "rf_prox": round(rf_prox*100, 3),
                    "liquidity": liquidity_ctx,
                    "zone": zone_ctx,
                    "structure": structure_ctx,
                    "rejection": rejection_buy,
                    "volume_spike": vol_spike_flag,
                    "location": location,
                    "smart_money": {
                        "bias": smart_money["institutional_bias"],
                        "bias_detailed": smart_money.get("institutional_bias_detailed", "NEUTRAL"),
                        "dominant": smart_money["smart_money_dominant"],
                        "distribution_risk": round(smart_money["distribution_risk"], 1),
                        "accumulation": round(smart_money["accumulation_strength"], 1)
                    },
                    "momentum": {
                        "expansion": momentum["trend_expansion"],
                        "decay": momentum["momentum_decay"],
                        "exhaustion_risk": round(momentum["exhaustion_risk"], 1),
                        "greed": momentum["greed_state"]
                    }
                })
            if final_score_sell >= 5:
                sell_candidates.append({
                    "symbol": sym,
                    "score": round(final_score_sell, 2),
                    "rf_prox": round(rf_prox*100, 3),
                    "liquidity": liquidity_ctx,
                    "zone": zone_ctx,
                    "structure": structure_ctx,
                    "rejection": rejection_sell,
                    "volume_spike": vol_spike_flag,
                    "location": compute_location(df, price, "SELL"),
                    "smart_money": {
                        "bias": smart_money["institutional_bias"],
                        "bias_detailed": smart_money.get("institutional_bias_detailed", "NEUTRAL"),
                        "dominant": smart_money["smart_money_dominant"],
                        "distribution_risk": round(smart_money["distribution_risk"], 1),
                        "accumulation": round(smart_money["accumulation_strength"], 1)
                    },
                    "momentum": {
                        "expansion": momentum["trend_expansion"],
                        "decay": momentum["momentum_decay"],
                        "exhaustion_risk": round(momentum["exhaustion_risk"], 1),
                        "greed": momentum["greed_state"]
                    }
                })
        except Exception as e:
            continue
    buy_sorted = sorted(buy_candidates, key=lambda x: x["score"], reverse=True)[:10]
    sell_sorted = sorted(sell_candidates, key=lambda x: x["score"], reverse=True)[:10]
    return buy_sorted, sell_sorted

def build_rf_dashboard():
    dashboard = []
    candidates = scan_market_rf(top_n=30)
    for c in candidates:
        dashboard.append({
            "symbol": c["symbol"],
            "status": c.get("status", "PROXIMITY"),
            "icon": "🔔" if c["rf_triggered"] else "📡",
            "score": c["score"],
            "adx": c["adx"],
            "rsi": c["rsi"],
            "atrp": c["atrp"],
            "signal": c["rf_signal"] or "N/A"
        })
    MEMORY["rf_dashboard"] = dashboard
    return dashboard

def run_scanner_v2():
    try:
        buy, sell = smart_scanner_v2()
        MEMORY["scanner_v2_buy"] = buy
        MEMORY["scanner_v2_sell"] = sell
        MEMORY["scanner_v2_last_scan"] = time.time()
        log_execution(f"[SCANNER] TOP BUY updated: {len(buy)} candidates", "INFO")
        log_execution(f"[SCANNER] TOP SELL updated: {len(sell)} candidates", "INFO")
    except Exception as e:
        log_execution(f"Smart Scanner v2 error: {e}", "ERROR")

# ========== RADAR FUNCTIONS ==========
def fast_market_filter(df):
    price = df['close'].iloc[-1]
    vol_usdt = df['volume'].iloc[-1] * price
    atr = compute_atr(df).iloc[-1]
    if vol_usdt < 1_000_000:
        return False
    if (atr / price) < 0.003:
        return False
    return True

def accumulation_v2(df):
    ema20 = df['close'].ewm(span=20).mean()
    ema50 = df['close'].ewm(span=50).mean()
    compression = abs(ema20.iloc[-1] - ema50.iloc[-1]) < df['close'].iloc[-1] * 0.002
    tight_range = (df['high'].rolling(10).max() - df['low'].rolling(10).min()) < df['close'].iloc[-1] * 0.01
    volume_dry = df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1]
    return compression and tight_range and volume_dry

def detect_sweep_simple(df):
    ctx = detect_liquidity_context(df)
    return ctx is not None

def radar_score(df):
    score = 0
    if accumulation_v2(df):
        score += 3
    if volume_pressure_real(df):
        score += 2
    if detect_sweep_simple(df):
        score += 2
    if near_key_zone(df, df['close'].iloc[-1]):
        score += 2
    return score

def near_key_zone(df, price):
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    for s in supports:
        if abs(price - s) / price < 0.003:
            return True
    for r in resistances:
        if abs(price - r) / price < 0.003:
            return True
    return False

def rebuild_radar_watchlist():
    symbols = get_usdt_perp_symbols()
    candidates = []
    for sym in symbols[:150]:
        try:
            df = get_ohlcv_safe(sym, 100)
            if df is None or not validate_dataframe(df, 80) or not fast_market_filter(df):
                continue
            score = radar_score(df)
            if score > 0:
                candidates.append({"symbol": sym, "score": score})
        except Exception:
            continue
    candidates.sort(key=lambda x: x["score"], reverse=True)
    MEMORY["radar_watchlist"] = candidates[:30]
    MEMORY["radar_top5"] = candidates[:5]
    log_execution(f"Radar rebuilt: {len(candidates)} candidates, top5: {[c['symbol'] for c in MEMORY['radar_top5']]}", "INFO")

def refresh_radar_watchlist():
    wl = MEMORY.get("radar_watchlist", [])
    updated = []
    for entry in wl:
        sym = entry["symbol"]
        try:
            df = get_ohlcv_safe(sym, 100)
            if df is None or not validate_dataframe(df, 80):
                continue
            score = radar_score(df)
            if score > 0:
                updated.append({"symbol": sym, "score": score})
        except Exception:
            continue
    updated.sort(key=lambda x: x["score"], reverse=True)
    MEMORY["radar_watchlist"] = updated[:30]
    MEMORY["radar_top5"] = updated[:5]
    log_execution(f"Radar refreshed: {len(updated)} symbols remain in watchlist", "INFO")

def radar_entry_scan():
    if not MEMORY.get("radar_top5"):
        return
    now = time.time()
    for entry in MEMORY["radar_top5"]:
        sym = entry["symbol"]
        last = LAST_ENTRY_PER_SYMBOL.get(sym, 0)
        if now - last < RADAR_COOLDOWN_SEC:
            continue
        df = get_ohlcv_safe(sym, 120)
        if df is None or not validate_dataframe(df, 80):
            continue
        price = df['close'].iloc[-1]
        atr_val = compute_atr(df).iloc[-1]
        ob = get_orderbook_cached(sym, limit=10)
        if ob is None:
            continue
        for side_try in ("BUY", "SELL"):
            should_enter, classification, reason_str = check_institutional_entry(sym, side_try, df, ob, atr_val, price)
            if should_enter:
                should_enter_narr, final_class, narrative = evaluate_with_narrative(sym, side_try, price, atr_val, df, ob, side_try)
                if not should_enter_narr:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, side_try, "REVERSAL", atr_val, df)
                ok = execute_entry(side_try, sym, price, sl, tp1, tp2, 85, reason_str, atr_val,
                                   trade_type="RADAR_INST", entry_type="SMART_EARLY", classification=classification)
                if ok:
                    LAST_ENTRY_PER_SYMBOL[sym] = now
                    return True
        decision, dec_side, dec_info = smart_decision(df, ob, sym)
        if decision == "STOP_HUNT":
            should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
            reason_str = f"RADAR_STOP_HUNT mode={dec_info.get('mode')} | NARR={narrative['classification']}"
            ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                               trade_type="RADAR_SMART", entry_type="RADAR_STOP_HUNT", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        elif decision == "EXHAUSTION_ENTRY":
            should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
            reason_str = f"RADAR_EXHAUSTION zone={dec_info.get('zone')} mode={dec_info.get('mode')} | NARR={narrative['classification']}"
            ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                               trade_type="RADAR_SMART", entry_type="RADAR_EXHAUSTION", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        total_v1, scn_v1, dir_v1, reasons_v1 = decision_score_v1(df, ob, atr_val, "BUY")
        total_v1 = apply_overrides_v1(df, atr_val, total_v1)
        if dir_v1 and total_v1 >= 5:
            should_enter, classification, narrative = evaluate_with_narrative(sym, dir_v1, price, atr_val, df, ob, dir_v1)
            if not should_enter:
                continue
            sl_v1, tp1_v1, tp2_v1 = compute_sl_tp(price, dir_v1,
                                                   "REVERSAL" if scn_v1 in ("TRAP","REVERSAL") else "EARLY_TREND",
                                                   atr_val, df)
            ok = decide_and_execute_v1(sym, dir_v1, total_v1, reasons_v1, price, sl_v1, tp1_v1, tp2_v1)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        total_score, scenario_name, scenario_dir, all_reasons = decision_score(df, ob, atr_val, "BUY")
        if total_score >= 7:
            should_enter, classification, narrative = evaluate_with_narrative(sym, scenario_dir, price, atr_val, df, ob, scenario_dir)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "EARLY_TREND", atr_val, df)
            reason_str = f"RADAR_UNIFIED_SNIPER ({scenario_name}) score={total_score} | NARR={narrative['classification']}"
            ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                               trade_type="RADAR_SCENARIO", entry_type="RADAR_SNIPER", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        elif total_score >= 5:
            should_enter, classification, narrative = evaluate_with_narrative(sym, scenario_dir, price, atr_val, df, ob, scenario_dir)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "EARLY_TREND", atr_val, df)
            reason_str = f"RADAR_UNIFIED_EARLY ({scenario_name}) score={total_score} | NARR={narrative['classification']}"
            ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                               trade_type="RADAR_SCENARIO", entry_type="RADAR_EARLY", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        for side in ("BUY", "SELL"):
            es, reasons = early_score(df, ob, atr_val, side)
            if es >= 6:
                should_enter, classification, narrative = evaluate_with_narrative(sym, side, price, atr_val, df, ob, side)
                if not should_enter:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, side, "EARLY_TREND", atr_val, df)
                reason_str = f"RADAR_EARLY ({','.join(reasons)}) score={es} | NARR={narrative['classification']}"
                ok = execute_entry(side, sym, price, sl, tp1, tp2, es, reason_str, atr_val,
                                   trade_type="RADAR_EARLY", entry_type="RADAR_SNIPER", classification=classification)
                if ok:
                    LAST_ENTRY_PER_SYMBOL[sym] = now
                    return True
    return False

# ========== DECISION SCORING FUNCTIONS ==========
def decision_score_v1(df, ob, atr_val, side):
    es, reasons = early_score(df, ob, atr_val, side)
    ctx = detect_liquidity_context(df)
    scenario = "TREND"
    direction = side
    if ctx == "sell_side_taken" and side == "BUY":
        scenario = "REVERSAL"
    elif ctx == "buy_side_taken" and side == "SELL":
        scenario = "REVERSAL"
    total_score = min(10, max(0, es + 2 if scenario == "REVERSAL" else es))
    return total_score, scenario, direction, reasons

def apply_overrides_v1(df, atr_val, score):
    if is_late_move(df, atr_val):
        score = max(0, score - 3)
    return score

def decide_and_execute_v1(symbol, side, total_score, reasons, price, sl, tp1, tp2):
    if total_score < 5:
        return False
    df = get_ohlcv_safe(symbol, 100)
    if df is None:
        return False
    ob = get_orderbook_cached(symbol, 10)
    atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
    should_enter, classification, narrative = evaluate_with_narrative(symbol, side, price, atr_val, df, ob, side)
    if not should_enter:
        return False
    reason_str = f"DECISION_V1 score={total_score} reasons={reasons} | NARR={narrative['classification']}"
    return execute_entry(side, symbol, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                         trade_type="DECISION_V1", entry_type="V1", classification=classification)

def decision_score(df, ob, atr_val, side):
    vol_state = classify_volume(df)
    scenario = advanced_detect_scenario(df, side, atr_val, vol_state)
    es, reasons = early_score(df, ob, atr_val, side)
    total = es
    if scenario == "TRAP_REVERSAL":
        total += 3
    elif scenario == "TREND_CONTINUATION":
        total += 2
    total = min(10, max(0, total))
    direction = side
    return total, scenario, direction, reasons

# ========== FLASK DASHBOARD ==========
app = Flask(__name__)

def render_live_supervisor_panel():
    return """
    <div id="rf-live-panel" style="display:none;" class="rf-live-supervisor">
      <div class="rf-live-header">
        <span class="rf-live-title">🧠 RF v28 Optimized Live Supervisor</span>
        <span id="rf-live-status-badge" class="rf-live-pill rf-live-pill-idle">⚡ ADAPTIVE LIVE SYNC</span>
      </div>
      <div class="rf-live-grid">
        <div class="rf-live-card"><div class="rf-live-metric-icon">💰</div><div class="rf-live-metric-label">Entry</div><div class="rf-live-metric-value" id="rf-sup-entry">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">📈</div><div class="rf-live-metric-label">Mark Price</div><div class="rf-live-metric-value" id="rf-sup-mark">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⚡</div><div class="rf-live-metric-label">ROE%</div><div class="rf-live-metric-value" id="rf-sup-roe">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">💵</div><div class="rf-live-metric-label">Unrealized PnL</div><div class="rf-live-metric-value" id="rf-sup-upnl">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">📊</div><div class="rf-live-metric-label">ADX</div><div class="rf-live-metric-value" id="rf-sup-adx">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🟢</div><div class="rf-live-metric-label">DI+</div><div class="rf-live-metric-value" id="rf-sup-dip">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🔴</div><div class="rf-live-metric-label">DI-</div><div class="rf-live-metric-value" id="rf-sup-dim">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🔥</div><div class="rf-live-metric-label">Continuation</div><div class="rf-live-metric-value" id="rf-sup-cont">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🧠</div><div class="rf-live-metric-label">Thesis Failure</div><div class="rf-live-metric-value" id="rf-sup-fail">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">✅</div><div class="rf-live-metric-label">Confidence</div><div class="rf-live-metric-value" id="rf-sup-conf">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🎯</div><div class="rf-live-metric-label">TP1</div><div class="rf-live-metric-value" id="rf-sup-tp1">❌</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🎯</div><div class="rf-live-metric-label">TP2</div><div class="rf-live-metric-value" id="rf-sup-tp2">❌</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⚡</div><div class="rf-live-metric-label">Trailing</div><div class="rf-live-metric-value" id="rf-sup-trail">❌</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🧠</div><div class="rf-live-metric-label">Personality</div><div class="rf-live-metric-value" id="rf-sup-personality">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🏦</div><div class="rf-live-metric-label">Institutional Flow</div><div class="rf-live-metric-value" id="rf-sup-flow">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⚙️</div><div class="rf-live-metric-label">Trade State</div><div class="rf-live-metric-value" id="rf-sup-state">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">📏</div><div class="rf-live-metric-label">Trail Mult</div><div class="rf-live-metric-value" id="rf-sup-trail-mult">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⏰</div><div class="rf-live-metric-label">Delay TP1</div><div class="rf-live-metric-value" id="rf-sup-delay-tp1">❌</div></div>
      </div>
      <div class="rf-live-status-row">
        <span id="rf-pill-thesis" class="rf-live-pill rf-live-pill-active">🧠 THESIS ACTIVE</span>
        <span id="rf-pill-trail" class="rf-live-pill">⚡ TRAILING OFF</span>
        <span id="rf-pill-flow" class="rf-live-pill">🏦 NEUTRAL</span>
        <span id="rf-pill-reclaim" class="rf-live-pill">🟢 RECLAIM LOW</span>
      </div>
    </div>
    <style>
    .rf-live-supervisor {
      background: linear-gradient(145deg, #0f1724 0%, #0a0f17 100%);
      border-radius: 20px;
      padding: 20px;
      margin-bottom: 20px;
      border: 1px solid #2c3e50;
    }
    .rf-live-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 18px;
      padding-bottom: 12px;
      border-bottom: 1px solid #2c3e50;
    }
    .rf-live-title {
      font-size: 18px;
      font-weight: bold;
      color: #00ffa6;
    }
    .rf-live-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .rf-live-card {
      background: #111827;
      border-radius: 14px;
      padding: 10px;
      text-align: center;
      transition: 0.2s;
    }
    .rf-live-metric-icon {
      font-size: 22px;
      margin-bottom: 4px;
    }
    .rf-live-metric-label {
      font-size: 11px;
      color: #9ca3af;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .rf-live-metric-value {
      font-size: 15px;
      font-weight: bold;
      color: #e6edf3;
      margin-top: 4px;
    }
    .rf-live-status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .rf-live-pill {
      background: #111827;
      padding: 6px 14px;
      border-radius: 30px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid #2c3e50;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .rf-live-pill-active {
      background: rgba(0, 255, 166, 0.1);
      border-color: #00ffa6;
      color: #00ffa6;
    }
    .rf-live-pill-failed {
      background: rgba(255, 77, 77, 0.1);
      border-color: #ff4d4d;
      color: #ff4d4d;
    }
    .rf-live-pill-trail {
      background: rgba(0, 255, 166, 0.1);
      border-color: #00ffa6;
    }
    .rf-live-pill-flow-buy {
      background: rgba(0, 255, 166, 0.1);
      border-color: #00ffa6;
      color: #00ffa6;
    }
    .rf-live-pill-flow-sell {
      background: rgba(255, 77, 77, 0.1);
      border-color: #ff4d4d;
      color: #ff4d4d;
    }
    .rf-live-pill-risk-low {
      color: #00ffa6;
    }
    .rf-live-pill-risk-mid {
      color: #ffc800;
    }
    .rf-live-pill-risk-high {
      color: #ff4d4d;
    }
    </style>
    """

@app.route("/")
def dashboard():
    rf_items = MEMORY.get("rf_dashboard", [])[:20]
    rf_html = "".join([f"<div>{item['icon']} {item['symbol']} | {item['status']} | score={item['score']:.2f} | ADX={item['adx']:.1f} | RSI={item['rsi']:.1f}</div>" for item in rf_items])
    
    scanner_buy = MEMORY.get("scanner_v2_buy", [])
    scanner_sell = MEMORY.get("scanner_v2_sell", [])
    buy_html = ""
    for b in scanner_buy:
        icon = "🔥" if b["score"] >= 7 else "⚡"
        sm = b.get("smart_money", {})
        mom = b.get("momentum", {})
        sm_str = f"{sm.get('bias_detailed', sm.get('bias', '?'))} "
        if sm.get("dominant"): sm_str += "🧠"
        mom_str = ""
        if mom.get("expansion"): mom_str += "🚀"
        if mom.get("decay"): mom_str += "📉"
        buy_html += f"<div>{icon} {b['symbol']} | Score: {b['score']}<br>📍 {b['location']} | RF: {b['rf_prox']}% | Vol: {'Spike' if b['volume_spike'] else 'Norm'} | Rej: {'✔' if b['rejection'] else '✖'}<br>🏦 {sm_str} | 📈 {mom_str}</div><hr>"
    sell_html = ""
    for s in scanner_sell:
        icon = "🔥" if s["score"] >= 7 else "⚡"
        sm = s.get("smart_money", {})
        mom = s.get("momentum", {})
        sm_str = f"{sm.get('bias_detailed', sm.get('bias', '?'))} "
        if sm.get("dominant"): sm_str += "🧠"
        mom_str = ""
        if mom.get("expansion"): mom_str += "🚀"
        if mom.get("decay"): mom_str += "📉"
        sell_html += f"<div>{icon} {s['symbol']} | Score: {s['score']}<br>📍 {s['location']} | RF: {s['rf_prox']}% | Vol: {'Spike' if s['volume_spike'] else 'Norm'} | Rej: {'✔' if s['rejection'] else '✖'}<br>🏦 {sm_str} | 📈 {mom_str}</div><hr>"
    scanner_v2_section = f"""
    <div class="section smart-layer"><div class="title">📡 SMART SCANNER v2 (Ranked)</div>
    <div style="display:flex; gap:20px;">
        <div style="flex:1; background:#0f1724; padding:12px; border-radius:8px;"><b>🟢 TOP 10 BUY</b><br>{buy_html or 'No candidates'}</div>
        <div style="flex:1; background:#0f1724; padding:12px; border-radius:8px;"><b>🔴 TOP 10 SELL</b><br>{sell_html or 'No candidates'}</div>
    </div>
    </div>
    """
    
    decision_panel_html = """
    <div id="decision-panel" style="padding:12px; border:1px solid #2c3e50; margin-bottom:16px; border-radius:8px; background:#0a0c10;">
      <h3>🧠 SMC Decision Engine (Scenario + Decision)</h3>
      <div id="decision-list" style="max-height:400px; overflow-y:auto; font-size:13px;"></div>
    </div>
    """
    
    watchlist_panel_html = """
    <div class="section smart-layer">
      <div class="title">👁 WATCHLIST / ACTIVE CANDIDATES</div>
      <div id="watchlist-panel" style="max-height:400px; overflow-y:auto; font-size:13px; background:#0f1724; padding:10px; border-radius:8px;">
        Loading...
      </div>
    </div>
    """
    
    no_entry_feed_section_html = """
    <div class="section smart-layer"><div class="title">🚫 WHY NO ENTRY (Last 5)</div>
    <div id="no-entry-feed" class="card" style="font-size:12px;"></div>
    </div>
    """
    
    free_balance_card = '<div class="card">FREE BALANCE<div id="free_bal">-</div><div id="avail_margin">-</div></div>'
    
    continuation_panel_html = """
    <div class="section smart-layer">
      <div class="title">📈 CONTINUATION ENGINE</div>
      <div id="continuation-panel" class="card" style="font-size:12px;"></div>
    </div>
    """
    
    thesis_panel_html = """
    <div class="section smart-layer">
      <div class="title">🧠 TRADE THESIS</div>
      <div id="thesis-panel" class="card" style="font-size:12px;"></div>
    </div>
    """
    
    confidence_regime_panel = """
    <div class="section smart-layer">
      <div class="title">📊 CONFIDENCE & REGIME</div>
      <div class="grid">
        <div class="card">Current Confidence<div id="current_conf">-</div></div>
        <div class="card">Market Regime<div id="market_regime">-</div></div>
        <div class="card">Continuation Pressure<div id="cont_pressure">-</div></div>
        <div class="card">Thesis Failure Score<div id="thesis_failure">-</div></div>
      </div>
    </div>
    """
    
    flow_section_html = """
    <div class="section smart-layer">
      <div class="title">🧠 Institutional Flow Intelligence</div>
      <div class="rf-flow-grid">
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Banker Pressure</div><div id="flow-banker" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Retail Pressure</div><div id="flow-retail" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Hot Money</div><div id="flow-hot" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Institutional Bias</div><div id="flow-bias" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Flow Alignment</div><div id="flow-align" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Distribution Risk</div><div id="flow-dist" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Momentum Health</div><div id="flow-mom-health" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Continuation Strength</div><div id="flow-cont-str" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Exhaustion Risk</div><div id="flow-exh-risk" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Climax Risk</div><div id="flow-climax" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Greed State</div><div id="flow-greed" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Smart Money Dominant</div><div id="flow-dom" class="rf-flow-value">-</div></div>
      </div>
    </div>
    <style>
    .rf-flow-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-top: 8px;
    }
    .rf-flow-card {
      background: #111827;
      border-radius: 12px;
      padding: 8px;
      text-align: center;
    }
    .rf-flow-metric-label {
      font-size: 11px;
      color: #9ca3af;
      text-transform: uppercase;
    }
    .rf-flow-value {
      font-size: 16px;
      font-weight: bold;
      margin-top: 4px;
      color: #e6edf3;
    }
    </style>
    """
    
    supervisor_panel_html = render_live_supervisor_panel()
    
    html = f"""
<!DOCTYPE html>
<html><head><title>RF v28 Optimized Live Supervisor</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{{background:#0b0f14;color:#e6edf3;font-family:Consolas;margin:0}}
.header{{padding:14px 16px;background:#111827;color:#00ff9f;font-size:22px;}}
.section{{padding:12px 14px;border-bottom:1px solid #1f2937}}
.title{{color:#9ca3af;margin-bottom:6px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.card{{background:#111827;border-radius:10px;padding:10px}}
.green{{color:#00ffa6}}
.red{{color:#ff4d4d}}
.log,.err{{max-height:220px;overflow:auto;white-space:pre-wrap;font-size:12px}}
.btn{{background:#2d3748;border:none;color:white;padding:8px 16px;margin:4px;border-radius:6px;cursor:pointer}}
.btn-buy{{background:#0f7b3a}}
.btn-sell{{background:#9b2c2c}}
.btn-close{{background:#4a5568}}
.smart-layer{{background:#0f1724;margin-top:12px;border-radius:8px}}
.position-details{{font-size:14px}}
</style>
</head>
<body>
<div class="header">🔥 RF v28 Optimized Live Supervisor</div>
{decision_panel_html}
{scanner_v2_section}
{supervisor_panel_html}
{flow_section_html}
{continuation_panel_html}
{thesis_panel_html}
{confidence_regime_panel}
<div class="section"><div class="title">💰 ACCOUNT & PERFORMANCE</div><div class="grid">
<div class="card">Balance<div id="bal">-</div></div>
{free_balance_card}
<div class="card">Mode<div id="mode">-</div></div>
<div class="card">Trades<div id="trades">0</div></div>
<div class="card">Wins<div id="wins" class="green">0</div></div>
<div class="card">Losses<div id="losses" class="red">0</div></div>
<div class="card">WinRate<div id="winrate">0%</div></div>
</div></div>
<div class="section"><div class="title">📊 TOTAL P&L & LAST TRADE</div><div class="grid">
<div class="card">Total PnL%<div id="total_pnl" class="green">0%</div></div>
<div class="card">Total PnL USDT<div id="total_pnl_usdt">0.00</div></div>
<div class="card">Last Trade<div id="last_trade">N/A</div></div>
</div></div>
<div class="section"><div class="title">📍 LIVE POSITION</div>
<div id="pos" class="card"></div>
</div>
<div class="section smart-layer"><div class="title">📡 TOP RF OPPORTUNITIES</div>
<div id="top5" class="card"></div>
</div>
<div class="section smart-layer"><div class="title">📡 RF SIGNALS (Trigger Candidates)</div>
<div id="rfSignals" class="card">{rf_html}</div>
</div>
{watchlist_panel_html}
<div class="section"><div class="title">📜 EXECUTION LOG</div><div id="logs" class="card log"></div></div>
<div class="section"><div class="title">🚨 SYSTEM ERRORS</div><div id="errors" class="card err"></div></div>
{no_entry_feed_section_html}
<div class="section"><div class="title">🎮 MANUAL CONTROLS</div>
<button class="btn btn-buy" onclick="manualTrade('BUY')">BUY</button>
<button class="btn btn-sell" onclick="manualTrade('SELL')">SELL</button>
<button class="btn btn-close" onclick="manualClose()">CLOSE</button>
</div>
<div class="section smart-layer"><div class="title">📡 MONITORING</div><div class="grid">
<div class="card">Regime<div id="regimeLabel">-</div></div>
<div class="card">Scanned<div id="scanned">0</div></div>
<div class="card">Last Scan<div id="lastScan">-</div></div>
</div></div>
<div class="section smart-layer"><div class="title">🩺 SYSTEM HEALTH</div><div class="grid">
<div class="card">API Status<div id="apiStatus">-</div></div>
<div class="card">Errors<div id="errCount">0</div></div>
<div class="card">Bot Status<div id="botStatus">-</div></div>
</div></div>
<script>
let lastFetch = 0;
let cachedData = null;
async function fetchData() {{
    const now = Date.now();
    if (cachedData && (now - lastFetch) < 2000) {{
        updateUI(cachedData);
        return;
    }}
    lastFetch = now;
    try {{
        const r = await fetch('/data');
        const d = await r.json();
        cachedData = d;
        updateUI(d);
    }} catch(e) {{ console.error(e); }}
}}
function updateUI(d) {{
    document.getElementById("bal").innerText = d.balance.toFixed(2);
    document.getElementById("free_bal").innerText = "$" + d.free_balance.toFixed(2);
    document.getElementById("avail_margin").innerText = "Margin: " + d.avail_margin.toFixed(2);
    document.getElementById("mode").innerText = d.mode;
    document.getElementById("trades").innerText = d.stats.trades;
    document.getElementById("wins").innerText = d.stats.wins;
    document.getElementById("losses").innerText = d.stats.losses;
    document.getElementById("winrate").innerText = d.stats.win_rate.toFixed(1)+"%";
    document.getElementById("total_pnl").innerHTML = d.total_pnl || "0%";
    document.getElementById("total_pnl_usdt").innerHTML = d.total_pnl_usdt ? d.total_pnl_usdt.toFixed(2) : "0.00";
    document.getElementById("last_trade").innerText = d.last_trade || "N/A";
    if(d.position) {{
        let pnlClass = d.position.pnl >= 0 ? "green" : "red";
        document.getElementById("pos").innerHTML = `
            <div><b>${{d.position.symbol}}</b> | ${{d.position.side}} | ${{d.position.entry_type}} (${{d.position.classification}})</div>
            <div>Entry: ${{d.position.entry}} | PnL: <span class="${{pnlClass}}">${{d.position.pnl}}%</span></div>
            <div>SL: ${{d.position.sl}} | TP1: ${{d.position.tp1}} | TP2: ${{d.position.tp2}}</div>
            <div>TP1 done: ${{d.position.tp1_done}} | Trailing: ${{d.position.trailing_active}}</div>
            <div>Location: ${{d.position.location}} | Zone: ${{d.position.zone}}</div>
            <div>Narrative: ${{d.position.narrative_classification}} (Conf: ${{d.position.narrative_confidence}}) | Conf Level: ${{d.position.confidence_level}}</div>
            <div>Current Confidence: ${{d.position.current_confidence}} | Regime: ${{d.position.market_regime}} | Cont. Pressure: ${{d.position.continuation_pressure}}</div>
            <div>Trade State: ${{d.position.trade_state}} | Trail Mult: ${{d.position.trail_multiplier}} | Delay TP1: ${{d.position.delay_tp1}}</div>
        `;
    }} else {{
        document.getElementById("pos").innerHTML = "No active trade";
    }}
    if(d.live_trade_mode && d.supervisor) {{
        const sup = d.supervisor;
        document.getElementById("rf-sup-entry").innerText = sup.entry_price?.toFixed(4) || "-";
        document.getElementById("rf-sup-mark").innerText = sup.mark_price?.toFixed(4) || "-";
        document.getElementById("rf-sup-roe").innerHTML = sup.roe_pct?.toFixed(2) + "%";
        document.getElementById("rf-sup-upnl").innerText = sup.unrealized_pnl?.toFixed(2) || "-";
        document.getElementById("rf-sup-adx").innerText = sup.adx?.toFixed(1) || "-";
        document.getElementById("rf-sup-dip").innerText = sup.di_plus?.toFixed(1) || "-";
        document.getElementById("rf-sup-dim").innerText = sup.di_minus?.toFixed(1) || "-";
        document.getElementById("rf-sup-cont").innerText = sup.continuation_pressure || "-";
        document.getElementById("rf-sup-fail").innerText = sup.thesis_failure_score || "-";
        document.getElementById("rf-sup-conf").innerText = sup.current_confidence?.toFixed(1) || "-";
        document.getElementById("rf-sup-tp1").innerHTML = sup.tp1_hit ? "✅" : "❌";
        document.getElementById("rf-sup-tp2").innerHTML = sup.tp2_hit ? "✅" : "❌";
        document.getElementById("rf-sup-trail").innerHTML = sup.trailing_active ? "✅" : "❌";
        document.getElementById("rf-sup-personality").innerText = sup.trade_personality || "NEUTRAL";
        document.getElementById("rf-sup-flow").innerText = sup.institutional_flow || "NEUTRAL";
        document.getElementById("rf-sup-state").innerText = sup.trade_state || "RANGE_CHOP";
        document.getElementById("rf-sup-trail-mult").innerText = sup.trail_multiplier || "1.5";
        document.getElementById("rf-sup-delay-tp1").innerHTML = sup.delay_tp1 ? "✅" : "❌";
        const reclaim = sup.reclaim_risk || 0;
        let reclaimClass = "rf-live-pill-risk-low";
        if (reclaim > 0.6) reclaimClass = "rf-live-pill-risk-high";
        else if (reclaim > 0.3) reclaimClass = "rf-live-pill-risk-mid";
        document.getElementById("rf-pill-reclaim").innerHTML = `🟢 RECLAIM ${{(reclaim*100).toFixed(0)}}%`;
        document.getElementById("rf-pill-reclaim").className = `rf-live-pill ${{reclaimClass}}`;
        const trailActive = sup.trailing_active;
        document.getElementById("rf-pill-trail").innerHTML = trailActive ? "⚡ TRAILING ON" : "⚡ TRAILING OFF";
        document.getElementById("rf-pill-trail").className = trailActive ? "rf-live-pill rf-live-pill-trail" : "rf-live-pill";
        document.getElementById("rf-live-panel").style.display = "block";
    }} else {{
        document.getElementById("rf-live-panel").style.display = "none";
    }}
    if(d.continuation_probability) {{
        let color = d.continuation_probability >= 0.65 ? "green" : (d.continuation_probability >= 0.5 ? "yellow" : "red");
        document.getElementById("continuation-panel").innerHTML = `
            <div>Continuation: <span style="color:${{color}};">${{(d.continuation_probability*100).toFixed(1)}}%</span></div>
            <div>Hold Quality: ${{d.hold_quality}}</div>
            <div>Trend Strength: ${{d.trend_strength}}</div>
            <div>Counter Pressure: ${{d.counter_pressure}}</div>
            <div>Reclaim Risk: ${{d.reclaim_risk}}</div>
            <div>Reasons: ${{(d.continuation_reasons || []).join(", ")}}</div>
        `;
    }}
    if(d.trade_thesis) {{
        let t = d.trade_thesis;
        document.getElementById("thesis-panel").innerHTML = `
            <div>Status: ${{t.current_status || "ACTIVE"}}</div>
            <div>Confidence: ${{t.confidence}}</div>
            <div>Continuation Prob: ${{t.continuation_probability}}</div>
            <div>Exhaustion Prob: ${{t.exhaustion_probability}}</div>
            <div>Entry Reasons: ${{(t.entry_reason || []).join(", ")}}</div>
            <div>Risks: ${{(t.risk_factors || []).join(", ")}}</div>
        `;
    }}
    document.getElementById("current_conf").innerHTML = (d.current_confidence || 50).toFixed(1);
    document.getElementById("market_regime").innerHTML = d.market_regime || "UNKNOWN";
    document.getElementById("cont_pressure").innerHTML = d.continuation_pressure || 50;
    document.getElementById("thesis_failure").innerHTML = d.thesis_failure_score || 0;
    document.getElementById("logs").innerHTML = (d.logs || []).slice(-15).join("<br>");
    document.getElementById("errors").innerHTML = (d.errors || []).slice(-5).join("<br>");
    let top5Html = "";
    (d.top5 || []).forEach(o => {{
        top5Html += `<div><b>${{o.symbol}}</b> | Score: ${{o.score.toFixed(2)}} | ADX: ${{o.adx || 0}} | RSI: ${{o.rsi || 0}}</div><hr>`;
    }});
    document.getElementById("top5").innerHTML = top5Html || "No opportunities";
    document.getElementById("scanned").innerText = d.scanned_count;
    document.getElementById("lastScan").innerText = d.last_scan ? new Date(d.last_scan*1000).toLocaleTimeString() : "-";
    document.getElementById("regimeLabel").innerText = d.regime;
    document.getElementById("apiStatus").innerText = d.health.api;
    document.getElementById("errCount").innerText = d.health.errors;
    document.getElementById("botStatus").innerText = d.health.status;
    if(d.rf_dashboard) {{
        let rfHtml = "";
        d.rf_dashboard.forEach(item => {{
            let signalIcon = item.signal === "BUY" ? "🟢" : (item.signal === "SELL" ? "🔴" : "⚪");
            rfHtml += `<div>${{item.icon}} ${{signalIcon}} ${{item.symbol}} | ${{item.status}} | score=${{item.score.toFixed(2)}} | ADX=${{item.adx||0}} | RSI=${{item.rsi||0}}</div>`;
        }});
        document.getElementById("rfSignals").innerHTML = rfHtml || "No RF signals";
    }}
    if(d.watchlist) {{
        let wHtml = "";
        for (let sym in d.watchlist) {{
            let w = d.watchlist[sym];
            let sideIcon = w.side === "BUY" ? "🟢" : "🔴";
            let strengthIcon = w.strength === "STRONG" ? "⚡" : (w.strength === "MEDIUM" ? "🟡" : "👁");
            let reasonsStr = (w.reasons || []).join(", ");
            let stateColor = "";
            if (w.state === "CONFIRMED") stateColor = "#2ecc71";
            else if (w.state === "DISPLACEMENT") stateColor = "#f1c40f";
            else if (w.state === "REJECTION") stateColor = "#e74c3c";
            else if (w.state === "RETEST") stateColor = "#3498db";
            else stateColor = "#95a5a6";
            let lastUpdate = new Date(w.last_update * 1000).toLocaleTimeString();
            let extraInfo = "";
            if (w.smart_money_bias_detailed) extraInfo += ` | Bias: ${{w.smart_money_bias_detailed}}`;
            else if (w.smart_money_bias) extraInfo += ` | Bias: ${{w.smart_money_bias}}`;
            if (w.distribution_risk !== undefined) extraInfo += ` | DistRisk: ${{w.distribution_risk}}`;
            if (w.momentum_expansion) extraInfo += ` | 🚀`;
            if (w.momentum_decay) extraInfo += ` | 📉`;
            wHtml += `<div style="margin-bottom:8px; border-bottom:1px solid #2c3e50; padding-bottom:4px;">
              <b>${{sideIcon}} ${{w.symbol}}</b> | Score: ${{w.score}} | <span style="color:${{stateColor}}">${{w.state}}</span> | ${{w.trade_type}} | ${{strengthIcon}} ${{w.strength}}
              <br>Reasons: ${{reasonsStr}}
              <br><small>Stage: ${{w.institutional_stage || "WATCH"}} | Prep: ${{w.institutional_prepared ? "YES" : "NO"}} | A-Grade: ${{w.a_grade_ready ? "YES" : "NO"}} ${{extraInfo}}</small>
              <br><small>Last update: ${{lastUpdate}}</small>
            </div>`;
        }}
        document.getElementById("watchlist-panel").innerHTML = wHtml || "No active candidates";
    }} else {{
        document.getElementById("watchlist-panel").innerHTML = "No watchlist data";
    }}
    let noEntryHtml = "";
    if(d.no_entry_feed) {{
        d.no_entry_feed.forEach(item => {{
            let timeStr = new Date(item.time * 1000).toLocaleTimeString();
            noEntryHtml += `<div>${{timeStr}} | ${{item.symbol}} ${{item.side}}: ${{item.reason}} (score ${{item.score}})</div>`;
        }});
    }}
    document.getElementById("no-entry-feed").innerHTML = noEntryHtml || "No recent skips";
    if(d.institutional_flow) {{
        let flow = d.institutional_flow;
        document.getElementById("flow-banker").innerHTML = flow.banker_pressure.toFixed(1);
        document.getElementById("flow-retail").innerHTML = flow.retailer_pressure.toFixed(1);
        document.getElementById("flow-hot").innerHTML = flow.hot_money.toFixed(1);
        document.getElementById("flow-bias").innerHTML = flow.institutional_bias_detailed || flow.institutional_bias;
        document.getElementById("flow-align").innerHTML = flow.flow_alignment.toFixed(1);
        document.getElementById("flow-dist").innerHTML = flow.distribution_risk.toFixed(1);
        document.getElementById("flow-mom-health").innerHTML = flow.momentum_health.toFixed(1);
        document.getElementById("flow-cont-str").innerHTML = flow.continuation_strength.toFixed(1);
        document.getElementById("flow-exh-risk").innerHTML = flow.exhaustion_risk.toFixed(1);
        document.getElementById("flow-climax").innerHTML = flow.climax_risk.toFixed(1);
        document.getElementById("flow-greed").innerHTML = flow.greed_state ? "🚨 Yes" : "✅ No";
        document.getElementById("flow-dom").innerHTML = flow.smart_money_dominant ? "✅ Yes" : "❌ No";
    }}
}}
async function manualTrade(side){{ const r=await fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:side}})}}); const res=await r.json(); alert(res.message); }}
async function manualClose(){{ const r=await fetch('/close',{{method:'POST'}}); const res=await r.json(); alert(res.message); }}
setInterval(fetchData, 2000);
async function loadDecision() {{
  try {{
    const res = await fetch('/decision');
    const json = await res.json();
    const list = json.data || [];
    const container = document.getElementById("decision-list");
    container.innerHTML = "";
    for (let i = 0; i < list.length; i++) {{
      const s = list[i];
      const div = document.createElement("div");
      div.style.borderBottom = "1px solid #222";
      div.style.padding = "8px";
      const color = (s.decision === "ENTER") ? "#2ecc71" : "#e74c3c";
      let reasonsHtml = (s.reasons || []).join(" + ") || "-";
      let entryHtml = "";
      if (s.decision === "ENTER") {{
        entryHtml = "Spread: OK (" + s.spread + " <= " + s.max_spread + ")<br><b style=\\"color:" + color + "\\">Decision: ENTER</b><br>ADX: " + (s.adx || "?") + " | Sweep: Yes" + (s.extra ? " | " + s.extra : "");
      }} else {{
        entryHtml = "<b style=\\"color:" + color + "\\">Decision: SKIP</b><br>Reason: " + (s.skip_reason || "-");
      }}
      div.innerHTML = "<b style=\\"color:" + color + "\\">" + s.symbol + " | " + s.side + "</b><br>RF: " + s.rf + "<br>Score: " + s.score + "<br>Reasons: " + reasonsHtml + "<br>Type: " + s.type + "<br>" + entryHtml;
      container.appendChild(div);
    }}
  }} catch(e) {{ console.error(e); }}
}}
setInterval(loadDecision, 3000);
loadDecision();
fetchData();
</script>
</body></html>
"""
    return html

@app.route("/data")
def data():
    try:
        bal = get_balance_safe()
        free_bal = get_free_balance_safe()
        avail_margin = free_bal
        mode = "LIVE" if MODE_LIVE else "PAPER"
        DASHBOARD_STATE["account"]["balance"] = bal
        DASHBOARD_STATE["account"]["free_balance"] = free_bal
        DASHBOARD_STATE["account"]["available_margin"] = avail_margin
        DASHBOARD_STATE["account"]["mode"] = mode
        perf = get_dashboard_metrics()
        pos = None
        if STATE["open"] and STATE.get("current_symbol"):
            roe = STATE.get("roe_pct", 0.0)
            pos = {
                "symbol": STATE["current_symbol"],
                "side": STATE["side"],
                "entry": round(STATE["entry"],4),
                "qty": STATE["qty"],
                "pnl": round(roe, 2),
                "sl": round(STATE.get("synthetic_sl",0),4),
                "tp1": round(STATE.get("synthetic_tp1",0),4),
                "tp2": round(STATE.get("tp2_price",0),4),
                "tp1_done": STATE.get("tp1_hit", False),
                "trailing_active": STATE.get("trail_activated", False),
                "regime": MEMORY.get("regime", "UNKNOWN"),
                "trade_type": STATE.get("trade_type", "N/A"),
                "entry_type": STATE.get("entry_type", "N/A"),
                "classification": STATE.get("classification", "N/A"),
                "location": STATE.get("location", "N/A"),
                "zone": STATE.get("zone_info", "N/A"),
                "score": STATE.get("trade_score", 0),
                "narrative_classification": STATE.get("narrative_classification", ""),
                "narrative_confidence": STATE.get("narrative_confidence", 0.0),
                "confidence_level": STATE.get("confidence_level", ""),
                "current_confidence": STATE.get("current_confidence", 50.0),
                "market_regime": STATE.get("market_regime", "UNKNOWN"),
                "continuation_pressure": STATE.get("continuation_pressure", 50),
                "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
                "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
                "delay_tp1": STATE.get("delay_tp1", False),
                "trade_style": STATE.get("trade_style", "SCALP"),
                "entry_timing": STATE.get("entry_timing", "WAIT_RETEST"),
                "market_phase": STATE.get("market_phase", "UNKNOWN"),
                "zone_behaviour": STATE.get("zone_behaviour", "NEUTRAL"),
                "trade_board": STATE.get("trade_board", {}),
                "symbol_availability": SYMBOL_GUARD.status(STATE.get("current_symbol", "")),
                "market_session": STATE.get("market_session", {}),
            }
        else:
            pos = DASHBOARD_STATE["position"]
        health = MEMORY["health"].copy()
        health["errors"] = len(DASHBOARD_STATE["errors"])
        top5 = MEMORY.get("top_candidates", [])[:5] if "top_candidates" in MEMORY else []
        cleanup_watchlist()
        watchlist_data = MEMORY.get("watchlist", {})
        no_entry_feed = MEMORY.get("no_entry_feed", [])[-5:]
        live_data = {}
        supervisor_data = None
        if DASHBOARD_STATE.get("live_trade_mode", False) and STATE.get("open"):
            supervisor_data = {
                "side": STATE["side"],
                "entry_price": STATE["entry"],
                "mark_price": STATE.get("mark_price", 0),
                "unrealized_pnl": STATE.get("unrealized_pnl_usdt", 0),
                "roe_pct": STATE.get("roe_pct", 0),
                "liquidation_price": STATE.get("liquidation_price", 0),
                "position_size": STATE["qty"],
                "leverage": LEVERAGE,
                "tp1_hit": STATE.get("tp1_hit", False),
                "tp2_hit": STATE.get("tp2_hit", False),
                "trailing_active": STATE.get("trail_activated", False),
                "adx": STATE.get("adx_live", 0),
                "di_plus": STATE.get("di_plus_live", 0),
                "di_minus": STATE.get("di_minus_live", 0),
                "continuation_pressure": STATE.get("continuation_pressure", 50),
                "trend_strength": STATE.get("trend_strength", 0),
                "thesis_failure_score": STATE.get("thesis_failure_score", 0),
                "current_confidence": STATE.get("current_confidence", 50),
                "trade_personality": STATE.get("trade_personality", "NEUTRAL"),
                "institutional_flow": STATE.get("institutional_flow", "NEUTRAL"),
                "reclaim_risk": STATE.get("reclaim_risk", 0),
                "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
                "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
                "delay_tp1": STATE.get("delay_tp1", False),
                "trade_style": STATE.get("trade_style", "SCALP"),
                "entry_timing": STATE.get("entry_timing", "WAIT_RETEST"),
                "market_phase": STATE.get("market_phase", "UNKNOWN"),
                "zone_behaviour": STATE.get("zone_behaviour", "NEUTRAL"),
                "trade_board": STATE.get("trade_board", {}),
            }
            live_data["live_trade_mode"] = True
            live_data["supervisor"] = supervisor_data
            live_data["lifecycle_state"] = _live_manager.lifecycle_state.value
        else:
            live_data["live_trade_mode"] = False

        institutional_flow_data = DASHBOARD_STATE.get("institutional_flow", {})
        if not institutional_flow_data and STATE.get("smart_money"):
            mf = STATE.get("momentum_flow", {})
            institutional_flow_data = {
                "banker_pressure": STATE["smart_money"].get("banker_pressure", 0),
                "retailer_pressure": STATE["smart_money"].get("retailer_pressure", 0),
                "hot_money": STATE["smart_money"].get("hot_money_pressure", 0),
                "institutional_bias": STATE["smart_money"].get("institutional_bias", "NEUTRAL"),
                "institutional_bias_detailed": STATE["smart_money"].get("institutional_bias_detailed", "NEUTRAL"),
                "flow_alignment": STATE["smart_money"].get("flow_alignment", 0),
                "distribution_risk": STATE["smart_money"].get("distribution_risk", 0),
                "momentum_health": mf.get("momentum_health", 0),
                "continuation_strength": mf.get("continuation_strength", 0),
                "exhaustion_risk": mf.get("exhaustion_risk", 0),
                "climax_risk": mf.get("climax_risk", 0),
                "greed_state": mf.get("greed_state", False),
                "smart_money_dominant": STATE["smart_money"].get("smart_money_dominant", False)
            }

        payload = {
            "balance": bal,
            "free_balance": free_bal,
            "avail_margin": avail_margin,
            "mode": mode,
            "stats": DASHBOARD_STATE["stats"],
            "position": pos,
            "logs": DASHBOARD_STATE["logs"][-30:],
            "errors": DASHBOARD_STATE["errors"][-10:],
            "top5": top5,
            "candidates": MEMORY.get("top_candidates", []),
            "scanned_count": len(MEMORY.get("top_candidates", [])),
            "last_scan": MEMORY["last_scan"],
            "regime": MEMORY["regime"],
            "health": health,
            "rf_dashboard": MEMORY.get("rf_dashboard", [])[:20],
            "total_pnl": perf["total_pnl"],
            "total_pnl_usdt": perf["total_pnl_usdt"],
            "last_trade": perf["last_trade"],
            "scanner_v2_buy": MEMORY.get("scanner_v2_buy", []),
            "scanner_v2_sell": MEMORY.get("scanner_v2_sell", []),
            "watchlist": watchlist_data,
            "no_entry_feed": no_entry_feed,
            "continuation_probability": STATE.get("continuation_probability", 0.5),
            "hold_quality": STATE.get("hold_quality", "UNKNOWN"),
            "counter_pressure": STATE.get("counter_pressure", 0.0),
            "reclaim_risk": STATE.get("reclaim_risk", 0.0),
            "trend_strength": STATE.get("trend_strength", 0.0),
            "continuation_reasons": STATE.get("continuation_reasons", []),
            "trade_thesis": STATE.get("trade_thesis", {}),
            "current_confidence": STATE.get("current_confidence", 50.0),
            "market_regime": STATE.get("market_regime", "UNKNOWN"),
            "continuation_pressure": STATE.get("continuation_pressure", 50),
            "thesis_failure_score": STATE.get("thesis_failure_score", 0),
            "institutional_flow": institutional_flow_data,
            "trade_board": STATE.get("trade_board", {}),
            "trade_style": STATE.get("trade_style", "SCALP"),
            "entry_timing": STATE.get("entry_timing", "WAIT_RETEST"),
            "market_phase": STATE.get("market_phase", "UNKNOWN"),
            "zone_behaviour": STATE.get("zone_behaviour", "NEUTRAL"),
            "paused_symbols": SYMBOL_GUARD.snapshot(),
            "market_session": STATE.get("market_session", {}),
            "external_intelligence": MEMORY.get("external_intelligence", {}),
            "external_intelligence_top": MEMORY.get("external_intelligence_top", []),
            "external_intelligence_status": MEMORY.get("external_intelligence_status", "INIT"),
            "external_intelligence_last_scan": MEMORY.get("external_intelligence_last_scan", 0),
            "last_live_refresh": DASHBOARD_STATE.get("last_live_refresh", time.time()),
            **live_data
        }
        safe_payload = safe_json(payload)
        return jsonify(safe_payload), 200
    except Exception as e:
        log_execution(f"/data error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 200

@app.route("/trade", methods=["POST"])
def manual_trade():
    data = request.json
    side = data.get("side")
    if not side or side not in ["BUY","SELL"]:
        return jsonify({"error": "Invalid side"}),400
    if STATE["open"]:
        return jsonify({"error": "Position open"}),400
    sync_position_state(DEFAULT_SYMBOL)
    if STATE["open"]:
        return jsonify({"error": "Already in position (synced with exchange)"}),400
    if INSUFFICIENT_MARGIN_COOLDOWN_UNTIL and time.time() < INSUFFICIENT_MARGIN_COOLDOWN_UNTIL:
        return jsonify({"error": "Insufficient margin cooldown active"}),400
    price = get_ticker_safe(DEFAULT_SYMBOL)
    if not price or price <= 0:
        return jsonify({"error": "No price"}),400
    df = get_ohlcv_safe(DEFAULT_SYMBOL, 100)
    if df is None:
        return jsonify({"error": "No data"}),500
    atr = compute_atr(df).iloc[-1]
    sl = price - atr*1.6 if side=="BUY" else price + atr*1.6
    tp1 = price*1.006 if side=="BUY" else price*0.994
    tp2 = price*1.02 if side=="BUY" else price*0.98
    classification = "SNIPER"
    ok = execute_entry(side, DEFAULT_SYMBOL, price, sl, tp1, tp2, 80, "Manual override", atr, "HYBRID", "MANUAL", classification)
    return jsonify({"message": "Done" if ok else "Failed"}),200 if ok else 500

@app.route("/close", methods=["POST"])
def manual_close():
    if not STATE["open"]:
        return jsonify({"error": "No position"}),400
    price = get_ticker_safe(STATE["current_symbol"])
    if price:
        finalize_trade_with_reality(STATE["current_symbol"])
    else:
        close_position_full()
        finalize_trade_with_reality(STATE["current_symbol"])
    return jsonify({"message": "Closed"}),200

@app.route("/health")
def health():
    return jsonify({"ok": True})

app.add_url_rule('/narrative-debug', 'narrative_debug', narrative_debug)

def keep_alive():
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            requests.get(f"http://localhost:{os.environ.get('PORT', 8000)}/health", timeout=5)
        except:
            pass

_last_cleanup = 0
def hourly_cleanup():
    global _last_cleanup
    if time.time() - _last_cleanup < 3600:
        return
    CACHE["ohlcv"].clear()
    CACHE["ticker"].clear()
    CACHE["orderbook"].clear()
    gc.collect()
    _last_cleanup = time.time()

_last_snapshot_time = 0
def print_snapshot():
    global _last_snapshot_time
    now = time.time()
    if now - _last_snapshot_time < SNAPSHOT_INTERVAL:
        return
    _last_snapshot_time = now
    bal = get_balance_safe()
    free_bal = get_free_balance_safe()
    mode = "LIVE" if MODE_LIVE else "PAPER"
    perf = get_dashboard_metrics()
    print("\n" + "="*70)
    print(color_text(f"🔥 RF v28 Optimized ({mode}) - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", BOLD))
    print(f"💰 Balance (Total): {color_text(f'{bal:.2f} USDT', GREEN)}   Free: {color_text(f'{free_bal:.2f} USDT', GREEN)}")
    print(f"📊 Total PnL: {color_text(perf['total_pnl'], GREEN if perf['total_pnl'].startswith('+') else RED)} | Last Trade: {perf['last_trade']}")
    regime = MEMORY.get("regime", "RANGE")
    regime_color = CYAN if regime == "TREND" else YELLOW
    print(f"🧠 Regime: {color_text(regime, regime_color)} | Scanned: {len(MEMORY.get('top_candidates', []))}")
    print_rf_dashboard()
    buy = MEMORY.get("scanner_v2_buy", [])
    sell = MEMORY.get("scanner_v2_sell", [])
    if buy or sell:
        print(color_text("=== Smart Scanner v2 (Ranked) ===", MAGENTA))
        if buy:
            print(f"  BUY top: {buy[0]['symbol']} score={buy[0]['score']}")
        if sell:
            print(f"  SELL top: {sell[0]['symbol']} score={sell[0]['score']}")
    if STATE["open"]:
        roe = STATE.get("roe_pct", 0.0)
        roe_colored = color_pnl(roe)
        print(f"📊 POSITION: {STATE['current_symbol']} {STATE['side']} ({STATE.get('entry_type','?')} / {STATE.get('classification','?')})")
        print(f"   Entry: {STATE['entry']:.4f} | ROE: {roe_colored} (5x leveraged)")
        print(f"   SL: {STATE.get('synthetic_sl',0):.4f} | TP1: {STATE.get('synthetic_tp1',0):.4f} | TP2: {STATE.get('tp2_price',0):.4f}")
        print(f"   Narrative: {STATE.get('narrative_classification','N/A')} (Conf: {STATE.get('narrative_confidence',0):.1f}) | Conf Level: {STATE.get('confidence_level','')}")
        cp = STATE.get("continuation_probability", 0.5)
        hq = STATE.get("hold_quality", "UNKNOWN")
        print(f"   Continuation: {cp*100:.1f}% | Hold Quality: {hq}")
        print(f"   Current Confidence: {STATE.get('current_confidence',50):.1f} | Market Regime: {STATE.get('market_regime','UNKNOWN')} | Cont. Pressure: {STATE.get('continuation_pressure',50)}")
        print(f"   Trade State: {STATE.get('trade_state','RANGE_CHOP')} | Trail Mult: {STATE.get('smart_trail_mult',1.5)} | Delay TP1: {STATE.get('delay_tp1',False)}")
        if STATE.get("tp1_hit"): print(color_text(f"   ✅ TP1 achieved - partial close, SL moved to breakeven", GREEN))
        if DASHBOARD_STATE.get("live_trade_mode", False):
            print(color_text(f"   [LIVE MGMT] State: {_live_manager.lifecycle_state.value}", MAGENTA))
    else:
        print("📊 POSITION: None")
    print("="*70 + "\n")

def print_rf_dashboard():
    print("\n" + color_text("=== RF TRIGGER CANDIDATES (Top 20) ===", MAGENTA))
    for item in MEMORY.get("rf_dashboard", [])[:20]:
        signal_icon = "🟢" if item["signal"] == "BUY" else "🔴" if item["signal"] == "SELL" else "⚪"
        print(f"{item['icon']} {signal_icon} {item['symbol']} | {item['status']} | score={item['score']:.2f} | ADX={item['adx']:.1f} | RSI={item['rsi']:.1f}")
    print("")

# ========== SNIPER V2 ==========
SNIPER_ZONES = {}

def is_pivot_high(df, lookback=3):
    if len(df) < lookback * 2 + 1:
        return False
    current_high = df['high'].iloc[-1]
    left_highs = df['high'].iloc[-(lookback+1):-1]
    if any(current_high <= h for h in left_highs):
        return False
    return True

def is_pivot_low(df, lookback=3):
    if len(df) < lookback * 2 + 1:
        return False
    current_low = df['low'].iloc[-1]
    left_lows = df['low'].iloc[-(lookback+1):-1]
    if any(current_low >= l for l in left_lows):
        return False
    return True

def detect_strong_pivot(df, side, atr):
    if len(df) < 20:
        return False, []
    reasons = []
    strong = False
    if side == "TOP":
        move_up = df['high'].iloc[-1] - df['low'].iloc[-6] if len(df) >= 6 else 0
        if move_up > atr * 1.5:
            reasons.append("strong_move_up")
            strong = True
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - max(last['open'], last['close'])
        if upper_wick > body:
            reasons.append("rejection_wick")
            strong = True
        ema50 = ema(df['close'], 50).iloc[-1]
        distance = abs(last['close'] - ema50) / ema50 if ema50 > 0 else 0
        if distance > atr / last['close']:
            reasons.append("overextended")
            strong = True
    else:
        move_down = df['high'].iloc[-6] - df['low'].iloc[-1] if len(df) >= 6 else 0
        if move_down > atr * 1.5:
            reasons.append("strong_move_down")
            strong = True
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['open'], last['close']) - last['low']
        if lower_wick > body:
            reasons.append("rejection_wick")
            strong = True
        ema50 = ema(df['close'], 50).iloc[-1]
        distance = abs(last['close'] - ema50) / ema50 if ema50 > 0 else 0
        if distance > atr / last['close']:
            reasons.append("overextended")
            strong = True
    return strong, reasons

def check_sniper_confirmation(df, zone_type):
    if len(df) < 2:
        return False, 0
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']
    confirm = 0
    if zone_type == 'TOP':
        upper_wick = last['high'] - max(last['open'], last['close'])
        if range_ > 0 and upper_wick > body * 0.5:
            confirm += 1
    else:
        lower_wick = min(last['open'], last['close']) - last['low']
        if range_ > 0 and lower_wick > body * 0.5:
            confirm += 1
    if zone_type == 'TOP':
        zone_high = SNIPER_ZONES.get(df.symbol if hasattr(df, 'symbol') else '', {}).get('high', last['high']+1)
        if last['high'] > zone_high and last['close'] < zone_high:
            confirm += 1
    else:
        zone_low = SNIPER_ZONES.get(df.symbol if hasattr(df, 'symbol') else '', {}).get('low', last['low']-1)
        if last['low'] < zone_low and last['close'] > zone_low:
            confirm += 1
    if zone_type == 'TOP':
        if last['close'] < last['open'] and prev['close'] > prev['open']:
            confirm += 1
    else:
        if last['close'] > last['open'] and prev['close'] < prev['open']:
            confirm += 1
    return confirm >= 2, confirm

def sniper_engine_v2():
    symbols = [c["symbol"] for c in MEMORY.get("radar_top5", [])] if MEMORY.get("radar_top5") else get_usdt_perp_symbols()[:20]
    for sym in symbols:
        df = get_ohlcv_safe(sym, 150)
        if df is None or not validate_dataframe(df, 100):
            continue
        price = df['close'].iloc[-1]
        atr_val = compute_atr(df).iloc[-1]
        df.symbol = sym
        if sym not in SNIPER_ZONES or SNIPER_ZONES[sym]["state"] in ("IDLE", "EXPIRED"):
            if is_pivot_high(df, lookback=3):
                strong_top, reasons_top = detect_strong_pivot(df, "TOP", atr_val)
                if strong_top:
                    zone = {
                        "type": "TOP",
                        "price": df['high'].iloc[-1],
                        "high": df['high'].iloc[-1],
                        "low": df['high'].iloc[-1] - atr_val * 0.5,
                        "time": time.time(),
                        "state": "WAIT",
                        "confirm_count": 0,
                        "reasons": reasons_top
                    }
                    SNIPER_ZONES[sym] = zone
                    log_execution(f"[SNIPER_V2] {sym} TOP zone created at {zone['price']:.4f} reasons={reasons_top}", "INFO")
                    continue
            if is_pivot_low(df, lookback=3):
                strong_bottom, reasons_bottom = detect_strong_pivot(df, "BOTTOM", atr_val)
                if strong_bottom:
                    zone = {
                        "type": "BOTTOM",
                        "price": df['low'].iloc[-1],
                        "low": df['low'].iloc[-1],
                        "high": df['low'].iloc[-1] + atr_val * 0.5,
                        "time": time.time(),
                        "state": "WAIT",
                        "confirm_count": 0,
                        "reasons": reasons_bottom
                    }
                    SNIPER_ZONES[sym] = zone
                    log_execution(f"[SNIPER_V2] {sym} BOTTOM zone created at {zone['price']:.4f} reasons={reasons_bottom}", "INFO")
                    continue
        if sym in SNIPER_ZONES:
            zone = SNIPER_ZONES[sym]
            if zone["state"] == "WAIT":
                if zone["type"] == "TOP":
                    if zone["low"] <= price <= zone["high"]:
                        zone["state"] = "READY"
                        log_execution(f"[SNIPER_V2] {sym} price returned to TOP zone, state -> READY", "INFO")
                    elif price > zone["high"] + atr_val * 0.2:
                        zone["state"] = "EXPIRED"
                        log_execution(f"[SNIPER_V2] {sym} TOP zone expired (price too high)", "WARN")
                else:
                    if zone["low"] <= price <= zone["high"]:
                        zone["state"] = "READY"
                        log_execution(f"[SNIPER_V2] {sym} price returned to BOTTOM zone, state -> READY", "INFO")
                    elif price < zone["low"] - atr_val * 0.2:
                        zone["state"] = "EXPIRED"
                        log_execution(f"[SNIPER_V2] {sym} BOTTOM zone expired (price too low)", "WARN")
                continue
            if zone["state"] == "READY":
                confirmed, conf_count = check_sniper_confirmation(df, zone["type"])
                if confirmed:
                    side = "SELL" if zone["type"] == "TOP" else "BUY"
                    ob = get_orderbook_cached(sym, limit=10)
                    should_enter, classification, narrative = evaluate_with_narrative(sym, side, price, atr_val, df, ob, side)
                    if not should_enter:
                        continue
                    sl = zone["high"] + atr_val * 0.4 if side == "SELL" else zone["low"] - atr_val * 0.4
                    tp1 = price * (1 - 0.004) if side == "SELL" else price * (1 + 0.004)
                    tp2 = price * (1 - 0.01) if side == "SELL" else price * (1 + 0.01)
                    reason_str = f"SNIPER_V2 {zone['type']} conf={conf_count} reasons={zone.get('reasons', [])} | NARR={narrative['classification']}"
                    ok = execute_entry(side, sym, price, sl, tp1, tp2, 9, reason_str, atr_val,
                                       trade_type="SNIPER_V2", entry_type="STRONG_PIVOT", classification=classification)
                    if ok:
                        log_execution(f"[SNIPER_V2] {sym} {side} entry executed", "SUCCESS")
                        zone["state"] = "USED"
                        SNIPER_ZONES.pop(sym, None)
                        return True
                elif conf_count >= 1:
                    continue
                else:
                    if time.time() - zone["time"] > 3600:
                        zone["state"] = "EXPIRED"
                        log_execution(f"[SNIPER_V2] {sym} zone expired after 1 hour", "WARN")
                    continue
            if zone["state"] == "EXPIRED":
                SNIPER_ZONES.pop(sym, None)
    return False

# ========== SCANNER CONTEXT MODE – update institutional flow without a trade ==========
def update_institutional_flow_scanner():
    try:
        df = get_ohlcv_safe(DEFAULT_SYMBOL, 100)
        if df is None or not validate_dataframe(df, 80):
            log_execution("[SCANNER] No valid data for institutional flow update, using defaults", "WARN")
            DASHBOARD_STATE["institutional_flow"] = {
                "banker_pressure": 50.0, "retailer_pressure": 50.0, "hot_money": 50.0,
                "institutional_bias": "NEUTRAL", "institutional_bias_detailed": "NEUTRAL",
                "flow_alignment": 25.0, "distribution_risk": 0.0,
                "momentum_health": 50.0, "continuation_strength": 0.0, "exhaustion_risk": 0.0,
                "climax_risk": 0.0, "greed_state": False, "smart_money_dominant": False
            }
            return
        smart = SmartMoneyEngine.analyze_smart_money(df)
        mom = MomentumFlowEngine.analyze_momentum_flow(df)
        DASHBOARD_STATE["institutional_flow"] = {
            "banker_pressure": smart["banker_pressure"],
            "retailer_pressure": smart["retailer_pressure"],
            "hot_money": smart["hot_money_pressure"],
            "institutional_bias": smart["institutional_bias"],
            "institutional_bias_detailed": smart.get("institutional_bias_detailed", "NEUTRAL"),
            "flow_alignment": smart["flow_alignment"],
            "distribution_risk": smart["distribution_risk"],
            "momentum_health": mom["momentum_health"],
            "continuation_strength": mom["continuation_strength"],
            "exhaustion_risk": mom["exhaustion_risk"],
            "climax_risk": mom["climax_risk"],
            "greed_state": mom["greed_state"],
            "smart_money_dominant": smart["smart_money_dominant"]
        }
        DASHBOARD_STATE["last_live_refresh"] = time.time()
        log_execution(f"[SCANNER] Institutional flow updated: bias={smart['institutional_bias_detailed']}, dominance={smart['smart_money_dominant']}", "INFO")
    except Exception as e:
        log_execution(f"[SCANNER] Error updating institutional flow: {e}", "ERROR")

# ========== LIVE INSTITUTIONAL UPDATER THREAD (PAUSED DURING TRADE) ==========
def live_institutional_updater():
    while True:
        try:
            if STATE.get("open"):
                time.sleep(5)
                continue
            symbol = DEFAULT_SYMBOL
            df = get_ohlcv_safe(symbol, 100)
            if df is not None and validate_dataframe(df, 80):
                smart = SmartMoneyEngine.analyze_smart_money(df)
                mom = MomentumFlowEngine.analyze_momentum_flow(df)
                with _TRADE_LOCK:
                    DASHBOARD_STATE["institutional_flow"] = {
                        "banker_pressure": smart["banker_pressure"],
                        "retailer_pressure": smart["retailer_pressure"],
                        "hot_money": smart["hot_money_pressure"],
                        "institutional_bias": smart["institutional_bias"],
                        "institutional_bias_detailed": smart.get("institutional_bias_detailed", "NEUTRAL"),
                        "flow_alignment": smart["flow_alignment"],
                        "distribution_risk": smart["distribution_risk"],
                        "momentum_health": mom["momentum_health"],
                        "continuation_strength": mom["continuation_strength"],
                        "exhaustion_risk": mom["exhaustion_risk"],
                        "climax_risk": mom["climax_risk"],
                        "greed_state": mom["greed_state"],
                        "smart_money_dominant": smart["smart_money_dominant"]
                    }
                DASHBOARD_STATE["last_live_refresh"] = time.time()
            else:
                update_institutional_flow_scanner()
        except Exception as e:
            log_execution(f"[LIVE_UPDATER] Error: {e}", "ERROR")
        time.sleep(5)

# ========== MAIN LOOP ==========
SNIPER_MODE = True
CANDIDATE_SCAN_INTERVAL = 15

def sync_all_states():
    if PAPER_MODE:
        MEMORY["position_status"] = "OPEN" if STATE.get("open") else "CLOSED"
        MEMORY["total_pnl"] = PERF.get("total_pnl_usdt", 0.0)
        MEMORY["total_pnl_pct"] = PERF.get("total_pnl_pct", 0.0) * 100
        return
    symbol = STATE.get("current_symbol") if STATE.get("open") else (TRADE_STATE.get("symbol") if TRADE_STATE.get("in_position") else None)
    if not symbol:
        real_pos = fetch_position(DEFAULT_SYMBOL)
        if real_pos is None:
            MEMORY["position_status"] = "CLOSED"
            MEMORY["current_position"] = None
        else:
            MEMORY["position_status"] = "OPEN"
            MEMORY["current_position"] = real_pos
        real_pnl, real_pnl_pct = get_realized_pnl_for_symbol(DEFAULT_SYMBOL, lookback_seconds=30)
        MEMORY["total_pnl"] = real_pnl
        MEMORY["total_pnl_pct"] = real_pnl_pct
        return
    valid = validate_position_state(STATE, symbol)
    if valid is None:
        log_execution(f"[SYNC] Position on {symbol} closed externally – cleaning local state.", "WARN")
        with _TRADE_LOCK:
            STATE["open"] = False
            TRADE_STATE["in_position"] = False
        MEMORY["position_status"] = "CLOSED"
        MEMORY["current_position"] = None
        MEMORY["entry"] = None
        MEMORY["sl"] = None
        MEMORY["tp"] = None
    else:
        MEMORY["position_status"] = "OPEN"
        MEMORY["current_position"] = valid
    real_pnl, real_pnl_pct = get_realized_pnl_for_symbol(symbol)
    MEMORY["total_pnl"] = real_pnl
    MEMORY["total_pnl_pct"] = real_pnl_pct
    if "total_pnl_usdt" in PERF:
        PERF["total_pnl_usdt"] = real_pnl
        PERF["total_pnl_pct"] = real_pnl_pct / 100

def validate_position_state(local_pos, symbol):
    real_pos = fetch_position(symbol)
    if real_pos is None:
        return None
    return real_pos

_external_intel_service = None
_external_intel_alerted = {}

def _get_external_intelligence_service():
    global _external_intel_service
    if not EXTERNAL_INTELLIGENCE_AVAILABLE or not EXTERNAL_INTELLIGENCE_ENABLED:
        return None
    if _external_intel_service is None:
        _external_intel_service = ExternalIntelligenceService()
    return _external_intel_service

def _external_intelligence_symbols():
    raw = os.getenv("EXTERNAL_INTELLIGENCE_SYMBOLS", os.getenv("DEEP_SCAN_STOCK_SYMBOLS", ""))
    return [x.strip() for x in raw.split(",") if x.strip()]

def run_external_intelligence_scan(force=False):
    """Run SEC/OpenInsider/Finviz enrichment and publish alert-only intelligence."""
    service = _get_external_intelligence_service()
    if service is None:
        MEMORY["external_intelligence_status"] = "DISABLED"
        return {}
    symbols = _external_intelligence_symbols()
    if not symbols:
        MEMORY["external_intelligence_status"] = "NO_SYMBOLS"
        return {}
    try:
        result = service.scan(symbols, force=force)
        MEMORY["external_intelligence"] = result
        MEMORY["external_intelligence_top"] = service.top(10)
        MEMORY["external_intelligence_last_scan"] = time.time()
        MEMORY["external_intelligence_status"] = "HEALTHY"
        for item in service.top(10):
            ticker = item.get("ticker", "?")
            score = float(item.get("score", 0))
            if item.get("direction") == "BUY" and score >= EXTERNAL_INTELLIGENCE_ALERT_SCORE:
                last = _external_intel_alerted.get(ticker, 0)
                if time.time() - last >= max(900.0, EXTERNAL_INTELLIGENCE_INTERVAL_SEC):
                    reasons = "; ".join(item.get("reasons", [])[:4])
                    send_once(
                        f"🧠 <b>EXTERNAL INTELLIGENCE ALERT</b>\n"
                        f"📊 {ticker}\n"
                        f"🔥 Score: {score:.0f}/100\n"
                        f"📈 Direction: BUY BIAS\n"
                        f"🧩 {reasons[:500]}\n"
                        f"⚠️ Advisory only — not an automatic trade signal.",
                        f"ext_intel_{ticker}",
                        cooldown=max(900, int(EXTERNAL_INTELLIGENCE_INTERVAL_SEC)),
                    )
                    _external_intel_alerted[ticker] = time.time()
        return result
    except Exception as exc:
        MEMORY["external_intelligence_status"] = "DEGRADED"
        log_execution(f"[EXT-INTEL] scan failed: {exc}", "WARN")
        return {}

def main_loop_sniper():
    global INSUFFICIENT_MARGIN_COOLDOWN_UNTIL, _last_queue_promote, _last_queue_eval
    global _watchlist_analysis_last_run, _watchlist_ranking_last_update, _watchlist_analysis_stats
    last_scan = 0
    last_scanner_v2 = 0
    last_radar_scan = 0
    last_radar_refresh = 0
    last_candidate_scan = 0
    last_flow_update = 0
    last_universe_build = 0
    last_discovery_scan = 0
    last_external_intel_scan = 0
    last_priority_update = 0
    watchlist_rotation = None
    try:
        ex.load_markets()
        log_execution(f"Markets loaded", "INFO")
    except Exception as e:
        log_execution(f"Failed to load markets: {e}", "ERROR")
    tg_start(get_balance_safe(), "LIVE" if MODE_LIVE else "PAPER")
    run_scanner_v2()
    log_execution("[SCANNER] Initial scanner v2 run completed", "INFO")
    updater_thread = threading.Thread(target=live_institutional_updater, daemon=True, name="live_institutional_updater")
    register_runtime_thread(updater_thread)
    updater_thread.start()

    _last_queue_promote = time.time()
    _last_queue_eval = time.time()
    _watchlist_analysis_last_run = time.time()
    _watchlist_ranking_last_update = time.time()

    if not hasattr(main_loop_sniper, '_radar'):
        main_loop_sniper._radar = InstitutionalRadar()
        main_loop_sniper._news_intel = NewsRiskIntelligence()
        main_loop_sniper._news_intel._radar = main_loop_sniper._radar
        main_loop_sniper._last_radar_update = 0
        main_loop_sniper._last_news_update = 0
        main_loop_sniper._last_news_cleanup = 0

    while True:
        try:
            now = time.time()

            if now - main_loop_sniper._last_radar_update >= 1:
                main_loop_sniper._radar.update_all()
                main_loop_sniper._last_radar_update = now

            if now - main_loop_sniper._last_news_update >= 30:
                news_items = fetch_news()
                if news_items:
                    main_loop_sniper._news_intel.process_news_feed(news_items)
                main_loop_sniper._last_news_update = now

            if now - main_loop_sniper._last_news_cleanup >= 300:
                cleanup_expired_news()
                main_loop_sniper._last_news_cleanup = now

            sync_all_states()

            if now - last_discovery_scan > GLOBAL_SCAN_INTERVAL:
                global_discovery_scan()
                last_discovery_scan = now

            if EXTERNAL_INTELLIGENCE_ENABLED and now - last_external_intel_scan >= EXTERNAL_INTELLIGENCE_INTERVAL_SEC:
                run_external_intelligence_scan()
                last_external_intel_scan = now

            if now - last_priority_update > 300:
                WatchlistPriorityManager.update_priorities()
                last_priority_update = now

            if USE_EXECUTION_QUEUE:
                if now - _watchlist_analysis_last_run >= 15:
                    _analyze_next_watchlist_candidate()
                    _watchlist_analysis_last_run = now
                if now - _watchlist_ranking_last_update >= 60:
                    _update_watchlist_rankings()
                    _watchlist_ranking_last_update = now

            if USE_EXECUTION_QUEUE:
                if now - _last_queue_promote > QUEUE_PROMOTE_INTERVAL:
                    promote_to_queue()
                    _last_queue_promote = now
                if now - _last_queue_eval > QUEUE_RE_EVAL_INTERVAL:
                    queue.re_evaluate_all(lambda sym: get_ohlcv_safe(sym, 100))
                    _last_queue_eval = now
                if not (STATE.get("open") or TRADE_STATE.get("in_position")):
                    process_queue_entry()

                if now % 60 < 1:
                    queue.cleanup()

            if now - last_universe_build > 1800:
                universe = build_40_symbol_universe()
                watchlist_rotation = WatchlistRotation(universe)
                log_execution(f"[UNIVERSE] Built 40-symbol universe: {universe[:10]}...", "INFO")
                last_universe_build = now

            if now - last_flow_update > 60:
                update_institutional_flow_scanner()
                last_flow_update = now

            if not (TRADE_STATE["in_position"] or STATE["open"]):
                sync_position_state()
                if STATE.get("open"):
                    continue
            if TRADE_STATE["in_position"] or STATE["open"]:
                _live_manager.manage_live_trade()
            else:
                if INSUFFICIENT_MARGIN_COOLDOWN_UNTIL and time.time() < INSUFFICIENT_MARGIN_COOLDOWN_UNTIL:
                    time.sleep(1)
                    continue
                if now - last_scan >= GLOBAL_SCAN_INTERVAL:
                    cands = scan_market_rf(top_n=40)
                    MEMORY["top_candidates"] = cands
                    MEMORY["rf_watchlist"] = cands[:30]
                    build_rf_dashboard()
                    MEMORY["last_scan"] = now
                    MEMORY["scanned_count"] = len(cands)
                    log_execution(f"RF Scanner: {len(cands)} candidates", "INFO")
                    last_scan = now
                if now - last_scanner_v2 >= SCANNER_V2_INTERVAL:
                    run_scanner_v2()
                    last_scanner_v2 = now
                if SNIPER_MODE:
                    if now - last_radar_scan >= SCAN_INTERVAL:
                        rebuild_radar_watchlist()
                        last_radar_scan = now
                    if now - last_radar_refresh >= WATCHLIST_REFRESH:
                        refresh_radar_watchlist()
                        last_radar_refresh = now
                if not (INSUFFICIENT_MARGIN_COOLDOWN_UNTIL and time.time() < INSUFFICIENT_MARGIN_COOLDOWN_UNTIL):
                    if now - last_candidate_scan >= CANDIDATE_SCAN_INTERVAL:
                        if watchlist_rotation and watchlist_rotation.should_rotate():
                            batch = watchlist_rotation.get_next_batch()
                            for sym in batch:
                                df = get_ohlcv_safe(sym, 60)
                                if df is None:
                                    continue
                                price = df['close'].iloc[-1]
                                atr_val = compute_atr(df).iloc[-1]
                                ob = get_orderbook_cached(sym, limit=10)
                                if ob is None:
                                    continue
                        smart_opportunity_selection()
                        last_candidate_scan = now
                    time.sleep(1)
                else:
                    time.sleep(1)
                continue
            if STATE["open"] and STATE.get("current_symbol"):
                sym = STATE["current_symbol"]
                price = get_ticker_safe(sym)
                if price and price > 0:
                    df = get_ohlcv_safe(sym, 50)
                    if df is not None:
                        if council_exit(df, price):
                            finalize_trade_with_reality(sym)
                            clear_position_dashboard()
                            STATE["open"] = False
                            TRADE_STATE["in_position"] = False
                            continue
                        atr = compute_atr(df).iloc[-1]
                        scaling_logic(sym, df, None)
                        current_pnl = STATE.get("roe_pct", 0.0)
                        update_position_dashboard(sym, STATE["side"], STATE["entry"], STATE["qty"], current_pnl)
            if emergency_kill_switch_active():
                if STATE["open"]:
                    close_position_full()
                    clear_position_dashboard()
                    TRADE_STATE["in_position"] = False
                time.sleep(60)
                continue
            print_snapshot()
            hourly_cleanup()
            time.sleep(BASE_SLEEP)
        except Exception as e:
            log_execution(f"Main loop error: {traceback.format_exc()}", "ERROR")
            time.sleep(BASE_SLEEP)

main_loop = main_loop_sniper

def safe_main_loop():
    while not shutdown_requested():
        try:
            main_loop()
        except Exception as e:
            tb = traceback.format_exc()
            print(f"CRITICAL EXCEPTION: {tb}")
            try:
                log_execution(f"CRITICAL EXCEPTION: {tb}", "ERROR")
            except Exception as log_err:
                print(f"Failed to log: {log_err}")
            _RUNTIME_SHUTDOWN.wait(5)

def fetch_news():
    return []

def cleanup_expired_news():
    now = time.time()
    watchlist = MEMORY.get("watchlist", {})
    for symbol, entry in list(watchlist.items()):
        news = entry.get("news", {})
        expires_at = news.get("expires_at", 0)
        if expires_at and now > expires_at:
            entry.pop("news", None)
            entry.pop("analysis_priority", None)
            entry.pop("analysis_interval", None)
            log_execution(f"[NEWS] {symbol} priority returned to normal (news expired)", "INFO")

def global_discovery_scan():
    pass

def build_40_symbol_universe():
    return ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

class WatchlistRotation:
    def __init__(self, universe):
        self.universe = universe
    def should_rotate(self):
        return True
    def get_next_batch(self):
        return self.universe[:5]

def process_queue_entry():
    if not USE_EXECUTION_QUEUE:
        return
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        return
    best = queue.get_best_candidate()
    if best is None:
        return
    if best.state != ExecutionState.READY:
        return
    price = best.price
    atr = best.atr
    sl, tp1, tp2 = compute_sl_tp(price, best.side, "REVERSAL", atr, None)
    log_execution(f"[QUEUE] Executing best candidate {best.symbol} {best.side} (score={best.priority_score})", "INFO")
    # Thread the Atom classification (TREND / REVERSAL / SNIPER_REVERSAL) through
    # to position management so the open trade is managed by its real thesis
    # type instead of the generic "QUEUE" label (which always normalized to TREND).
    manage_type = getattr(best, "trade_type", None) or "TREND"
    execute_entry(best.side, best.symbol, price, sl, tp1, tp2, best.priority_score, best.original_reason,
                  atr, manage_type, "EXECUTION_QUEUE", classification="INSTITUTIONAL_SNIPER")

def _analyze_next_watchlist_candidate():
    global _watchlist_analysis_pointer, _watchlist_analysis_stats
    watchlist = MEMORY.get("watchlist", {})
    if not watchlist:
        return
    keys = list(watchlist.keys())
    if _watchlist_analysis_pointer >= len(keys):
        _watchlist_analysis_pointer = 0
    if not keys:
        return
    symbol = keys[_watchlist_analysis_pointer]
    _watchlist_analysis_pointer += 1
    entry = watchlist.get(symbol)
    if not entry:
        return
    side = entry.get("side", "BUY")
    df = get_ohlcv_safe(symbol, 100)
    if not is_valid_dataframe(df) or len(df) < 30:
        return
    ob = get_orderbook_cached(symbol, limit=10)
    price = df['close'].iloc[-1]
    atr = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01

    bull_ob, bear_ob, ob_details = compute_order_block_quality(df, side, atr)
    ob_score = bull_ob if side == "BUY" else bear_ob

    auth_score, trap_risk, auth_details = compute_liquidity_authenticity(df, side, atr)

    pools = build_liquidity_pools(df)
    swept_high, swept_low = detect_sweep(df, pools)
    liq_score = 50
    if side == "BUY" and swept_low:
        liq_score += 25
    if side == "SELL" and swept_high:
        liq_score += 25

    struct_shift = detect_structure_shift(df)
    bos_up, bos_down = detect_bos(df)
    struct_score = 50
    if side == "BUY" and (struct_shift == "bullish_shift" or bos_up):
        struct_score = 90
    elif side == "SELL" and (struct_shift == "bearish_shift" or bos_down):
        struct_score = 90

    zones = get_smart_zones(symbol, df, ob)
    zone_price = None
    if side == "BUY" and zones.get("buy_zones"):
        zone_price = zones["buy_zones"][0]["price"]
    elif side == "SELL" and zones.get("sell_zones"):
        zone_price = zones["sell_zones"][0]["price"]
    proximity = 50
    if zone_price:
        dist = abs(price - zone_price) / price
        if dist < 0.003:
            proximity = 90
        elif dist < 0.01:
            proximity = 70
        else:
            proximity = 40

    roro_ok, roro_class, roro_reason = check_institutional_entry(symbol, side, df, ob, atr, price)
    roro_signal = roro_ok

    ob_result = queue._select_strong_ob(df, side, atr) if hasattr(queue, '_select_strong_ob') else {}
    strong_ob = ob_result.get('grade', 'INVALID') in ('A+', 'A')
    ob_grade = ob_result.get('grade', 'NONE')

    vol_state = classify_volume(df)
    displacement = detect_displacement(df, side, atr, vol_state)
    rejection = candle_rejection(df, side)

    rf = RFEngine(20, 3.5).compute(df)
    rf_aligned = (rf["signal"] == side) and (abs(rf["distance"]) < 0.003)

    composite = (
        ob_score * 0.20 +
        liq_score * 0.15 +
        struct_score * 0.15 +
        proximity * 0.10 +
        auth_score * 0.10
    )
    if roro_signal:
        composite += 15
    if strong_ob:
        composite += 10
    if rf_aligned:
        composite += 5
    if displacement or rejection:
        composite += 5
    if trap_risk > 60:
        composite -= 10
    composite = max(0, min(100, composite))

    entry["analysis"] = {
        "last_analyzed": time.time(),
        "ob_score": ob_score,
        "liq_score": liq_score,
        "struct_score": struct_score,
        "proximity": proximity,
        "auth_score": auth_score,
        "roro_signal": roro_signal,
        "roro_reason": roro_reason,
        "strong_ob": strong_ob,
        "ob_grade": ob_grade,
        "rf_aligned": rf_aligned,
        "displacement": displacement,
        "rejection": rejection,
        "vol_state": vol_state,
        "composite_score": composite,
        "trap_risk": trap_risk,
        "ob_details": ob_details,
        "auth_details": auth_details,
        "zones": zones,
        "price": price,
        "atr": atr,
        "side": side,
        "timestamp": time.time()
    }
    entry["last_update"] = time.time()
    _watchlist_analysis_stats["total_analyzed"] += 1
    _watchlist_analysis_stats["analyzed_this_cycle"] += 1
    log_execution(f"[WATCHLIST_ANALYSIS] Analyzed {symbol} {side} | composite={composite:.1f} | roro={roro_signal} | OB={ob_grade}", "INFO", debounce_key=f"watchlist_analysis_{symbol}", debounce_sec=60)
    log_execution(f"[INSTITUTION_DETAIL] {symbol} | RORO={roro_signal} | OB={ob_grade} | composite={composite:.1f}", "INFO")

def _update_watchlist_rankings():
    watchlist = MEMORY.get("watchlist", {})
    if not watchlist:
        return
    now = time.time()
    analyzed = []
    for sym, entry in watchlist.items():
        analysis = entry.get("analysis")
        if analysis and (now - analysis.get("last_analyzed", 0) < 300):
            analyzed.append((sym, analysis.get("composite_score", 0)))
    analyzed.sort(key=lambda x: x[1], reverse=True)
    for rank, (sym, score) in enumerate(analyzed, 1):
        if sym in watchlist:
            watchlist[sym]["rank"] = rank
            watchlist[sym]["composite_score"] = score
    top5 = [{"symbol": sym, "score": score} for sym, score in analyzed[:5]]
    MEMORY["watchlist_top5"] = top5
    log_execution(f"[WATCHLIST_RANKING] Updated ranks: {len(analyzed)} active, top={top5[0]['symbol'] if top5 else 'N/A'}", "INFO")

def promote_to_queue():
    """Admit PREPARED or A-GRADE candidates from Institutional Zone Analysis.

    MEDIUM/STRONG + a real precursor starts institutional analysis immediately.
    A precursor cluster (>=2) makes the setup PREPARED_FOR_ENTRY and eligible
    for the queue. A-GRADE remains the preferred fast-confirm path. The queue
    still owns the live trigger, zone, score, ATOM and risk gates, so admission
    is preparation — never an order.
    """
    global _watchlist_analysis_stats
    if not USE_EXECUTION_QUEUE or STATE.get("open") or TRADE_STATE.get("in_position"):
        return 0

    registry = MEMORY.get("institutional_zone_analysis", {})
    watchlist = MEMORY.get("watchlist", {})
    if not isinstance(registry, dict) or not registry:
        return 0

    candidates = []
    for sym, zentry in registry.items():
        entry = watchlist.get(sym)
        if not isinstance(entry, dict):
            continue
        # A-GRADE remains the preferred/highest-confidence path, but it is no
        # longer a mandatory second trip through the pipeline. Once a candidate
        # has a real institutional precursor cluster (e.g. displacement +
        # rejection, BOS/MSB + retest, sweep + boost), it is PREPARED_FOR_ENTRY
        # and enters the execution queue for live trigger/zone confirmation.
        # The queue still owns the final trigger, score, ATOM and risk gates.
        if not bool(entry.get("a_grade_ready")) and not bool(entry.get("institutional_prepared")):
            continue
        if not bool(entry.get("institutional_zone_active")):
            continue
        if str((entry.get("pre_expansion") or {}).get("phase", "NEUTRAL")).upper() in {"OVEREXTENDED", "EXHAUSTION", "INVALIDATED"}:
            continue
        if sym in queue._candidates:
            continue
        candidates.append((sym, entry, zentry))

    candidates.sort(
        key=lambda x: (float(x[2].get("institutional_score", 0) or 0),
                       float(x[2].get("composite_score", 0) or 0),
                       int(x[2].get("precursor_count", 0) or 0)),
        reverse=True,
    )

    promoted = 0
    for sym, entry, zentry in candidates:
        analysis = entry.get("analysis", {}) or {}
        price = float(analysis.get("price", entry.get("price", 0)) or 0)
        atr = float(analysis.get("atr", 0.01) or 0.01)
        side = str(analysis.get("side", entry.get("side", "BUY"))).upper()
        if price <= 0 or atr <= 0:
            continue
        sl, tp1, tp2 = compute_sl_tp(price, side, "REVERSAL", atr, None)
        cand = ExecutionCandidate(
            symbol=sym, side=side, price=price, entry_price=price,
            stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, atr=atr,
            df=None, ob=None, original_score=float(zentry.get("composite_score", 0) or 0),
            original_reason="INSTITUTIONAL_ZONE_A_GRADE", signal_type="institutional_zone_a_grade",
            ob_cfg=AssetBehaviorProfile.ob_config(AssetBehaviorProfile.resolve_asset_class(sym))
        )
        cand.roro_signal = bool(analysis.get("roro_signal", False))
        cand.ob_grade = str(analysis.get("ob_grade", "NONE"))
        cand.strong_ob_present = cand.ob_grade in ("A+", "A")
        cand.is_a_grade = bool(entry.get("a_grade_ready"))
        cand.priority_score = float(zentry.get("institutional_score", 0) or 0) + float(zentry.get("composite_score", 0) or 0) * 0.5
        cand.decision_reasons.append(
            "A-GRADE" if cand.is_a_grade else
            "PREPARED:" + "+".join(entry.get("institutional_prepared_reasons", [])[:6])
        )
        cand.institutional_score = float(zentry.get("institutional_score", 0) or 0)
        cand.institutional_status = str(entry.get("intent_status", "NEUTRAL"))
        cand.institutional_layers = entry.get("intent_details", {}) or {}
        cand.pre_institutional_state = str(entry.get("pre_institutional_state", "BUILDING"))
        cand.watchlist_entry_time = float(entry.get("watchlist_entry_time", 0) or 0)
        cand.institutional_analysis_time = float(entry.get("institutional_analysis_time", 0) or 0)
        cand.trade_type = entry.get("trade_type", "REVERSAL")
        cand.trade_style = entry.get("trade_style", "SCALP")
        cand.entry_timing = entry.get("entry_timing", "WAIT_RETEST")
        cand.market_phase = entry.get("market_phase", "UNKNOWN")
        cand.zone_behaviour = entry.get("zone_behaviour", "NEUTRAL")
        cand.trade_intelligence = entry.get("trade_intelligence", {}) or {}
        if queue.add_candidate(cand):
            promoted += 1
            entry["queue_promoted_at"] = time.time()
            admission_state = "A_GRADE_ADMITTED" if cand.is_a_grade else "PREPARED_ADMITTED"
            entry["queue_state"] = admission_state
            zentry["queue_state"] = admission_state
            stage_label = "A-GRADE" if cand.is_a_grade else "PREPARED_FOR_ENTRY"
            log_execution(
                f"[QUEUE] Institutional Zone -> Execution Queue: {sym} {side} "
                f"{stage_label} score={cand.priority_score:.1f}", "SUCCESS"
            )

    _watchlist_analysis_stats["promoted"] = _watchlist_analysis_stats.get("promoted", 0) + promoted
    MEMORY["watchlist_queue_promotions"] = MEMORY.get("watchlist_queue_promotions", 0) + promoted
    return promoted

# ========== SURGICAL ENTRY QUALITY ENHANCEMENTS ==========
def compute_liquidity_authenticity(df, side, atr):
    if not is_valid_dataframe(df) or len(df) < 3:
        return 50, 50, {}
    details = {}
    score = 50
    trap_risk = 50
    pools = build_liquidity_pools(df)
    swept_high, swept_low = detect_sweep(df, pools)
    sweep_detected = (side == "BUY" and swept_low) or (side == "SELL" and swept_high)
    if not sweep_detected:
        details['sweep_detected'] = False
        return 30, 70, details
    details['sweep_detected'] = True
    score += 20
    trap_risk -= 10
    last = df.iloc[-1]
    if side == "BUY":
        reclaim = last['close'] > last['low'] + 0.2 * atr
    else:
        reclaim = last['close'] < last['high'] - 0.2 * atr
    details['reclaim'] = reclaim
    if reclaim:
        score += 15
        trap_risk -= 10
    else:
        trap_risk += 15
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']
    if range_ > 0:
        displacement = body / range_
        details['displacement_ratio'] = round(displacement, 2)
        if displacement > 0.6:
            score += 20
            trap_risk -= 15
        elif displacement > 0.4:
            score += 10
            trap_risk -= 5
        else:
            trap_risk += 15
    vol_state = classify_volume(df)
    details['volume_state'] = vol_state
    if vol_state in ("expansion", "spike"):
        score += 15
        trap_risk -= 10
    elif vol_state == "exhaustion":
        trap_risk += 15
        score -= 5
    struct_shift = detect_structure_shift(df)
    bos_up, bos_down = detect_bos(df)
    struct_ok = (side == "BUY" and (struct_shift == "bullish_shift" or bos_up)) or \
                (side == "SELL" and (struct_shift == "bearish_shift" or bos_down))
    details['structure_break'] = struct_ok
    if struct_ok:
        score += 20
        trap_risk -= 15
    else:
        trap_risk += 15
    if side == "BUY" and last['close'] < last['open'] and body > atr * 0.6:
        trap_risk += 20
    elif side == "SELL" and last['close'] > last['open'] and body > atr * 0.6:
        trap_risk += 20
    supports, resistances = get_clustered_zones(df, lookback=60)
    price = df['close'].iloc[-1]
    if side == "BUY":
        nearest_res = min([r for r in resistances if r > price], default=None)
        if nearest_res:
            room = (nearest_res - price) / price
            if room < 0.01:
                trap_risk += 20
            elif room < 0.02:
                trap_risk += 10
            details['room_to_run'] = round(room*100, 2)
    else:
        nearest_sup = max([s for s in supports if s < price], default=None)
        if nearest_sup:
            room = (price - nearest_sup) / price
            if room < 0.01:
                trap_risk += 20
            elif room < 0.02:
                trap_risk += 10
            details['room_to_run'] = round(room*100, 2)
    if len(df) >= 2:
        prev = df.iloc[-2]
        if side == "BUY":
            if last['close'] > prev['close'] and prev['close'] > prev['open']:
                score += 5
            else:
                trap_risk += 5
        else:
            if last['close'] < prev['close'] and prev['close'] < prev['open']:
                score += 5
            else:
                trap_risk += 5
    score = max(0, min(100, score))
    trap_risk = max(0, min(100, trap_risk))
    details['authenticity_score'] = score
    details['trap_risk'] = trap_risk
    return score, trap_risk, details

def compute_order_block_quality(df, side, atr):
    if not is_valid_dataframe(df) or len(df) < 5:
        return 50, 50, {}
    details = {}
    bullish_score = 50
    bearish_score = 50
    ob_bullish = detect_order_block(df, "BUY")
    ob_bearish = detect_order_block(df, "SELL")
    def causal_strength(ob, side):
        if not ob:
            return 30, "NONE"
        idx = ob['idx']
        origin = df.iloc[idx]
        pos = len(df) + idx if idx < 0 else idx
        leg = df.iloc[pos + 1:min(len(df), pos + 4)]
        if side == "BUY":
            if not leg.empty:
                future_high = leg['high'].max()
                move = future_high - origin['low']
            else:
                move = 0
        else:
            if not leg.empty:
                future_low = leg['low'].min()
                move = origin['high'] - future_low
            else:
                move = 0
        if move < atr * 0.5:
            strength = 40
            label = "WEAK"
        elif move < atr * 1.0:
            strength = 60
            label = "MEDIUM"
        elif move < atr * 2.0:
            strength = 80
            label = "STRONG"
        else:
            strength = 90
            label = "VERY_STRONG"
        recent_revisit = False
        if side == "BUY":
            recent_low = df['low'].iloc[-3:].min()
            if abs(recent_low - ob['low']) < atr * 0.3:
                recent_revisit = True
        else:
            recent_high = df['high'].iloc[-3:].max()
            if abs(recent_high - ob['high']) < atr * 0.3:
                recent_revisit = True
        if recent_revisit:
            strength = max(30, strength - 20)
            label += "_TESTED"
        return strength, label
    if ob_bullish:
        bull_str, bull_label = causal_strength(ob_bullish, "BUY")
        bullish_score = bull_str
        details['bullish_ob'] = {'price': ob_bullish['high'], 'strength': bull_str, 'label': bull_label}
    else:
        details['bullish_ob'] = None
        bullish_score = 30
    if ob_bearish:
        bear_str, bear_label = causal_strength(ob_bearish, "SELL")
        bearish_score = bear_str
        details['bearish_ob'] = {'price': ob_bearish['low'], 'strength': bear_str, 'label': bear_label}
    else:
        details['bearish_ob'] = None
        bearish_score = 30
    bullish_score = max(0, min(100, bullish_score))
    bearish_score = max(0, min(100, bearish_score))
    details['bullish_ob_score'] = bullish_score
    details['bearish_ob_score'] = bearish_score
    return bullish_score, bearish_score, details

def evaluate_post_sweep_response(df, side, atr, price, entry_price):
    if not is_valid_dataframe(df) or len(df) < 2:
        return 50, {}
    details = {}
    score = 50
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']
    if range_ > 0:
        efficiency = body / range_
        details['efficiency'] = round(efficiency, 2)
        if efficiency > 0.7:
            score += 20
        elif efficiency > 0.5:
            score += 10
        else:
            score -= 10
    move = abs(last['close'] - last['open'])
    atr_move = move / atr if atr > 0 else 0
    details['atr_move'] = round(atr_move, 2)
    if atr_move > 1.0:
        score += 20
    elif atr_move > 0.6:
        score += 10
    else:
        score -= 10
    vol_avg = df['volume'].iloc[-10:-1].mean()
    if vol_avg > 0:
        vol_ratio = last['volume'] / vol_avg
        details['vol_ratio'] = round(vol_ratio, 2)
        if vol_ratio > 1.5:
            score += 15
        elif vol_ratio > 1.2:
            score += 8
        elif vol_ratio < 0.6:
            score -= 15
    if len(df) >= 3:
        if side == "BUY" and last['close'] > prev['close']:
            score += 10
        elif side == "SELL" and last['close'] < prev['close']:
            score += 10
        else:
            score -= 5
    if side == "BUY":
        if last['close'] > entry_price + atr * 0.3:
            score += 10
        elif last['close'] > entry_price:
            score += 5
        else:
            score -= 10
    else:
        if last['close'] < entry_price - atr * 0.3:
            score += 10
        elif last['close'] < entry_price:
            score += 5
        else:
            score -= 10
    score = max(0, min(100, score))
    details['response_score'] = score
    return score, details

def detect_early_expansion(df, side, atr, price, entry_price, smart_money, momentum, adx, continuation_prob):
    if not is_valid_dataframe(df):
        return 50, "MID_MOVE", []
    score = 50
    classification = "MID_MOVE"
    reasons = []
    dist = abs(price - entry_price) / entry_price
    if dist > 0.03:
        classification = "LATE_MOVE"
        reasons.append("price_extended")
        score -= 20
    elif dist > 0.015:
        score -= 10
        reasons.append("moderate_extension")
    else:
        score += 10
        reasons.append("near_entry")
    mom_health = momentum.get('momentum_health', 50)
    cont_strength = momentum.get('continuation_strength', 50)
    expansion = momentum.get('trend_expansion', False)
    decay = momentum.get('momentum_decay', False)
    if expansion and cont_strength > 60 and mom_health > 55:
        score += 25
        reasons.append("momentum_expansion")
    elif expansion and mom_health > 50:
        score += 15
        reasons.append("moderate_expansion")
    elif decay:
        score -= 20
        reasons.append("momentum_decay")
    elif mom_health < 30:
        score -= 15
        reasons.append("weak_momentum")
    if adx > 25 and adx < 40:
        score += 15
        reasons.append(f"adx_{int(adx)}")
    elif adx >= 40:
        if continuation_prob > 0.7:
            score += 5
            reasons.append("strong_trend_continuation")
        else:
            score -= 10
            reasons.append("adx_overextended")
    banker = smart_money.get('banker_pressure', 50)
    retail = smart_money.get('retailer_pressure', 50)
    dist_risk = smart_money.get('distribution_risk', 0)
    if side == "BUY" and banker > retail and dist_risk < 30:
        score += 15
        reasons.append("smart_money_accumulation")
    elif side == "SELL" and retail > banker and dist_risk > 50:
        score += 15
        reasons.append("smart_money_distribution")
    elif dist_risk > 60:
        score -= 20
        reasons.append("distribution_risk")
    vol_state = classify_volume(df)
    if vol_state in ("expansion", "spike"):
        score += 10
        reasons.append("volume_expansion")
    elif vol_state == "exhaustion":
        score -= 15
        reasons.append("volume_exhaustion")
    if continuation_prob > 0.75:
        score += 10
        reasons.append("high_continuation_prob")
    elif continuation_prob < 0.5:
        score -= 10
        reasons.append("low_continuation_prob")
    exhaustion = momentum.get('exhaustion_risk', 0)
    if exhaustion > 60:
        score -= 20
        reasons.append("exhaustion_risk")
        classification = "EXHAUSTION"
    elif exhaustion > 40:
        score -= 10
        reasons.append("moderate_exhaustion")
    score = max(0, min(100, score))
    if score >= 80 and "near_entry" in reasons and "momentum_expansion" in reasons and "smart_money_accumulation" in reasons:
        classification = "EARLY_EXPANSION"
    elif score >= 70 and ("moderate_expansion" in reasons or "strong_trend_continuation" in reasons):
        classification = "EARLY_CONTINUATION"
    elif score >= 50:
        classification = "MID_MOVE"
    elif score >= 30:
        if "distribution_risk" in reasons or "exhaustion_risk" in reasons:
            classification = "EXHAUSTION"
        else:
            classification = "LATE_MOVE"
    else:
        classification = "TRAP"
    return score, classification, reasons

def entry_quality_assessment(symbol, side, price, df, ob, atr, existing_score, classification, trade_type, entry_type):
    if not is_valid_dataframe(df) or len(df) < 30:
        return {'decision': 'REJECT', 'reason': 'Insufficient data', 'quality_score': 0}
    auth_score, trap_risk, auth_details = compute_liquidity_authenticity(df, side, atr)
    bull_ob, bear_ob, ob_details = compute_order_block_quality(df, side, atr)
    resp_score, resp_details = evaluate_post_sweep_response(df, side, atr, price, price)
    smart = SmartMoneyEngine.analyze_smart_money(df)
    mom = MomentumFlowEngine.analyze_momentum_flow(df)
    adx_series = compute_adx(df)
    adx = adx_series.iloc[-1] if adx_series is not None else 20.0
    thesis = STATE.get('trade_thesis') or {}
    cont_eval = _continuation_engine.evaluate(side, df, {'atr': atr, 'adx': adx, 'di_plus': 0, 'di_minus': 0}, thesis)
    early_score, early_class, early_reasons = detect_early_expansion(df, side, atr, price, price, smart, mom, adx, cont_eval.continuation_probability)
    weights = {
        'authenticity': 0.25,
        'order_block': 0.20,
        'response': 0.15,
        'early_expansion': 0.25,
        'continuation_prob': 0.15
    }
    quality_score = (
        auth_score * weights['authenticity'] +
        (bull_ob if side == "BUY" else bear_ob) * weights['order_block'] +
        resp_score * weights['response'] +
        early_score * weights['early_expansion'] +
        cont_eval.continuation_probability * 100 * weights['continuation_prob']
    )
    quality_score = max(0, min(100, quality_score))
    if trap_risk > 70:
        decision = 'REJECT'
        reason = f'High trap risk ({trap_risk:.0f})'
    elif early_class in ('EXHAUSTION', 'TRAP', 'LATE_MOVE') and quality_score < 60:
        decision = 'REJECT'
        reason = f'Early classification {early_class} with low quality'
    elif auth_score < 40:
        decision = 'REJECT'
        reason = 'Poor liquidity authenticity'
    elif (side == "BUY" and bear_ob > 70 and quality_score < 65) or (side == "SELL" and bull_ob > 70 and quality_score < 65):
        decision = 'REJECT'
        reason = 'Strong opposing OB outweighs quality'
    elif quality_score < 50:
        decision = 'WAIT'
        reason = 'Quality score below threshold'
    elif quality_score >= 70 and early_class in ('EARLY_EXPANSION', 'EARLY_CONTINUATION') and trap_risk < 25:
        decision = 'EARLY_ENTRY'
        reason = 'Exceptional early expansion setup'
    elif quality_score >= 70 and trap_risk < 40:
        decision = 'APPROVE'
        reason = 'Good quality setup'
    elif quality_score >= 60 and trap_risk < 50:
        decision = 'VALIDATE'
        reason = 'Requires next candle confirmation'
    else:
        decision = 'WAIT'
        reason = 'Insufficient confluence'
    log_execution(
        f"[ENTRY_QUALITY] {symbol} {side} | Quality={quality_score:.0f} | Trap={trap_risk:.0f} | "
        f"Auth={auth_score:.0f} | OB={bull_ob:.0f}/{bear_ob:.0f} | Early={early_class} | {decision}",
        "INFO", debounce_key=f"eq_{symbol}_{side}", debounce_sec=60
    )
    return {
        'decision': decision,
        'reason': reason,
        'quality_score': quality_score,
        'trap_risk': trap_risk,
        'early_expansion': early_class,
        'authenticity_score': auth_score,
        'order_block_score': bull_ob if side == "BUY" else bear_ob,
        'response_score': resp_score,
        'continuation_probability': cont_eval.continuation_probability,
        'details': {
            'auth': auth_details,
            'ob': ob_details,
            'response': resp_details,
            'early_reasons': early_reasons
        }
    }

# ========== EXECUTION QUEUE INTEGRATION ==========
class OrderBlockQuality(Enum):
    FRESH = "FRESH"
    TESTED = "TESTED"
    WEAK = "WEAK"
    BROKEN = "BROKEN"
    FAKE = "FAKE"

class InstitutionalBehaviour(Enum):
    ACCUMULATION = "ACCUMULATION"
    DISTRIBUTION = "DISTRIBUTION"
    RE_ACCUMULATION = "RE_ACCUMULATION"
    RE_DISTRIBUTION = "RE_DISTRIBUTION"
    NEUTRAL = "NEUTRAL"

class MarketStructure(Enum):
    BOS = "BOS"
    CHOCH = "CHOCH"
    MSS = "MSS"
    NONE = "NONE"

class OpportunityType(Enum):
    INSTITUTIONAL_REVERSAL = "INSTITUTIONAL_REVERSAL"
    TREND_CONTINUATION = "TREND_CONTINUATION"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    DISTRIBUTION_ENTRY = "DISTRIBUTION_ENTRY"
    ACCUMULATION_ENTRY = "ACCUMULATION_ENTRY"
    LOW_QUALITY = "LOW_QUALITY"
    FAKE_BREAKOUT = "FAKE_BREAKOUT"
    WEAK_ORDER_BLOCK = "WEAK_ORDER_BLOCK"

def record_gate_event(symbol, stage, blocker, detail="", side=""):
    try:
        feed = MEMORY.setdefault("gate_feed", [])
        feed.append({
            "time": time.time(),
            "symbol": symbol,
            "side": side,
            "stage": stage,
            "blocker": blocker,
            "detail": str(detail)[:220],
        })
        if len(feed) > 60:
            del feed[:-60]
        if _append_decision_journal is not None:
            _append_decision_journal(
                symbol=symbol, side=side, stage=stage,
                decision="VETO" if blocker else "OBSERVE",
                reason=blocker, detail=detail,
            )
    except Exception:
        pass

class ExecutionState(Enum):
    DISCOVERED = "DISCOVERED"
    WATCHLIST = "WATCHLIST"
    GOOD_ZONE = "GOOD_ZONE"
    WAITING_TRIGGER = "WAITING_TRIGGER"
    TRIGGER_DETECTED = "TRIGGER_DETECTED"
    ENTRY_VALIDATION = "ENTRY_VALIDATION"
    READY = "READY"
    EXECUTED = "EXECUTED"
    INVALIDATED = "INVALIDATED"
    RETURNED_WATCHLIST = "RETURNED_WATCHLIST"

@dataclass
class ZoneMetrics:
    order_block_quality: float = 50.0
    zone_strength: float = 50.0
    liquidity_quality: float = 50.0
    institutional_confidence: float = 50.0
    structure_alignment: float = 50.0
    entry_timing: float = 50.0
    trend_alignment: float = 50.0
    risk_score: float = 50.0
    trigger_state: str = "WAITING_TRIGGER"
    liquidity_evidence: dict = field(default_factory=dict)
    ifvg_warning: str = "CLEAR"
    ifvg_penalty: float = 0.0
    confluence_bonus: float = 0.0

    @property
    def final_zone_score(self) -> float:
        weights = {
            'order_block_quality': 0.12,
            'zone_strength': 0.18,
            'liquidity_quality': 0.18,
            'institutional_confidence': 0.15,
            'structure_alignment': 0.15,
            'entry_timing': 0.10,
            'trend_alignment': 0.05,
            'risk_score': 0.07
        }
        score = 0.0
        for attr, w in weights.items():
            score += getattr(self, attr, 50) * w
        if self.ifvg_penalty and self.ifvg_penalty > 0:
            score = max(0.0, score - float(self.ifvg_penalty))
        if self.confluence_bonus and self.confluence_bonus > 0:
            score = min(100.0, score + float(self.confluence_bonus))
        return round(score, 2)

@dataclass
class ExecutionCandidate:
    symbol: str
    side: str
    price: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    atr: float
    df: pd.DataFrame
    ob: Any
    zone_metrics: ZoneMetrics = field(default_factory=ZoneMetrics)
    opportunity_type: OpportunityType = OpportunityType.LOW_QUALITY
    market_structure: MarketStructure = MarketStructure.NONE
    institutional_behaviour: InstitutionalBehaviour = InstitutionalBehaviour.NEUTRAL
    state: ExecutionState = ExecutionState.DISCOVERED
    priority_score: float = 0.0
    added_at: float = field(default_factory=time.time)
    last_evaluated: float = field(default_factory=time.time)
    evaluation_count: int = 0
    confirmation_count: int = 0
    last_confirm_signature: str = ""
    last_confirm_marker: str = ""
    original_score: float = 0.0
    original_reason: str = ""
    signal_type: str = ""
    zone_low: float = 0.0
    zone_high: float = 0.0
    zone_origin_bar: int = -1
    zone_created_at: float = 0.0
    zone_touches: int = 0
    zone_state: str = "ACTIVE"
    entry_distance_atr: float = 0.0
    decision: str = ""
    decision_label: str = ""
    decision_reasons: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    gate_status: dict = field(default_factory=dict)
    roro_signal: bool = False
    roro_reason: str = ""
    ob_grade: str = "NONE"
    is_a_grade: bool = False
    strong_ob_present: bool = False
    roro_score: float = 0.0
    institutional_score: float = 0.0
    institutional_status: str = "NEUTRAL"
    institutional_layers: dict = field(default_factory=dict)
    institutional_acceleration: float = 0.0
    pre_institutional_state: str = "IDLE"
    news_risk_level: str = "NEUTRAL"
    news_impact_score: float = 0.0
    news_event_type: str = ""
    watchlist_entry_time: float = 0.0
    institutional_analysis_time: float = 0.0
    expansion_detected_time: float = 0.0
    confirmation_logged: bool = False
    expansion_logged: bool = False
    asset_class: str = "CRYPTO"
    ob_cfg: dict = field(default_factory=dict)
    # ===== ATOM INTELLIGENCE LAYER (Roro Entry -> Atom Approval) =====
    trade_type: str = TRADE_TREND
    atom_intel: dict = field(default_factory=dict)
    atom_approved: bool = False
    atom_hard_reject: bool = False
    atom_sniper_confirmed: bool = False
    atom_confidence_adjust: float = 0.0
    trade_style: str = "SCALP"
    entry_timing: str = "WAIT_RETEST"
    market_phase: str = "UNKNOWN"
    zone_behaviour: str = "NEUTRAL"
    trade_intelligence: dict = field(default_factory=dict)
    candidate_id: str = field(default_factory=lambda: f"cand_{int(time.time()*1000)}")

    def zone_mid(self) -> float:
        if self.zone_low and self.zone_high:
            return (self.zone_low + self.zone_high) / 2.0
        return self.entry_price

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'side': self.side,
            'entry_price': self.entry_price,
            'zone_low': self.zone_low,
            'zone_high': self.zone_high,
            'zone_state': self.zone_state,
            'entry_distance_atr': round(self.entry_distance_atr, 3),
            'decision': self.decision,
            'decision_label': self.decision_label,
            'decision_reasons': self.decision_reasons,
            'evidence': self.evidence,
            'opportunity_type': self.opportunity_type.value,
            'market_structure': self.market_structure.value,
            'institutional_behaviour': self.institutional_behaviour.value,
            'zone_score': self.zone_metrics.final_zone_score,
            'priority_score': self.priority_score,
            'state': self.state.value,
            'trigger_state': self.zone_metrics.trigger_state,
            'ob_score': self.zone_metrics.order_block_quality,
            'zone_strength': self.zone_metrics.zone_strength,
            'liquidity': self.zone_metrics.liquidity_quality,
            'liquidity_state': self.zone_metrics.liquidity_evidence.get('state', 'LIQUIDITY_UNAVAILABLE'),
            'liquidity_evidence': self.zone_metrics.liquidity_evidence,
            'institutional': self.zone_metrics.institutional_confidence,
            'structure': self.zone_metrics.structure_alignment,
            'timing': self.zone_metrics.entry_timing,
            'trend': self.zone_metrics.trend_alignment,
            'risk': self.zone_metrics.risk_score,
            'ifvg_warning': self.zone_metrics.ifvg_warning,
            'ifvg_penalty': self.zone_metrics.ifvg_penalty,
            'evaluation_count': self.evaluation_count,
            'confirmation_count': self.confirmation_count,
            'gate_status': self.gate_status,
            'last_update': self.last_evaluated,
            'roro_signal': self.roro_signal,
            'roro_reason': self.roro_reason,
            'ob_grade': self.ob_grade,
            'is_a_grade': self.is_a_grade,
            'strong_ob_present': self.strong_ob_present,
            'roro_score': self.roro_score,
            'institutional_score': self.institutional_score,
            'institutional_status': self.institutional_status,
            'institutional_acceleration': self.institutional_acceleration,
            'pre_institutional_state': self.pre_institutional_state,
            'news_risk_level': self.news_risk_level,
            'news_impact_score': self.news_impact_score,
            'news_event_type': self.news_event_type,
            'trade_style': self.trade_style,
            'entry_timing': self.entry_timing,
            'market_phase': self.market_phase,
            'zone_behaviour': self.zone_behaviour,
            'trade_intelligence': self.trade_intelligence,
        }

# Minimum OHLCV depth required by the TradingView evidence layer on the live
# institutional path so EMA200 (>= 200 bars) and the HTF EMA200-slope proxy
# (EMA200 needs a 21-bar window, so >= 210 bars) can actually fire. 260 leaves
# warmup margin. If the exchange cannot supply this depth, the evidence engine
# marks EMA200/HTF as explicitly unavailable (never silently "neutral").
INSTITUTIONAL_OHLCV_DEPTH = 260

class PreInstitutionalState(Enum):
    IDLE = "IDLE"
    WATCH = "WATCH"
    INSTITUTIONAL_WATCH = "INSTITUTIONAL_WATCH"
    MONITORING = "MONITORING"
    BUILDING = "BUILDING"
    CONFIRMED = "CONFIRMED"
    PRE_ENTRY_READY = "PRE_ENTRY_READY"
    WAITING_TRIGGER = "WAITING_TRIGGER"

class InstitutionalRadar:
    def __init__(self):
        self._last_update = {}
        self._lock = threading.RLock()
        self._state_machine = PreInstitutionalStateMachine()

    def update_all(self):
        watchlist = MEMORY.get("watchlist", {})
        now = time.time()
        registry = MEMORY.setdefault("institutional_zone_analysis", {})
        # Remove symbols that disappeared from the discovery watchlist before
        # doing the next analysis pass. This is the rotation/expiry boundary.
        for sym, item in list(registry.items()):
            if sym not in watchlist or float(item.get("expires_at", 0) or 0) < now:
                registry.pop(sym, None)
                if isinstance(watchlist, dict) and isinstance(watchlist.get(sym), dict):
                    watchlist[sym]["institutional_zone_active"] = False
        MEMORY["institutional_zone_analysis"] = registry
        MEMORY["institutional_zone_count"] = len(registry)
        if not watchlist:
            return
        priorities = self._calculate_priorities(watchlist)
        for symbol, entry in watchlist.items():
            interval = self._get_update_interval(symbol, entry, priorities)
            last = self._last_update.get(symbol, 0)
            if now - last < interval:
                continue
            self._update_symbol(symbol, entry)
            self._last_update[symbol] = now

    def _calculate_priorities(self, watchlist):
        priorities = {}
        for symbol, entry in watchlist.items():
            priority = 0
            ob_distance = entry.get("analysis", {}).get("ob_distance", 1.0)
            if ob_distance < 0.003:
                priority += 40
            elif ob_distance < 0.01:
                priority += 20
            news = entry.get("news", {})
            if news.get("risk_level") in ("HIGH_RISK", "CRITICAL"):
                priority += 35
            elif news.get("risk_level") == "MEDIUM_RISK":
                priority += 15
            accel = entry.get("institutional_acceleration", 0)
            if accel > 5:
                priority += min(25, accel * 2)
            state = entry.get("pre_institutional_state", "IDLE")
            state_priority = {
                "PRE_ENTRY_READY": 30,
                "CONFIRMED": 25,
                "BUILDING": 20,
                "MONITORING": 15,
                "WATCH": 10,
                "IDLE": 0
            }
            priority += state_priority.get(state, 0)
            # PRE_EXPANSION promotion: early institutional evidence => priority
            # institutional analysis without waiting for STRONG.
            pre_state = entry.get("pre_expansion_state")
            pre_priorities = {
                "PRE_EXPANSION_LONG": 30,
                "PRE_EXPANSION_SHORT": 30,
                "PRE_EXPANSION_CONFLICT": 15,
            }
            priority += pre_priorities.get(pre_state, 0)
            priorities[symbol] = min(100, priority)
        return priorities

    def _get_update_interval(self, symbol, entry, priorities):
        priority = priorities.get(symbol, 0)
        state = entry.get("pre_institutional_state", "IDLE")
        base_intervals = {
            "PRE_ENTRY_READY": 10,
            "CONFIRMED": 15,
            "BUILDING": 20,
            "MONITORING": 30,
            "WATCH": 45,
            "IDLE": 90
        }
        interval = base_intervals.get(state, 60)
        # PRE_EXPANSION promotion: re-analyze promoted candidates more often so
        # the institutional machine can confirm / invalidate the hypothesis fast.
        pre_state = entry.get("pre_expansion_state")
        if pre_state == "PRE_EXPANSION_LONG" or pre_state == "PRE_EXPANSION_SHORT":
            interval = min(interval, 8)
        elif pre_state == "PRE_EXPANSION_CONFLICT":
            interval = min(interval, 15)
        if priority >= 80:
            interval = min(interval, 10)
        elif priority >= 60:
            interval = min(interval, 20)
        elif priority >= 40:
            interval = min(interval, 30)
        news = entry.get("news", {})
        if news.get("risk_level") == "CRITICAL":
            interval = min(interval, 8)
        elif news.get("risk_level") == "HIGH_RISK":
            interval = min(interval, 15)
        return max(5, interval)

    def _update_symbol(self, symbol, entry):
        df = get_ohlcv_safe(symbol, 100)
        if not is_valid_dataframe(df):
            return
        ob = get_orderbook_cached(symbol, limit=10)
        price = df['close'].iloc[-1]
        atr = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
        score, status, details = InstitutionalIntentEngine.detect(df, ob, symbol)
        entry["institutional"] = {
            "score": score,
            "status": status,
            "last_update": time.time(),
            "details": details
        }
        history = entry.get("institutional_history", [])
        history.append({
            "time": time.time(),
            "score": score,
            "status": status
        })
        if len(history) > 20:
            history = history[-20:]
        entry["institutional_history"] = history
        acceleration = self._calculate_acceleration(history)
        entry["institutional_acceleration"] = acceleration
        old_state = entry.get("pre_institutional_state", "IDLE")
        new_state = self._state_machine.update(symbol, entry, score, acceleration)
        if new_state != old_state:
            entry["pre_institutional_state"] = new_state
            entry["state_change_time"] = time.time()
            log_execution(
                f"[RADAR] {symbol} state: {old_state} → {new_state} (score={score:.1f}, accel={acceleration:.1f})",
                "INFO"
            )
        if not entry.get("institutional_analysis_logged"):
            entry["institutional_analysis_time"] = time.time()
            entry["institutional_analysis_logged"] = True
            log_execution(f"[INSTITUTION] {symbol} analysis started | score={score:.1f} status={status}", "INFO")

        # The TradingView evidence engine needs deeper history (EMA200 / HTF):
        # fetch the full evidence depth for PRE_EXPANSION only, keeping the
        # legacy snapshot for the existing institutional intent engine.
        df_evidence = get_ohlcv_safe(symbol, INSTITUTIONAL_OHLCV_DEPTH)
        if df_evidence is None or not is_valid_dataframe(df_evidence):
            df_evidence = df
        self._evaluate_pre_expansion(symbol, entry, df_evidence, price, atr, ob)
        self._update_a_grade_status(entry)
        self._sync_institutional_zone_registry(symbol, entry)

        # IMPORTANT: PRE_ENTRY_READY is still an institutional decision state,
        # not an execution-queue admission. The queue is downstream of the
        # dynamic Institutional Zone Analysis layer and accepts only explicit
        # A-GRADE/READY candidates. This keeps MEDIUM and early institutional
        # evidence from becoming an accidental entry path.

    _INSTITUTIONAL_PRECURSOR_KEYS = {
        "SWEEP", "DISPLACEMENT", "CHOCH_BOS", "BOS", "SMC_MSS",
        "OB_ZONE", "OB_RETEST", "FVG", "IMBALANCE", "REJECTION",
        "RO_RO_INSTITUTIONAL_FLOW", "SMART_MONEY", "MOMENTUM_ACCELERATION",
        "BOOST", "SHOCK",
    }

    def _sync_institutional_zone_registry(self, symbol, entry):
        """Publish the dynamic Institutional Zone Analysis layer.

        This registry is deliberately separate from both the discovery
        watchlist and the execution queue. MEDIUM + institutional precursor
        evidence is enough to enter this layer; nothing here is an entry order.
        The registry is continuously re-ranked and symbols are removed when the
        precursor thesis disappears, becomes invalid/exhausted, or expires.
        """
        registry = MEMORY.setdefault("institutional_zone_analysis", {})
        now = time.time()
        strength = str(entry.get("strength", "WEAK")).upper()
        evidence = set(entry.get("pre_expansion_evidence", []) or [])
        precursor = sorted(evidence & self._INSTITUTIONAL_PRECURSOR_KEYS)
        rich = entry.get("pre_expansion", {}) or {}
        hypothesis = entry.get("pre_expansion_state")
        phase = str(rich.get("phase", "NEUTRAL")).upper()
        active = (
            bool(entry.get("deep_analyzed"))
            and strength in {"MEDIUM", "STRONG"}
            and len(precursor) >= 1
            and hypothesis in {"PRE_EXPANSION_LONG", "PRE_EXPANSION_SHORT", "PRE_EXPANSION_CONFLICT"}
            and phase not in {"OVEREXTENDED", "EXHAUSTION", "INVALIDATED"}
            and entry.get("state") not in {"EXPIRED", "INVALIDATED", "NEWS_RISK", "ERROR", "DATA_DEGRADED"}
        )

        if active:
            item = registry.setdefault(symbol, {})
            item.update({
                "symbol": symbol,
                "side": entry.get("side", "BUY"),
                "strength": strength,
                "state": "INSTITUTIONAL_WATCH",
                "hypothesis": hypothesis,
                "precursor_evidence": precursor,
                "precursor_count": len(precursor),
                "institutional_score": float((entry.get("institutional") or {}).get("score", entry.get("intent_score", 0)) or 0),
                "composite_score": float(entry.get("analysis", {}).get("composite_score", entry.get("score", 0)) or 0),
                "zone": rich.get("zone"),
                "zone_quality": float(rich.get("zone_quality", 0) or 0),
                "zone_verdict": rich.get("zone_verdict", "NEUTRAL"),
                "phase": phase,
                "indicator_alignment": rich.get("indicator_alignment", "NEUTRAL"),
                "price": float(entry.get("price", 0) or 0),
                "a_grade_ready": bool(entry.get("a_grade_ready", False)),
                "a_grade_reasons": list(entry.get("a_grade_reasons", []) or []),
                "watchlist_entry_time": float(entry.get("watchlist_entry_time", 0) or 0),
                "institutional_analysis_time": float(entry.get("institutional_analysis_time", 0) or 0),
                "last_update": now,
                "expires_at": now + float(os.getenv("INSTITUTIONAL_ZONE_TTL_SEC", "900")),
            })
            entry["institutional_zone_active"] = True
            entry["institutional_zone_last_update"] = now
        else:
            registry.pop(symbol, None)
            entry["institutional_zone_active"] = False

        # Dynamic rotation: no fixed 10/60 list. It is bounded only by the
        # currently valid watchlist opportunities, with an optional operator
        # cap if explicitly configured.
        for sym, item in list(registry.items()):
            if sym not in MEMORY.get("watchlist", {}):
                registry.pop(sym, None)
                continue
            if float(item.get("expires_at", 0) or 0) < now:
                registry.pop(sym, None)
                if sym in MEMORY.get("watchlist", {}):
                    MEMORY["watchlist"][sym]["institutional_zone_active"] = False
                    MEMORY["watchlist"][sym]["institutional_zone_expired_at"] = now
        ranked = sorted(
            registry.items(),
            key=lambda kv: (bool(kv[1].get("a_grade_ready")), int(kv[1].get("precursor_count", 0)),
                            float(kv[1].get("institutional_score", 0)), float(kv[1].get("composite_score", 0))),
            reverse=True,
        )
        cap = int(os.getenv("INSTITUTIONAL_ZONE_MAX", "0") or 0)
        if cap > 0 and len(ranked) > cap:
            for sym, _ in ranked[cap:]:
                registry.pop(sym, None)
                if sym in MEMORY.get("watchlist", {}):
                    MEMORY["watchlist"][sym]["institutional_zone_active"] = False
        MEMORY["institutional_zone_analysis"] = dict(ranked[:cap] if cap > 0 else ranked)
        MEMORY["institutional_zone_count"] = len(MEMORY["institutional_zone_analysis"])
        MEMORY["institutional_zone_updated_at"] = now

    def _update_a_grade_status(self, entry):
        """Derive the pre-queue A-GRADE/READY flag from institutional evidence.

        This is a qualification label only. Execution still requires the queue's
        live causal-zone, trigger, confirmation, Atom and risk gates.
        """
        analysis = entry.get("analysis", {}) or {}
        rich = entry.get("pre_expansion", {}) or {}
        phase = str(rich.get("phase", "NEUTRAL")).upper()
        reasons = []
        ob_grade = str(analysis.get("ob_grade", "NONE")).upper()
        roro = bool(analysis.get("roro_signal", False))
        struct = float(analysis.get("struct_score", 0) or 0)
        liq = float(analysis.get("liq_score", 0) or 0)
        trap = float(analysis.get("trap_risk", 100) or 100)
        alignment = str(rich.get("indicator_alignment", "NEUTRAL")).upper()
        hypothesis = entry.get("pre_expansion_state")
        precursor = set(entry.get("pre_expansion_evidence", []) or [])
        deep = bool(entry.get("deep_analyzed"))
        zone_active = bool(entry.get("institutional_zone_active", False)) or (
            deep and entry.get("strength") in {"MEDIUM", "STRONG"}
            and hypothesis in {"PRE_EXPANSION_LONG", "PRE_EXPANSION_SHORT"}
            and len(precursor & self._INSTITUTIONAL_PRECURSOR_KEYS) >= 1
            and phase not in {"OVEREXTENDED", "EXHAUSTION", "INVALIDATED"}
        )
        if ob_grade in {"A", "A+"}: reasons.append(f"OB_{ob_grade}")
        if roro: reasons.append("INSTITUTIONAL_FLOW")
        if struct >= 70: reasons.append("STRUCTURE")
        if liq >= 70: reasons.append("LIQUIDITY")
        if alignment in {"BULLISH", "BEARISH"} and hypothesis != "PRE_EXPANSION_CONFLICT": reasons.append("INDICATOR_ALIGNMENT")
        precursor_count = len(precursor & self._INSTITUTIONAL_PRECURSOR_KEYS)
        prepared = (
            deep and zone_active and entry.get("strength") in {"MEDIUM", "STRONG"}
            and hypothesis in {"PRE_EXPANSION_LONG", "PRE_EXPANSION_SHORT"}
            and phase not in {"OVEREXTENDED", "EXHAUSTION", "INVALIDATED"}
            and precursor_count >= 2
        )
        ready = (
            prepared
            and ob_grade in {"A", "A+"}
            and roro and struct >= 70 and liq >= 60
            and trap < 70
        )
        entry["institutional_prepared"] = bool(prepared)
        entry["institutional_prepared_reasons"] = sorted(precursor & self._INSTITUTIONAL_PRECURSOR_KEYS)
        entry["a_grade_ready"] = bool(ready)
        entry["a_grade_reasons"] = reasons
        if ready:
            entry["institutional_stage"] = "A-GRADE_READY"
        elif prepared:
            entry["institutional_stage"] = "PREPARED_FOR_ENTRY"
        else:
            entry["institutional_stage"] = "INSTITUTIONAL_WATCH" if zone_active else "WATCH"
        return bool(ready)

    def _compute_tv_indicators(self, df, atr):
        """TradingView-style indicator evidence (VWAP, ADX/DMI, ATR, EMA50/200,
        VWMA, Chandelier, SMC/SFP/MSS, HTF trend, strong candle). Reused as the
        mathematical reference for the PRE_EXPANSION evidence engine. Defensive:
        returns neutral when there is insufficient data (indicators need not all
        flip on the same candle). Signals that require deep history (EMA200,
        HTF trend) are explicitly marked ``available: False`` when the feed is
        too shallow instead of being silently treated as neutral/valid."""
        neutral = {
            "alignment": "NEUTRAL", "phase": "NEUTRAL", "tv": {},
            "data_depth": {"bars": 0, "ema200": False, "htf": False},
        }
        try:
            if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
                return neutral
            close = df["close"]
            n = len(close)
            open_v = float(df["open"].iloc[-1]) if "open" in df.columns else float(close.iloc[-1])
            last = float(close.iloc[-1])
            atr_v = float(atr) if atr is not None and atr > 0 else 0.0
            tv = {}

            # VWAP (only directional with a real reclaim: price beyond VWAP AND
            # VWAP slope confirming the move, so a flat tape stays neutral)
            try:
                vw = vwap_features(df)
                vwap_val = float(vw.get("vwap", 0))
                vw_slope = float(vw.get("slope", 0))
                vw_above = last > vwap_val * (1 + 1e-9) and vw_slope > 0
                vw_below = last < vwap_val * (1 - 1e-9) and vw_slope < 0
                tv["vwap"] = {
                    "value": vwap_val,
                    "distance": float(vw.get("distance", 0)),
                    "slope": vw_slope,
                    "price_above_vwap": True if vw_above else (False if vw_below else None),
                }
            except Exception:
                tv["vwap"] = {"price_above_vwap": None}

            # EMA 50/200 trend stack (EMA200 requires >= 200 bars; explicitly
            # marked unavailable below that so it is never a false "valid").
            ema_info = {}
            ema_info["htf_available"] = n >= 200
            if n >= 50:
                e50 = float(ema(close, 50).iloc[-1])
                ema_info["ema50"] = e50
                ema_info["price_above_50"] = last > e50
            if n >= 200:
                e200 = float(ema(close, 200).iloc[-1])
                ema_info["price_above_200"] = last > e200
                if "ema50" in ema_info:
                    ema_info["bull"] = e50 > e200 and last > e50
                    ema_info["bear"] = e50 < e200 and last < e50
            tv["ema"] = ema_info

            # VWMA / L&L trend stack (tolerance for float noise)
            vwma = {}
            if len(close) >= 20:
                vw = (close * df["volume"]).rolling(20).sum() / (
                    df["volume"].rolling(20).sum() + 1e-9)
                vwma_last = float(vw.iloc[-1])
                tol = abs(vwma_last) * 1e-7 + 1e-9
                vwma["value"] = vwma_last
                vwma["bull"] = last > vwma_last + tol
                vwma["bear"] = last < vwma_last - tol
            tv["vwma"] = vwma

            # ADX / DMI direction (numbers come from the shared compute_adx helper so
            # the evidence layer cannot drift from the rest of the engine).
            dmi = {}
            if n >= 40:
                try:
                    adx_series, pdi_series, mdi_series = compute_adx(df, return_di=True)
                    pdi_l = float(pdi_series.iloc[-1])
                    mdi_l = float(mdi_series.iloc[-1])
                    adx = float(adx_series.iloc[-1])
                    dmi = {"adx": adx, "pdi": pdi_l, "mdi": mdi_l}
                    dmi["bull"] = pdi_l > mdi_l and adx >= 20
                    dmi["bear"] = mdi_l > pdi_l and adx >= 20
                except Exception:
                    dmi = {}
            tv["dmi"] = dmi

            # Chandelier direction (20, ATR*3) — exclusive: only counts when
            # price is beyond its stop AND moving in that direction (flat tape
            # yields neutral because both conditions cannot hold at once).
            chand = {}
            if len(df) >= 20 and atr_v > 0:
                hh = float(df["high"].rolling(20).max().iloc[-1])
                ll = float(df["low"].rolling(20).min().iloc[-1])
                prev_cl = df["close"].shift(1)
                prev_val = float(prev_cl.iloc[-1]) if pd.notna(prev_cl.iloc[-1]) else last
                chand["bull"] = last > (hh - 3.0 * atr_v) and last > prev_val
                chand["bear"] = last < (ll + 3.0 * atr_v) and last < prev_val
            tv["chandelier"] = chand

            # SFP / SMC / MSS structure
            sfp = {"bull": False, "bear": False}
            try:
                shift = detect_structure_shift(df)
                bos_up, bos_down = detect_bos(df)
                sfp["bull"] = shift == "bullish_shift" or bool(bos_up)
                sfp["bear"] = shift == "bearish_shift" or bool(bos_down)
            except Exception:
                pass
            tv["sfp"] = sfp

            # HTF trend proxy (EMA200 slope + price location; needs >= 210 bars so the
            # slope has a 21-bar window on top of the 200-bar EMA. Marked
            # unavailable when the live feed is too shallow — never a false
            # "valid" signal).
            htf = {"bull": False, "bear": False, "available": n >= 210}
            if n >= 210:
                e200s = ema(close, 200)
                slope = float(e200s.iloc[-1] - e200s.iloc[-21])
                htf["bull"] = slope > 0 and last > float(e200s.iloc[-1])
                htf["bear"] = slope < 0 and last < float(e200s.iloc[-1])
            tv["htf"] = htf

            # Body expansion (strong candle) vs ATR + volume
            body_ratio = (abs(last - open_v) / atr_v) if atr_v > 0 else 0.0
            tv["body"] = {
                "body_ratio": body_ratio,
                "bull": body_ratio > 0.5 and last > open_v,
                "bear": body_ratio > 0.5 and last < open_v,
            }

            # Build directional alignment from the indicator set
            bull = tv.get("vwap", {}).get("price_above_vwap") is True
            bear_ = tv.get("vwap", {}).get("price_above_vwap") is False
            b = set()
            s = set()
            if bull: b.add("VWAP")
            if bear_: s.add("VWAP")
            if tv.get("ema", {}).get("bull"): b.add("EMA_TREND")
            if tv.get("ema", {}).get("bear"): s.add("EMA_TREND")
            if tv.get("vwma", {}).get("bull"): b.add("VWMA_TREND")
            if tv.get("vwma", {}).get("bear"): s.add("VWMA_TREND")
            if tv.get("dmi", {}).get("bull"): b.add("ADX_DMI")
            if tv.get("dmi", {}).get("bear"): s.add("ADX_DMI")
            if tv.get("chandelier", {}).get("bull"): b.add("CHANDELIER")
            if tv.get("chandelier", {}).get("bear"): s.add("CHANDELIER")
            if tv.get("sfp", {}).get("bull"): b.add("SMC_MSS")
            if tv.get("sfp", {}).get("bear"): s.add("SMC_MSS")
            if tv.get("htf", {}).get("bull"): b.add("HTF_TREND")
            if tv.get("htf", {}).get("bear"): s.add("HTF_TREND")
            if tv.get("body", {}).get("bull"): b.add("STRONG_CANDLE")
            if tv.get("body", {}).get("bear"): s.add("STRONG_CANDLE")
            if b and not s:
                alignment = "BULLISH"
            elif s and not b:
                alignment = "BEARISH"
            elif b and s:
                alignment = "CONFLICT"
            else:
                alignment = "NEUTRAL"
            return {
                "alignment": alignment,
                "phase": "NEUTRAL",
                "tv": tv,
                "ipa": {"bull": sorted(b), "bear": sorted(s)},
                "price": last,
                "body_ratio": body_ratio,
                "data_depth": {
                    "bars": n,
                    "ema50": n >= 50,
                    "ema200": n >= 200,
                    "htf": n >= 210,
                },
            }
        except Exception:
            return {"alignment": "NEUTRAL", "phase": "NEUTRAL", "tv": {},
                    "ipa": {"bull": [], "bear": []}, "price": None, "body_ratio": 0.0,
                    "data_depth": {"bars": 0, "ema200": False, "htf": False}}

    def _classify_expansion_phase(self, tv, analysis, mom):
        """Classify the expansion phase for an actively-promoted candidate:
        EARLY_EXPANSION / EXPANSION / OVEREXTENDED / EXHAUSTION (else NEUTRAL /
        BUILDING). Entry is only permitted around EARLY_EXPANSION by the
        existing entry authority — never after OVEREXTENDED / EXHAUSTION."""
        vol_state = analysis.get("vol_state", "neutral")
        vol_exp = vol_state == "expansion"
        body_ratio = tv.get("body_ratio", 0.0)
        dmi = tv.get("tv", {}).get("dmi", {})
        adx = float(dmi.get("adx", 0) or 0)
        vw = tv.get("tv", {}).get("vwap", {})
        vw_dist = abs(float(vw.get("distance", 0) or 0))
        tb = bool(mom.get("trend_expansion", False))
        decay = bool(mom.get("momentum_decay", False))
        exh = float(mom.get("exhaustion_risk", 0) or 0)
        struct_ok = float(analysis.get("struct_score", 50) or 50) >= 70

        # Extreme stretch: far beyond VWAP, or an oversized body. ADX is used as
        # trend / DMI confirmation below — not as an overextension detector.
        overextended = vw_dist > 0.15 or body_ratio > 4.0
        exhausted = bool(decay) or exh > 50 or vol_state == "exhaustion"
        if exhausted:
            return "EXHAUSTION", True
        if overextended:
            return "OVEREXTENDED", True
        strong_body = body_ratio > 1.2
        # Confirmed expansion: volume + body expansion + established structure.
        if vol_exp and strong_body and struct_ok:
            return "EXPANSION", False
        # Early expansion: the BEGINNING of the move — volume or body turning
        # over with momentum accelerating, before the structure fully confirms.
        if (vol_exp or strong_body) and (tb or adx >= 15):
            return "EARLY_EXPANSION", False
        return "BUILDING", False

    def _analyze_zone_and_indicators(self, entry, df, price, atr, mom):
        """Zone-first analysis: identify the relevant zone (OB / institution /
        S&R / FVG / imbalance) from the existing analysis artifacts (no parallel
        scanner) and assess zone quality, volume in zone, liquidity, price
        location, and the TradingView indicator evidence. Determines whether the
        zone is accumulating (demand) or distributing (supply)."""
        analysis = entry.get("analysis", {}) or {}
        res = {
            "zone": None, "zone_quality": 0.0, "volume_in_zone": 0.0,
            "liquidity_around_zone": 0.0, "price_location": "NEUTRAL",
            "zone_verdict": "NEUTRAL", "phase": "NEUTRAL", "fresh": False,
        }
        try:
            if df is None or not isinstance(df, pd.DataFrame) or len(df) < 2:
                return res
            res["fresh"] = True

            # Zone identification (reuse existing analysis, not a new scanner)
            ob_grade = str(analysis.get("ob_grade", "NONE"))
            zones = analysis.get("zones")
            proximity = analysis.get("proximity", analysis.get("ob_distance", 1.0))
            if ob_grade in ("A+", "A"):
                res["zone"] = "OB"
                res["zone_quality"] = 1.0 if ob_grade == "A+" else 0.85
            elif ob_grade == "B":
                res["zone"] = "OB"
                res["zone_quality"] = 0.6
            elif zones:
                res["zone"] = "INSTITUTIONAL_ZONE"
                res["zone_quality"] = 0.5
            elif proximity is not None and float(proximity) < 0.01:
                res["zone"] = "S_R"
                res["zone_quality"] = 0.5

            # Volume / liquidity / price location around the zone
            vol_state = analysis.get("vol_state", "neutral")
            liq_score = float(analysis.get("liq_score", 50) or 50)
            res["volume_in_zone"] = (
                1.0 if vol_state == "expansion"
                else 0.5 if vol_state == "normal"
                else 0.0)
            res["liquidity_around_zone"] = min(1.0, liq_score / 100.0)
            res["price_location"] = analysis.get("price_location", "MID")

            tv_res = self._compute_tv_indicators(df, atr)
            res["tv"] = tv_res.get("tv", {})
            res["ipa"] = tv_res.get("ipa", {"bull": [], "bear": []})
            res["alignment"] = tv_res.get("alignment", "NEUTRAL")
            res["body_ratio"] = tv_res.get("body_ratio", 0.0)

            # Zone verdict: accumulation (demand) vs distribution (supply) using
            # zone credibility + liquidity + volume + price response, NOT plain
            # candle volume alone.
            res["phase"], _over = self._classify_expansion_phase(tv_res, analysis, mom)
            res["data_depth"] = tv_res.get("data_depth", {})
            return res
        except Exception:
            return res

    def _zone_verdict(self, zone_res, bull, bear):
        """Combine zone quality, liquidity, volume-pressure and directional
        evidence into ACCUMULATION / DISTRIBUTION / NEUTRAL. Volume is only
        volume-pressure evidence; structure/liquidity/price-response decide."""
        zq = zone_res.get("zone_quality", 0.0)
        liq = zone_res.get("liquidity_around_zone", 0.0)
        volz = zone_res.get("volume_in_zone", 0.0)
        net = len(bull) - len(bear)
        credible = zq >= 0.5 and liq >= 0.5
        if net > 0 and credible:
            return "ACCUMULATION"
        if net < 0 and credible:
            return "DISTRIBUTION"
        return "NEUTRAL"

    def _evaluate_pre_expansion(self, symbol, entry, df, price, atr, ob):
        """Evidence-driven PRE_EXPANSION promotion for early institutional
        candidates (TradingView / PRE-EXPANSION evidence engine).

        When a Watchlist asset first appears as MEDIUM with meaningful
        institutional evidence (OB/Zone, MSS/BOS/CHoCH, FVG, imbalance,
        displacement, rejection, shock/boost), it is promoted IMMEDIATELY to
        PRIORITY INSTITUTIONAL ZONE ANALYSIS through the existing radar, WITHOUT
        waiting for STRONG or the major expansion candle. MEDIUM is the trigger
        to START watching the setup, never an entry. Promotion only marks the
        candidate for faster institutional re-analysis, records zone-first +
        indicator evidence, classifies the expansion phase (EARLY_EXPANSION /
        EXPANSION / OVEREXTENDED / EXHAUSTION) and preserves a directional
        hypothesis (PRE_EXPANSION_LONG / PRE_EXPANSION_SHORT /
        PRE_EXPANSION_CONFLICT). The existing institutional entry authority
        remains the sole decision maker for actual execution.
        """
        analysis = entry.get("analysis", {}) or {}
        reasons = entry.get("reasons", []) or []
        side = str(entry.get("side", "BUY")).upper()
        strength = entry.get("strength", "WEAK")
        state_name = entry.get("state", "")

        # Momentum flow (used for shock/boost AND expansion phase).
        mom = {}
        try:
            mom = MomentumFlowEngine.analyze_momentum_flow(df)
        except Exception:
            mom = {}

        # ---- Zone-first + TradingView indicator evidence --------------------
        zone_res = self._analyze_zone_and_indicators(entry, df, price, atr, mom)
        tv_bull = zone_res.get("ipa", {}).get("bull", [])
        tv_bear = zone_res.get("ipa", {}).get("bear", [])

        # ---- Gather directional institutional evidence ----------------------
        bull = set()
        bear = set()

        # Indicators (TradingView evidence engine)
        bull.update(tv_bull)
        bear.update(tv_bear)

        # Displacement (directional)
        disp = analysis.get("displacement", False)
        if disp and side == "BUY":
            bull.add("DISPLACEMENT")
        if disp and side == "SELL":
            bear.add("DISPLACEMENT")

        # Rejection (directional pinbar)
        rej = analysis.get("rejection", False)
        if rej and side == "BUY":
            bull.add("REJECTION")
        if rej and side == "SELL":
            bear.add("REJECTION")

        # BOS / structure shift (up = LONG evidence, down = SHORT evidence)
        bos_up, bos_down = False, False
        try:
            bos_up, bos_down = detect_bos(df)
        except Exception:
            pass
        struct_score = analysis.get("struct_score", 50)
        if bos_up or (struct_score >= 70 and side == "BUY"):
            bull.add("BOS")
        if bos_down or (struct_score >= 70 and side == "SELL"):
            bear.add("BOS")

        # FVG / imbalance (directional, from analysis or narrative)
        fvg = bool(analysis.get("fvg", False)) or "FVG" in reasons
        if fvg:
            if side == "BUY":
                bull.add("FVG")
            else:
                bear.add("FVG")
        imbalance = bool(analysis.get("imbalance", False)) or "Imbalance" in reasons
        if imbalance:
            if side == "BUY":
                bull.add("IMBALANCE")
            else:
                bear.add("IMBALANCE")
        # MSB / MSS narrative
        if any(k in reasons for k in ("MSB", "MSS")):
            if side == "BUY":
                bull.add("MSB_MSS")
            else:
                bear.add("MSB_MSS")

        # Liquidity sweep evidence (liq_score high => sweep taken on entry side)
        liq_score = analysis.get("liq_score", 50)
        if liq_score >= 70 and side == "BUY":
            bull.add("SWEEP")
        if liq_score >= 70 and side == "SELL":
            bear.add("SWEEP")

        # OB / zone quality + retest (zone/quality prioritized once promoted)
        ob_grade = analysis.get("ob_grade", "NONE")
        if ob_grade in ("A+", "A") and zone_res.get("zone_quality", 0) >= 0.5:
            if side == "BUY":
                bull.add("OB_ZONE")
            else:
                bear.add("OB_ZONE")

        # Volume expansion (supporting, direction-agnostic -> adds to entry side)
        vol_state = analysis.get("vol_state", "neutral")
        vol_shock = vol_state == "expansion"

        # RORO / institutional flow signal (directional)
        roro = analysis.get("roro_signal", False)
        if roro:
            if side == "BUY":
                bull.add("RO_RO_INSTITUTIONAL_FLOW")
            else:
                bear.add("RO_RO_INSTITUTIONAL_FLOW")

        # Smart money bias / momentum flow
        smb = str(entry.get("smart_money_bias", "NEUTRAL")).upper()
        if smb in ("LONG", "BULLISH"):
            bull.add("SMART_MONEY")
        elif smb in ("SHORT", "BEARISH"):
            bear.add("SMART_MONEY")

        mom_exp = bool(mom.get("trend_expansion", False))
        continuation = float(mom.get("continuation_strength", 0) or 0)
        exhaustion = float(mom.get("exhaustion_risk", 0) or 0)

        # Shock / Boost: strong displacement body + volume expansion + momentum.
        shock_boost = False
        flow_bias = str(mom.get("flow_bias", "NEUTRAL")).upper()
        shock_boost = bool(mom.get("trend_expansion", False)) and vol_shock
        if flow_bias == "BUY":
            bull.add("MOMENTUM_ACCELERATION")
        elif flow_bias == "SELL":
            bear.add("MOMENTUM_ACCELERATION")
        if mom_exp:
            if side == "BUY":
                bull.add("BOOST")
            else:
                bear.add("BOOST")
        if shock_boost and side == "BUY":
            bull.add("SHOCK")
        if shock_boost and side == "SELL":
            bear.add("SHOCK")

        # Narrative reasons (already stored in watchlist entry)
        if "Sweep" in reasons and side == "BUY":
            bull.add("SWEEP")
        if "Sweep" in reasons and side == "SELL":
            bear.add("SWEEP")
        if "CHoCH/BOS" in reasons:
            if side == "BUY":
                bull.add("CHOCH_BOS")
            else:
                bear.add("CHOCH_BOS")
        if "ZONE_RETEST" in reasons and side == "BUY":
            bull.add("OB_RETEST")
        if "ZONE_RETEST" in reasons and side == "SELL":
            bear.add("OB_RETEST")
        if "Displacement" in reasons and side == "BUY":
            bull.add("DISPLACEMENT")
        if "Displacement" in reasons and side == "SELL":
            bear.add("DISPLACEMENT")

        # ---- Zone verdict: accumulation vs distribution ---------------------
        zone_verdict = self._zone_verdict(zone_res, bull, bear)
        zone_res["zone_verdict"] = zone_verdict

        # ---- Directional hypothesis --------------------------------------
        confidence = min(100.0, 20.0 + len(bull) * 12 + len(bear) * 12 + (10 if vol_shock else 0))
        if bull and not bear:
            hypothesis = "PRE_EXPANSION_LONG"
        elif bear and not bull:
            hypothesis = "PRE_EXPANSION_SHORT"
        elif bull and bear:
            hypothesis = "PRE_EXPANSION_CONFLICT"
        else:
            hypothesis = None
        evidence_reasons = sorted(bull | bear)
        ev_count = len(bull) + len(bear)
        precursor_evidence = sorted((bull | bear) & self._INSTITUTIONAL_PRECURSOR_KEYS)
        precursor_count = len(precursor_evidence)

        # ---- Invalidation signal -----------------------------------------
        invalidated = False
        invalid_reason = None
        trap_risk = analysis.get("trap_risk", 0)
        # Invalidation uses the FRESH momentum/exhaustion computed this tick,
        # never the values frozen at watchlist-entry creation.
        momentum_decay = bool(mom.get("momentum_decay", False))
        if trap_risk > 70:
            invalidated = True
            invalid_reason = "TRAP_RISK"
        elif exhaustion > 50:
            invalidated = True
            invalid_reason = "MOMENTUM_COLLAPSE"
        elif momentum_decay and ev_count == 0:
            invalidated = True
            invalid_reason = "MOMENTUM_COLLAPSE"

        prev = entry.get("pre_expansion_state")
        now = time.time()

        # ---- Apply state transitions --------------------------------------
        if invalidated and prev:
            entry.pop("pre_expansion_state", None)
            if "pre_expansion" in entry:
                entry["pre_expansion"]["phase"] = "INVALIDATED"
            entry["pre_expansion_invalidated_time"] = now
            log_execution(
                f"[PRE_EXPANSION] {symbol} demoted: reason={invalid_reason}",
                "WARN",
            )
            return

        promoted = False
        if hypothesis and strength in ("MEDIUM", "STRONG"):
            # Promote when there is meaningful early institutional evidence,
            # regardless of MEDIUM/STRONG. MEDIUM is the classic early-warning
            # trigger this feature exists to surface; STRONG passes through
            # unchanged (it already enters via the existing authority).
            if precursor_count >= 1 and (ev_count >= 2 or (vol_shock and ev_count >= 1)):
                if prev != hypothesis:
                    entry["pre_expansion_state"] = hypothesis
                    entry["pre_expansion_evidence"] = evidence_reasons
                    entry["institutional_precursor_evidence"] = precursor_evidence
                    entry["pre_expansion_time"] = now
                    entry["pre_expansion_confidence"] = round(confidence, 1)
                    log_execution(
                        f"[PRE_EXPANSION] {symbol} promoted from WATCHLIST "
                        f"reason={'+'.join(evidence_reasons)} state={strength} "
                        f"bias={'LONG' if hypothesis.endswith('LONG') else 'SHORT' if hypothesis.endswith('SHORT') else 'CONFLICT'} "
                        f"zone={zone_res.get('zone') or 'NONE'} "
                        f"verdict={zone_verdict} "
                        f"action=PRIORITY_INSTITUTIONAL_ZONE_ANALYSIS",
                        "SUCCESS",
                    )
                promoted = True

        if not promoted:
            # No promotion reached: if a previous PRE_EXPANSION no longer holds
            # and evidence dropped, demote back to normal watchlist monitoring.
            if prev and not invalidated:
                if hypothesis is None or ev_count < 1:
                    entry.pop("pre_expansion_state", None)
                    log_execution(
                        f"[PRE_EXPANSION] {symbol} no longer priority (evidence dropped)",
                        "INFO",
                    )
                    return

        # ---- Persist rich zone + indicator + phase snapshot ---------------
        if zone_res.get("fresh"):
            rich = entry.setdefault("pre_expansion", {})
            phase = zone_res.get("phase", "NEUTRAL")
            # Timestamps: pre_expansion < early_expansion < strong/expansion.
            if phase == "EARLY_EXPANSION" and not rich.get("early_expansion_time"):
                rich["early_expansion_time"] = now
            if phase == "EXPANSION" and not rich.get("expansion_time"):
                rich["expansion_time"] = now
            rich["phase"] = phase
            rich["zone"] = zone_res.get("zone")
            rich["zone_quality"] = zone_res.get("zone_quality", 0.0)
            rich["volume_in_zone"] = zone_res.get("volume_in_zone", 0.0)
            rich["liquidity_around_zone"] = zone_res.get("liquidity_around_zone", 0.0)
            rich["price_location"] = zone_res.get("price_location", "MID")
            rich["zone_verdict"] = zone_verdict
            rich["indicator_alignment"] = zone_res.get("alignment", "NEUTRAL")
            rich["body_ratio"] = zone_res.get("body_ratio", 0.0)
            rich["tv"] = zone_res.get("tv", {})
            rich["data_depth"] = zone_res.get("data_depth", {})
            rich["hypothesis"] = hypothesis
            rich["updated_at"] = now

            # Indicator transition memory (remember previous states).
            hist = entry.setdefault("indicator_history", [])
            hist.append({
                "time": now,
                "phase": phase,
                "alignment": zone_res.get("alignment", "NEUTRAL"),
                "zone": zone_res.get("zone"),
            })
            if len(hist) > 20:
                entry["indicator_history"] = hist[-20:]

    def _calculate_acceleration(self, history):
        if len(history) < 3:
            return 0.0
        scores = [h["score"] for h in history[-3:]]
        delta1 = scores[1] - scores[0]
        delta2 = scores[2] - scores[1]
        acceleration = delta2 - delta1
        return max(-100, min(100, acceleration * 2))

    def _promote_to_queue(self, symbol, entry):
        # Legacy compatibility hook. Admission is now hard-gated by the
        # dynamic Institutional Zone Analysis layer and explicit A-GRADE.
        if not entry.get("institutional_zone_active") or not entry.get("a_grade_ready"):
            return False
        if symbol in queue._candidates:
            return False
        inst = entry.get("institutional", {})
        price = entry.get("price", 0)
        atr = entry.get("atr", 0.01)
        cand = ExecutionCandidate(
            symbol=symbol,
            side=entry.get("side", "BUY"),
            price=price,
            entry_price=price,
            stop_loss=entry.get("sl", 0),
            take_profit_1=entry.get("tp1", 0),
            take_profit_2=entry.get("tp2", 0),
            atr=atr,
            df=None,
            ob=None,
            original_score=inst.get("score", 0),
            original_reason="Institutional radar pre-entry",
            signal_type="institutional_radar",
            ob_cfg=AssetBehaviorProfile.ob_config(AssetBehaviorProfile.resolve_asset_class(symbol))
        )
        cand.institutional_score = inst.get("score", 0)
        cand.institutional_status = inst.get("status", "NEUTRAL")
        cand.institutional_layers = inst.get("details", {})
        cand.institutional_acceleration = entry.get("institutional_acceleration", 0)
        cand.pre_institutional_state = entry.get("pre_institutional_state", "PRE_ENTRY_READY")
        news = entry.get("news", {})
        cand.news_risk_level = news.get("risk_level", "NEUTRAL")
        cand.news_impact_score = news.get("impact_score", 0)
        cand.news_event_type = news.get("event_type", "")
        cand.watchlist_entry_time = entry.get("watchlist_entry_time", 0)
        cand.institutional_analysis_time = entry.get("institutional_analysis_time", 0)
        cand.priority_score = cand.institutional_score + cand.institutional_acceleration * 0.5
        ti = entry.get("trade_intelligence") or {}
        cand.trade_style = ti.get("trade_style", entry.get("trade_style", "SCALP"))
        cand.entry_timing = ti.get("timing", entry.get("entry_timing", "WAIT_RETEST"))
        cand.market_phase = ti.get("phase", entry.get("market_phase", "UNKNOWN"))
        cand.zone_behaviour = ti.get("behaviour", entry.get("zone_behaviour", "NEUTRAL"))
        cand.trade_intelligence = ti
        queue.add_candidate(cand)
        log_execution(
            f"[RADAR] {symbol} promoted to Queue (A-GRADE, score={cand.institutional_score:.1f}, accel={cand.institutional_acceleration:.1f})",
            "SUCCESS"
        )
        return True

class PreInstitutionalStateMachine:
    def __init__(self):
        self.states = {}

    def update(self, symbol, entry, score, acceleration):
        current = entry.get("pre_institutional_state", "IDLE")
        news = entry.get("news", {})
        if news.get("risk_level") in ("HIGH_RISK", "CRITICAL"):
            thresholds = {
                "IDLE": 35,
                "WATCH": 50,
                "MONITORING": 60,
                "BUILDING": 68,
                "CONFIRMED": 72
            }
        else:
            thresholds = {
                "IDLE": 45,
                "WATCH": 55,
                "MONITORING": 70,
                "BUILDING": 75,
                "CONFIRMED": 75
            }
        if current == "IDLE":
            if score >= thresholds["IDLE"]:
                return "WATCH"
            return "IDLE"
        elif current == "WATCH":
            if score >= thresholds["WATCH"] and acceleration > 0:
                return "MONITORING"
            elif score < 40:
                return "IDLE"
            return "WATCH"
        elif current == "MONITORING":
            if score >= thresholds["MONITORING"] and acceleration > 2:
                return "BUILDING"
            elif score < 50:
                return "WATCH"
            return "MONITORING"
        elif current == "BUILDING":
            if score >= thresholds["BUILDING"] and acceleration > 0:
                return "CONFIRMED"
            elif score < 65:
                return "MONITORING"
            return "BUILDING"
        elif current == "CONFIRMED":
            ob_distance = entry.get("analysis", {}).get("ob_distance", 1.0)
            if score >= thresholds["CONFIRMED"] and ob_distance < 0.005 and acceleration >= 0:
                return "PRE_ENTRY_READY"
            elif score < 70:
                return "BUILDING"
            return "CONFIRMED"
        elif current == "PRE_ENTRY_READY":
            return "PRE_ENTRY_READY"
        return current

class NewsRiskIntelligence:
    EVENT_IMPACT = {
        "FOMC": 90, "CPI": 85, "NFP": 80,
        "ETF_DECISION": 85, "EARNINGS": 70,
        "TOKEN_UNLOCK": 65, "LISTING": 60,
        "REGULATORY": 75, "MACRO": 70
    }
    RELIABLE_SOURCES = {
        "federalreserve.gov": 90,
        "bloomberg.com": 80,
        "reuters.com": 80,
        "coindesk.com": 70
    }
    def __init__(self):
        self._processed_news = {}
        self._radar = None
        self._lock = threading.RLock()

    def process_news_feed(self, news_items):
        for item in news_items:
            news_id = self._generate_id(item)
            if news_id in self._processed_news:
                continue
            self._processed_news[news_id] = time.time()
            classification = self._classify_news(item)
            for symbol in self._get_affected_symbols(item):
                self._update_asset_priority(symbol, classification)

    def _classify_news(self, item):
        title = item.get("title", "")
        description = item.get("description", "")
        source = item.get("source", "")
        event_type = self._detect_event_type(title, description)
        impact = self.EVENT_IMPACT.get(event_type, 30)
        source_quality = self.RELIABLE_SOURCES.get(source, 40)
        freshness = self._calculate_freshness(item)
        event_time = item.get("event_time")
        proximity = self._calculate_proximity(event_time) if event_time else 50
        risk_score = (
            impact * 0.35 +
            source_quality * 0.20 +
            freshness * 0.25 +
            proximity * 0.20
        )
        if risk_score >= 80:
            risk_level = "CRITICAL"
        elif risk_score >= 65:
            risk_level = "HIGH_RISK"
        elif risk_score >= 45:
            risk_level = "MEDIUM_RISK"
        else:
            risk_level = "LOW_RISK"
        return {
            "risk_level": risk_level,
            "risk_score": risk_score,
            "event_type": event_type,
            "source_quality": source_quality,
            "freshness": freshness,
            "event_time": event_time,
            "expires_at": time.time() + self._calculate_ttl(event_type)
        }

    def _update_asset_priority(self, symbol, classification):
        with self._lock:
            if symbol not in MEMORY.get("watchlist", {}):
                return
            entry = MEMORY["watchlist"][symbol]
            entry["news"] = {
                "risk_level": classification["risk_level"],
                "impact_score": classification["risk_score"],
                "event_type": classification["event_type"],
                "freshness": classification["freshness"],
                "expires_at": classification["expires_at"],
                "last_update": time.time()
            }
            if classification["risk_level"] in ("HIGH_RISK", "CRITICAL"):
                entry["analysis_priority"] = "INSTITUTIONAL_PRIORITY"
                entry["analysis_interval"] = 10 if classification["risk_level"] == "CRITICAL" else 15
                log_execution(
                    f"[NEWS_PRIORITY] {symbol} {classification['risk_level']} → URGENT "
                    f"institutional analysis (event: {classification['event_type']})",
                    "INFO"
                )
                if self._radar:
                    self._radar._update_symbol(symbol, entry)
                    log_execution(
                        f"[INSTITUTIONAL_PREP] {symbol} analyzed ahead of event",
                        "INFO"
                    )

    def _detect_event_type(self, title, description):
        text = (title + " " + description).lower()
        keywords = {
            "FOMC": ["fomc", "federal reserve", "fed rate"],
            "CPI": ["cpi", "consumer price", "inflation"],
            "NFP": ["nonfarm", "jobs report", "employment"],
            "ETF_DECISION": ["etf", "exchange-traded fund"],
            "EARNINGS": ["earnings", "quarterly results"],
            "TOKEN_UNLOCK": ["unlock", "vesting", "token release"],
            "REGULATORY": ["regulatory", "sec", "regulation"],
            "MACRO": ["gdp", "economic growth", "pmi"]
        }
        for event_type, terms in keywords.items():
            if any(term in text for term in terms):
                return event_type
        return "GENERAL_NEWS"

    def _calculate_proximity(self, event_time):
        now = time.time()
        if event_time <= now:
            return 100
        time_until = event_time - now
        if time_until < 3600:
            return 95
        elif time_until < 7200:
            return 80
        elif time_until < 21600:
            return 60
        return 30

    def _calculate_ttl(self, event_type):
        high_impact = ["FOMC", "CPI", "NFP", "ETF_DECISION", "REGULATORY"]
        if event_type in high_impact:
            return 7200
        return 3600

    def _generate_id(self, item):
        title = item.get("title", "")
        source = item.get("source", "")
        timestamp = item.get("timestamp", time.time())
        return f"{source}_{title[:30]}_{int(timestamp)}"

    def _get_affected_symbols(self, item):
        symbols = []
        for sym in MEMORY.get("watchlist", {}).keys():
            if sym.split('/')[0].upper() in item.get("title", "").upper():
                symbols.append(sym)
        return symbols[:3]

    def _calculate_freshness(self, item):
        timestamp = item.get("timestamp", time.time())
        age = time.time() - timestamp
        if age < 60:
            return 100
        elif age < 300:
            return 80
        elif age < 1800:
            return 50
        else:
            return 20

class ExecutionQueue:
    def __init__(self, max_size: int = 15, re_eval_interval: float = 5.0):
        self._candidates: Dict[str, ExecutionCandidate] = {}
        self._max_size = max_size
        self._re_eval_interval = re_eval_interval
        self._lock = threading.RLock()
        self.total_evaluations = 0
        self.total_rejected = 0
        self.total_executed = 0
        self._last_promote = 0
        self._last_evidence = {}
        self.gate_stats = {
            "insufficient_data": 0,
            "extended": 0,
            "ob_broken": 0,
            "zone_stale": 0,
            "zone_invalidated": 0,
            "score_too_low": 0,
            "expired": 0,
            "evicted_capacity": 0,
            "ready": 0,
            "a_grade_ready": 0,
        }

    def add_candidate(self, candidate: ExecutionCandidate) -> bool:
        with self._lock:
            if candidate.zone_low > 0 and candidate.zone_high > 0:
                stale_refs = MEMORY.get("stale_zone_refs", {})
                key = f"{candidate.symbol}:{candidate.side}"
                if key in stale_refs:
                    ref = stale_refs[key]
                    zl = ref.get("zone_low", 0)
                    zh = ref.get("zone_high", 0)
                    tol = 0.005 * max(abs(zl), abs(zh), 1e-9)
                    if abs(zl - candidate.zone_low) < tol and abs(zh - candidate.zone_high) < tol:
                        log_execution(f"[QUEUE] Same zone detected for {candidate.symbol} (stale), rejecting", "WARN")
                        return False
            if len(self._candidates) >= self._max_size:
                lowest = min(
                    [(s, c) for s, c in self._candidates.items() if c.state != ExecutionState.READY],
                    key=lambda x: x[1].priority_score,
                    default=None
                )
                if lowest:
                    self._candidates.pop(lowest[0])
                    self.total_rejected += 1
                else:
                    return False
            if candidate.symbol in self._candidates:
                existing = self._candidates[candidate.symbol]
                if candidate.zone_metrics.final_zone_score > existing.zone_metrics.final_zone_score:
                    self._candidates[candidate.symbol] = candidate
                    return True
                return False
            self._candidates[candidate.symbol] = candidate
            return True

    def re_evaluate_all(self, data_fetcher):
        if not self._candidates:
            return
        with self._lock:
            for symbol, cand in list(self._candidates.items()):
                if (cand.institutional_score >= 70 and
                    cand.pre_institutional_state in ("CONFIRMED", "PRE_ENTRY_READY")):
                    df = data_fetcher(symbol)
                    if df is not None and self._check_entry_conditions(df, cand.side, cand.atr):
                        cand.state = ExecutionState.READY
                        log_execution(f"[QUEUE] {symbol} confirmed READY with Institutional Context", "SUCCESS")
                    else:
                        cand.state = ExecutionState.WAITING_TRIGGER
                    continue
                df = data_fetcher(symbol)
                if not is_valid_dataframe(df, required_cols=['timestamp','open','high','low','close','volume']) or len(df) < 30:
                    self.gate_stats["insufficient_data"] += 1
                    record_gate_event(symbol, "QUEUE", "INSUFFICIENT_DATA",
                                      "re-evaluation fetch returned no usable frame", cand.side)
                    self._invalidate(symbol, "Insufficient data")
                    continue
                current_price = df['close'].iloc[-1]
                atr = compute_atr(df).iloc[-1] if len(df) > 14 else current_price * 0.01
                cand.atr = atr
                if cand.zone_low == 0.0 and cand.zone_high == 0.0:
                    zl, zh, zbar, ztouch = self._find_causal_ob_zone(df, cand.side, atr, cand.ob_cfg)
                    if zl and zh:
                        cand.zone_low, cand.zone_high = zl, zh
                        cand.zone_origin_bar = zbar
                        cand.zone_touches = ztouch
                        if cand.zone_created_at == 0.0:
                            cand.zone_created_at = time.time()
                        anchor = zl if cand.side == "BUY" else zh
                        if anchor > 0:
                            cand.entry_price = anchor
                    elif cand.entry_price > 0:
                        cand.zone_low = cand.entry_price - atr * 0.5
                        cand.zone_high = cand.entry_price + atr * 0.5
                        if cand.zone_created_at == 0.0:
                            cand.zone_created_at = time.time()
                anchor_price = cand.zone_mid()
                cand.entry_distance_atr = (abs(current_price - anchor_price) / atr) if atr > 0 else 0.0
                if self._update_zone_lifecycle(cand, current_price, atr):
                    if cand.state in (ExecutionState.RETURNED_WATCHLIST, ExecutionState.INVALIDATED):
                        continue
                if self._is_extended(cand, current_price):
                    self.gate_stats["extended"] += 1
                    record_gate_event(symbol, "QUEUE", "PRICE_EXTENDED",
                                      f"{cand.entry_distance_atr:.2f} ATR from zone mid", cand.side)
                    self._return_to_watchlist(symbol, "Price extended")
                    continue
                if self._is_order_block_broken(cand, current_price):
                    self.gate_stats["ob_broken"] += 1
                    record_gate_event(symbol, "QUEUE", "ORDER_BLOCK_BROKEN",
                                      "close through OB far edge", cand.side)
                    self._invalidate(symbol, "Order block broken")
                    continue
                ob_score, ob_type = self._evaluate_order_block(df, cand.side, atr, cand.ob_cfg)
                zone_score = self._evaluate_zone_strength(df, cand.side, atr, cand.entry_price)
                liq_score, liq_evidence = self._evaluate_liquidity(df, cand.side, atr)
                inst_score = self._evaluate_institutional(df, cand.side)
                struct_score, struct_type = self._evaluate_structure(df, cand.side)
                timing_score = self._evaluate_timing(df, cand.side, atr, current_price, cand.entry_price)
                trend_score = self._evaluate_trend_alignment(df, cand.side)
                risk_score = self._evaluate_risk(cand, current_price)
                trigger_state = self._detect_trigger_state(df, cand.side, atr, cand.entry_price)
                cand.evidence = dict(self._last_evidence)
                confirm_triggers = ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED")
                if trigger_state in confirm_triggers:
                    marker = self._confirm_marker(trigger_state, cand.evidence, len(df), current_price, atr)
                    if marker != cand.last_confirm_marker:
                        cand.confirmation_count += 1
                        cand.last_confirm_marker = marker
                else:
                    cand.confirmation_count = 0
                    cand.last_confirm_marker = ""
                metrics = ZoneMetrics(
                    order_block_quality=ob_score,
                    zone_strength=zone_score,
                    liquidity_quality=liq_score,
                    liquidity_evidence=liq_evidence,
                    institutional_confidence=inst_score,
                    structure_alignment=struct_score,
                    entry_timing=timing_score,
                    trend_alignment=trend_score,
                    risk_score=risk_score,
                    trigger_state=trigger_state
                )
                ob_analysis_now = getattr(self, "_last_ob_analysis", {}) or {}
                metrics.confluence_bonus = min(
                    5.0,
                    float(ob_analysis_now.get("bonus", 0.0)) * 0.25
                    + (1.0 if struct_type in (MarketStructure.BOS, MarketStructure.MSS) else 0.0)
                )
                # ===== ULTIMATE SNIPER CONFLUENCE (advisory, enrichment only) =====
                # Computed over closed candles only. It adds a small, bounded
                # boon/penalty on top of the existing causal-OB confluence bonus
                # and can NEVER gate an entry on its own, nor flip a weak setup
                # into a strong one, nor bypass Entry Quality / Risk / Execution.
                sniper_shift = 0.0
                sniper_conf = None
                if SNIPER_ENRICHMENT_AVAILABLE and SniperEnrichmentEngine is not None:
                    try:
                        _s_eng = SniperEnrichmentEngine(
                            SniperEnrichmentConfig.from_dict(cand.ob_cfg)
                        )
                        sniper_conf = _s_eng.analyze(df, symbol, side=cand.side)
                        # Scale width: 0.25 keeps it modest vs the OB confluence.
                        sniper_shift = float(sniper_conf.score_shift) * 0.25
                        # Never let the sniper layer alone push a weak zone to
                        # strong: keep the combined bonus at the same ceiling and
                        # only nudge it within that bound.
                        metrics.confluence_bonus = max(
                            0.0,
                            min(5.0, metrics.confluence_bonus + sniper_shift)
                        )
                    except Exception as _sniper_eval_err:  # pragma: no cover - defensive
                        if not getattr(self, "_sniper_warned", False):
                            self._sniper_warned = True
                            log_execution(
                                f"[SNIPER] eval failed for {symbol}: {_sniper_eval_err}", "ERROR"
                            )
                # Persist detailed evidence so callers can see WHY it nudged.
                try:
                    if sniper_conf is not None:
                        cand.evidence["sniper"] = sniper_conf.to_dict()
                        cand.evidence["sniper_shift"] = round(sniper_shift, 3)
                except Exception:
                    pass
                opp_type = self._classify_opportunity(metrics, struct_type, cand.side, df)
                behaviour = self._detect_institutional_behaviour(df, cand.side)
                cand.zone_metrics = metrics
                cand.opportunity_type = opp_type
                cand.market_structure = struct_type
                cand.institutional_behaviour = behaviour
                cand.last_evaluated = time.time()
                cand.evaluation_count += 1
                cand.priority_score = metrics.final_zone_score
                roro_ok, roro_class, roro_reason = check_institutional_entry(
                    symbol, cand.side, df, cand.ob, atr, current_price
                )
                cand.roro_signal = roro_ok
                cand.roro_reason = roro_reason if roro_ok else "Roro failed"
                cand.strong_ob_present = False
                cand.ob_grade = "NONE"
                cand.is_a_grade = False
                cand.roro_score = 0.0
                if roro_ok:
                    intent_info = MEMORY.get(f"intent_{symbol}", {})
                    cand.roro_score = intent_info.get("score", 75)
                    ob_result = self._select_strong_ob(df, cand.side, atr, cand.ob_cfg)
                    cand.ob_grade = ob_result.get('grade', 'INVALID')
                    cand.strong_ob_present = ob_result['grade'] in ('A+', 'A')
                    if cand.strong_ob_present:
                        cand.is_a_grade = True
                        cand.decision = "A-GRADE_RORO_OB"
                        cand.decision_reasons = [
                            f"Roro: {roro_reason[:50]}",
                            f"OB: {ob_result['grade']} score={ob_result['score']:.1f}",
                            f"disp={ob_result['displacement_atr']:.2f}ATR",
                            f"fresh={ob_result['freshness']}"
                        ]
                        log_execution(
                            f"[A-GRADE] {symbol} {cand.side} | OB={ob_result['grade']} | "
                            f"score={ob_result['score']:.1f} | disp={ob_result['displacement_atr']:.2f}ATR",
                            "SUCCESS", debounce_key=f"agrade_{symbol}", debounce_sec=30
                        )
                    else:
                        cand.decision = "RORO_SIGNAL_WEAK_OB"
                        cand.decision_reasons = [f"Roro OK but OB {cand.ob_grade}"]
                else:
                    cand.decision = "NO_RORO_SIGNAL"
                    cand.decision_reasons = [roro_reason if not roro_ok else "Roro failed"]
                cand.evidence['roro_signal'] = roro_ok
                cand.evidence['ob_grade'] = cand.ob_grade
                cand.evidence['is_a_grade'] = cand.is_a_grade
                # ===== ATOM INTELLIGENCE LAYER (Roro Entry -> Atom Approval) =====
                # Roro stays the ENTRY ENGINE (timing + entry signal above). Atom
                # verifies OB Quality, Zone Freshness, Liquidity Support, Structure,
                # classifies TREND/REVERSAL and (for REVERSAL only) requires extra
                # Sniper confirmation before allowing READY. It is additive and
                # advisory: it never flips a weak setup into a strong one and only
                # hard-rejects Fake/Broken/Stale OB or an over-mitigated zone.
                cand.trade_type = TRADE_TREND
                cand.atom_approved = False
                cand.atom_hard_reject = False
                cand.atom_sniper_confirmed = False
                cand.atom_confidence_adjust = 0.0
                if ATOM_INTELLIGENCE_AVAILABLE and AtomIntelligenceEngine is not None:
                    try:
                        _ai_conf = sniper_conf.to_dict() if sniper_conf is not None else {}
                        _sniper_ev = _ai_conf.get("evidence", {}) or {}
                        _sweep_aligned = bool(
                            cand.evidence.get("ob_sweep_aligned", False)
                            or cand.evidence.get("sweep_quality", "none") in ("strong", "weak")
                        )
                        _mss_up = bool(_sniper_ev.get("mss_bullish", False))
                        _mss_dn = bool(_sniper_ev.get("mss_bearish", False))
                        _ai_eng = AtomIntelligenceEngine(cand.ob_cfg)
                        _ai = _ai_eng.evaluate(
                            df, cand.side, atr,
                            ob_grade=cand.ob_grade,
                            ob_score=float(metrics.order_block_quality),
                            zone_age_bars=(len(df) - cand.zone_origin_bar) if cand.zone_origin_bar > 0 else 999,
                            zone_touches=int(cand.zone_touches),
                            zone_mitigated_bars=(2 if cand.zone_state in ("INVALIDATED", "STALE") else 0),
                            price_interacting=cand.zone_state in ("ENTRY_WINDOW", "RETEST", "ACTIVE"),
                            trigger_state=metrics.trigger_state,
                            sweep_aligned=_sweep_aligned,
                            mss_bullish=_mss_up,
                            mss_bearish=_mss_dn,
                            struct_score=float(struct_score),
                            adx=float(compute_adx(df).iloc[-1]) if len(df) >= 15 else 0.0,
                            sniper_conf=sniper_conf,
                            act_demand=_sniper_ev.get("nearest_demand"),
                            act_supply=_sniper_ev.get("nearest_supply"),
                            delta_bullish=bool(_sniper_ev.get("delta_bullish", False)),
                            delta_bearish=bool(_sniper_ev.get("delta_bearish", False)),
                            volume_ratio=float(_sniper_ev.get("volume_ratio", 0.0) or 0.0),
                        )
                        cand.trade_type = _ai.trade_type
                        cand.atom_intel = _ai.to_dict()
                        cand.atom_approved = bool(_ai.approved)
                        cand.atom_hard_reject = bool(_ai.hard_reject)
                        cand.atom_sniper_confirmed = bool(_ai.sniper_confirmed)
                        cand.atom_confidence_adjust = float(_ai.confidence_adjust)
                        cand.evidence["atom_intel"] = cand.atom_intel
                        cand.evidence["atom_trade_type"] = cand.trade_type
                        # Bounded, advisory confidence adjust (capped like sniper,
                        # never flips weak -> strong).
                        metrics.confluence_bonus = max(
                            0.0, min(5.0, metrics.confluence_bonus + _ai.confidence_adjust * 0.25))
                        cand.priority_score = metrics.final_zone_score
                        if _ai.hard_reject:
                            log_execution(
                                f"[ATOM-INTEL] {symbol} HARD_REJECT {_ai.trade_type}: "
                                f"{'; '.join(_ai.reasons[:3])}", "WARN")
                        else:
                            _sniper_desc = (
                                f"req={_ai.sniper_required} confirmed={_ai.sniper_confirmed}"
                                if _ai.sniper_required
                                else "not_required"
                            )
                            log_execution(
                                f"[ATOM-INTEL] {symbol} {cand.side} type={_ai.trade_type} "
                                f"approved={_ai.approved} sniper={_sniper_desc} "
                                f"ob={_ai.ob_grade}({_ai.ob_score:.0f}) zone={_ai.freshness.state} "
                                f"liq={_ai.liquidity.score:.0f} | {'; '.join(_ai.reasons[:3])}",
                                "INFO", debounce_key=f"atomintel_{symbol}", debounce_sec=30)
                    except Exception as _atom_eval_err:  # pragma: no cover - defensive
                        if not getattr(self, "_atom_intel_warned", False):
                            self._atom_intel_warned = True
                            log_execution(f"[ATOM-INTEL] eval failed for {symbol}: {_atom_eval_err}", "ERROR")

                # ===== EARLY ENTRY CONFLUENCE LAYER (advisory, NOT a gate) =====
                # Detects the START of a move out of a fresh, liquidity-backed
                # zone (distance_from_zone + phase FIRST/DEVELOPING/LATE + RF
                # alignment). It is advisory only: it stores evidence, emits the
                # [ATOM-EARLY] log and nudges confluence_bonus (capped) -- it
                # NEVER blocks a Roro-valid entry and leaves RORO / RF / Entry /
                # Execution / Risk untouched.
                if EARLY_CONFLUENCE_AVAILABLE and EarlyEntryConfluenceEngine is not None:
                    try:
                        # Range Filter (existing engine, part of confluence only).
                        _rf_sig = None
                        try:
                            _rf_d = RFEngine(20, 3.5).compute(df)
                            _rf_sig = _rf_d.get("signal")
                        except Exception:  # pragma: no cover - defensive
                            _rf_sig = None
                        _zone_lo = getattr(cand, "zone_low", None)
                        _zone_hi = getattr(cand, "zone_high", None)
                        _zone_state = getattr(cand, "zone_state", "ACTIVE") or "ACTIVE"
                        _zone_fresh = "UNKNOWN"
                        _atom_map = cand.atom_intel or {}
                        _ff = _atom_map.get("freshness_state")
                        if _ff in ("FRESH", "AGING", "STALE", "OVER_MITIGATED"):
                            _zone_fresh = _ff
                        elif _zone_state in ("ENTRY_WINDOW", "RETEST", "ACTIVE"):
                            _zone_fresh = "FRESH"
                        _liq_score = float(_atom_map.get("liquidity_score", 50.0))
                        _liq_sup = "STRONG" if _liq_score >= 70 else ("PRESENT" if _liq_score >= 50 else "WEAK")
                        _early_zone_type = "DEMAND" if str(cand.side).upper() in ("BUY", "LONG") else "SUPPLY"
                        _early_res = analyze_early_entry(
                            df, cand.side,
                            zone_low=_zone_lo, zone_high=_zone_hi,
                            zone_type=_early_zone_type,
                            zone_freshness=_zone_fresh,
                            liquidity_support=_liq_sup,
                            rf_signal=_rf_sig,
                            symbol=symbol)
                        cand.early_entry = _early_res.to_dict()
                        cand.evidence["early_entry_confluence"] = cand.early_entry
                        # Advisory bonus ONLY (never a gate). We apply it as a
                        # pure boost, never a subtraction, so this layer can NOT
                        # drag the Roro/ATOM-granted confluence_bonus below the
                        # A-grade fast-confirm threshold (which lives in _update_state
                        # / _is_a_grade and is part of the RORO readiness machine).
                        # A LATE / weak-confluence verdict is advisory via evidence
                        # and the [ATOM-EARLY] log only; it must never flip a
                        # Roro-valid candidate out of its own READY decision.
                        _early_shift = float(_early_res.score_shift or 0.0)
                        if _early_shift > 0.0:
                            metrics.confluence_bonus = max(
                                0.0, min(5.0, metrics.confluence_bonus + _early_shift))
                        cand.priority_score = metrics.final_zone_score
                        if _early_res.action == EARLY_ACTION_ENTRY:
                            log_execution(_early_res.log_line, "INFO",
                                          debounce_key=f"earlyentry_{symbol}", debounce_sec=15)
                        elif _early_res.evidence.phase == EARLY_PHASE_LATE:
                            log_execution(
                                _early_res.log_line,
                                "INFO", debounce_key=f"earlyentry_{symbol}", debounce_sec=15)
                    except Exception as _early_eval_err:  # pragma: no cover - defensive
                        if not getattr(self, "_early_conf_warned", False):
                            self._early_conf_warned = True
                            log_execution(f"[ATOM-EARLY] eval failed for {symbol}: {_early_eval_err}", "ERROR")
                self._update_state(cand, current_price)
                self.total_evaluations += 1
                if cand.priority_score < 30:
                    self.gate_stats["score_too_low"] += 1
                    record_gate_event(symbol, "QUEUE", "SCORE_TOO_LOW",
                                      f"zone_score={cand.priority_score:.1f} < 30", cand.side)
                    self._return_to_watchlist(symbol, "Score too low")

    def _zone_anchor(self, cand):
        return cand.zone_mid() if (cand.zone_low and cand.zone_high) else cand.entry_price

    @staticmethod
    def _resolve_ob_cfg(symbol):
        """OB-detection config for a symbol (legacy unified when tuning off)."""
        return AssetBehaviorProfile.ob_config(AssetBehaviorProfile.resolve_asset_class(symbol or ""))

    def _is_extended(self, cand, price):
        anchor = self._zone_anchor(cand)
        return abs(price - anchor) > cand.atr * 1.5

    def _is_order_block_broken(self, cand, price):
        anchor = self._zone_anchor(cand)
        if cand.side == "BUY":
            return price < anchor - cand.atr * 0.8
        else:
            return price > anchor + cand.atr * 0.8

    def _update_zone_lifecycle(self, cand, price, atr):
        if atr <= 0:
            return False
        if not (cand.zone_low and cand.zone_high):
            cand.zone_state = "ACTIVE"
            return False
        dist_atr = abs(price - cand.zone_mid()) / atr
        in_window = dist_atr <= 0.75
        near_window = dist_atr <= 1.5
        if cand.side == "BUY" and price < cand.zone_low - atr * 0.15:
            cand.zone_state = "INVALIDATED"
            record_gate_event(cand.symbol, "QUEUE", "ZONE_INVALIDATED",
                              f"close {price:.6g} through zone low {cand.zone_low:.6g}", cand.side)
            self._invalidate(cand.symbol, "Zone invalidated (close through far edge)")
            return True
        if cand.side == "SELL" and price > cand.zone_high + atr * 0.15:
            cand.zone_state = "INVALIDATED"
            record_gate_event(cand.symbol, "QUEUE", "ZONE_INVALIDATED",
                              f"close {price:.6g} through zone high {cand.zone_high:.6g}", cand.side)
            self._invalidate(cand.symbol, "Zone invalidated (close through far edge)")
            return True
        if dist_atr > 1.5:
            if cand.zone_state in ("EXPANDED_AWAY", "STALE"):
                cand.zone_state = "STALE"
                cand.decision = "STALE"
                cand.decision_reasons = [f"price {dist_atr:.1f} ATR from zone; original OB escaped"]
                record_gate_event(cand.symbol, "QUEUE", "ZONE_STALE",
                                  f"price {dist_atr:.1f} ATR from OB; move already left", cand.side)
                self._return_to_watchlist(cand.symbol, f"Zone stale ({dist_atr:.1f} ATR from OB)")
                return True
            cand.zone_state = "EXPANDED_AWAY"
            return False
        if in_window and cand.zone_state == "EXPANDED_AWAY":
            cand.zone_state = "RETEST"
            return False
        if in_window:
            cand.zone_state = "ENTRY_WINDOW"
        elif near_window:
            cand.zone_state = "ACTIVE"
        return False

    def _evaluate_order_block(self, df, side, atr, cfg=None):
        self._last_ob_analysis = {}
        if not is_valid_dataframe(df) or atr <= 0:
            return 25, OrderBlockQuality.WEAK
        side = str(side).upper()
        n = len(df)
        search_bars = int((cfg or {}).get("ob_search_bars", 35))
        min_disp = float((cfg or {}).get("ob_min_disp_atr", 0.6))
        lookback_start = max(2, n - search_bars)
        best = None
        best_broken = None
        for i in range(lookback_start, n - 2):
            base = df.iloc[i]
            body = abs(float(base['close']) - float(base['open']))
            rng = max(float(base['high']) - float(base['low']), 1e-12)
            if body / rng > 0.75:
                continue
            if side == "BUY" and float(base['close']) >= float(base['open']):
                continue
            if side == "SELL" and float(base['close']) <= float(base['open']):
                continue
            future = df.iloc[i + 1:min(n, i + 4)]
            if future.empty:
                continue
            if side == "BUY":
                displacement = float(future['close'].max()) - float(base['high'])
                directional = float(future['close'].iloc[-1]) > float(base['high'])
                zone_low, zone_high = float(base['low']), float(base['open'])
            else:
                displacement = float(base['low']) - float(future['close'].min())
                directional = float(future['close'].iloc[-1]) < float(base['low'])
                zone_low, zone_high = float(base['open']), float(base['high'])
            displacement_atr = displacement / atr
            if displacement_atr < min_disp or not directional:
                continue
            vol_avg = float(df['volume'].iloc[max(0, i - 10):i].mean()) if 'volume' in df else 0.0
            disp_vol = float(future['volume'].max()) if 'volume' in future else 0.0
            vol_ratio = disp_vol / vol_avg if vol_avg > 0 else 1.0
            later = df.iloc[i + 1:]
            touches = 0
            broken = False
            for _, candle in later.iterrows():
                low = float(candle['low']); high = float(candle['high']); close = float(candle['close'])
                if high >= zone_low and low <= zone_high:
                    touches += 1
                if side == "BUY" and close < zone_low - atr * 0.15:
                    broken = True
                if side == "SELL" and close > zone_high + atr * 0.15:
                    broken = True
            price = float(df['close'].iloc[-1])
            distance = abs(price - (zone_low + zone_high) / 2) / max(price, 1e-12)
            candidate = (displacement_atr, vol_ratio, -touches, -distance, i, broken, zone_low, zone_high)
            # A broken OB can never be traded; prefer any valid candidate and
            # keep the best broken one only as a fallback signal (BROKEN) so a
            # garbage best never masks a real causal OB (mirrors
            # _find_causal_ob_zone, which already skips broken candles).
            if broken:
                if best_broken is None or candidate[:5] > best_broken[:5]:
                    best_broken = candidate
                continue
            if best is None or candidate[:5] > best[:5]:
                best = candidate
        if best is None and best_broken is not None:
            return 20, OrderBlockQuality.BROKEN
        if best is None:
            return 25, OrderBlockQuality.FAKE
        displacement_atr, vol_ratio, neg_touches, neg_distance, idx, broken, zone_low, zone_high = best
        touches = int(-neg_touches)
        if broken:
            return 20, OrderBlockQuality.BROKEN
        score = 50.0
        score += min(25.0, max(0.0, (displacement_atr - 0.6) * 22.0))
        score += min(15.0, max(0.0, (vol_ratio - 1.0) * 10.0))
        score += 12.0 if touches <= 1 else 6.0 if touches <= 2 else -8.0
        recent = df.iloc[max(idx + 1, n - 4):]
        rejection = False
        if not recent.empty:
            last = recent.iloc[-1]
            if side == "BUY":
                rejection = float(last['low']) <= zone_high and float(last['close']) > float(last['open'])
            else:
                rejection = float(last['high']) >= zone_low and float(last['close']) < float(last['open'])
        if rejection:
            score += 8.0
        synergy = self._ob_synergy(df, side, atr, idx, zone_low, zone_high, cfg)
        score += synergy["bonus"]
        self._last_ob_analysis = synergy
        score = max(0.0, min(100.0, score))
        if score >= 82 and touches <= 1:
            quality = OrderBlockQuality.FRESH
        elif score >= 65:
            quality = OrderBlockQuality.TESTED
        elif score < 45:
            quality = OrderBlockQuality.WEAK
        else:
            quality = OrderBlockQuality.TESTED
        return round(score, 2), quality

    def _find_causal_ob_zone(self, df, side, atr, cfg=None):
        if not is_valid_dataframe(df) or atr <= 0:
            return 0.0, 0.0, -1, 0
        side = str(side).upper()
        n = len(df)
        search_bars = int((cfg or {}).get("ob_search_bars", 35))
        min_disp = float((cfg or {}).get("ob_min_disp_atr", 0.6))
        lookback_start = max(2, n - search_bars)
        best = None
        for i in range(lookback_start, n - 2):
            base = df.iloc[i]
            body = abs(float(base['close']) - float(base['open']))
            rng = max(float(base['high']) - float(base['low']), 1e-12)
            if body / rng > 0.75:
                continue
            if side == "BUY" and float(base['close']) >= float(base['open']):
                continue
            if side == "SELL" and float(base['close']) <= float(base['open']):
                continue
            future = df.iloc[i + 1:min(n, i + 4)]
            if future.empty:
                continue
            if side == "BUY":
                displacement = float(future['close'].max()) - float(base['high'])
                directional = float(future['close'].iloc[-1]) > float(base['high'])
                zone_low, zone_high = float(base['low']), float(base['open'])
            else:
                displacement = float(base['low']) - float(future['close'].min())
                directional = float(future['close'].iloc[-1]) < float(base['low'])
                zone_low, zone_high = float(base['open']), float(base['high'])
            displacement_atr = displacement / atr
            if displacement_atr < min_disp or not directional:
                continue
            later = df.iloc[i + 1:]
            touches = 0
            broken = False
            for _, candle in later.iterrows():
                low = float(candle['low']); high = float(candle['high']); close = float(candle['close'])
                if high >= zone_low and low <= zone_high:
                    touches += 1
                if side == "BUY" and close < zone_low - atr * 0.15:
                    broken = True
                if side == "SELL" and close > zone_high + atr * 0.15:
                    broken = True
            if broken:
                continue
            price = float(df['close'].iloc[-1])
            distance = abs(price - (zone_low + zone_high) / 2) / max(price, 1e-12)
            candidate = (displacement_atr, 0.0, -touches, -distance, i, zone_low, zone_high)
            if best is None or candidate[:5] > best[:5]:
                best = candidate
        if best is None:
            return 0.0, 0.0, -1, 0
        _, _, neg_touches, _, idx, zone_low, zone_high = best
        return zone_low, zone_high, idx, int(-neg_touches)

    def _evaluate_zone_strength(self, df, side, atr, entry_price):
        if not is_valid_dataframe(df):
            return 50
        touches = 0
        rejections = 0
        vol_sum = 0
        for i in range(max(0, len(df)-30), len(df)-1):
            candle = df.iloc[i]
            if side == "BUY":
                if abs(candle['low'] - entry_price) < atr * 0.5:
                    touches += 1
                    if df['close'].iloc[i+1] > candle['close']:
                        rejections += 1
                        vol_sum += candle['volume']
            else:
                if abs(candle['high'] - entry_price) < atr * 0.5:
                    touches += 1
                    if df['close'].iloc[i+1] < candle['close']:
                        rejections += 1
                        vol_sum += candle['volume']
        score = 50
        if touches >= 4:
            score += 25
        elif touches >= 2:
            score += 12
        elif touches >= 1:
            score += 5
        if rejections >= 3:
            score += 20
        elif rejections >= 2:
            score += 10
        avg_vol = df['volume'].iloc[-30:].mean()
        if touches > 0 and avg_vol > 0:
            avg_touch_vol = vol_sum / touches
            if avg_touch_vol > 2 * avg_vol:
                score += 15
            elif avg_touch_vol > 1.5 * avg_vol:
                score += 8
        return min(100, max(0, score))

    def _absorption_evidence(self, df, side, sweep_j):
        if not is_valid_dataframe(df) or sweep_j is None or len(df) < sweep_j + 4 or 'volume' not in df.columns:
            return {"score": 0, "detected": False}
        trail = df.iloc[sweep_j+1:sweep_j+4]
        vols = trail['volume'] if 'volume' in df else None
        mean_vol = float(vols.mean()) if vols is not None else 0.0
        repeater = []
        for _, c in trail.iterrows():
            if side == "BUY" and float(c['close']) > float(c['open']):
                repeater.append(True)
            elif side == "SELL" and float(c['close']) < float(c['open']):
                repeater.append(True)
        absorbed = all(repeater) and len(repeater) > 0
        score = 70 if absorbed else 20
        return {"score": score, "detected": bool(absorbed)}

    def _evaluate_liquidity(self, df, side, atr):
        if not is_valid_dataframe(df):
            return 50.0, {"state": "LIQUIDITY_UNAVAILABLE", "pool": 0, "proximity": 0,
                          "sweep": 0, "sweep_age": None, "displacement": 0,
                          "rejection": 0, "structure": 0, "absorption": 0,
                          "data_conf": 0, "composite": 50.0}
        pools = self._build_liquidity_pools(df)
        price = float(df['close'].iloc[-1]) if len(df) else 0.0
        key = 'low_pools' if side == "BUY" else 'high_pools'
        pool_list = pools.get(key, []) if isinstance(pools, dict) else []
        if not pool_list or len(pool_list) == 0:
            return 30.0, {"state": "LIQUIDITY_UNAVAILABLE", "pool": 0, "proximity": 0,
                          "sweep": 0, "sweep_age": None, "displacement": 0,
                          "rejection": 0, "structure": 0, "absorption": 0,
                          "data_conf": 0, "composite": 30.0}
        best = min(pool_list, key=lambda p: abs(price - p[1])) if pool_list else None
        if best is None:
            return 30.0, {"state": "LIQUIDITY_UNAVAILABLE", "pool": 0, "proximity": 0,
                          "sweep": 0, "sweep_age": None, "displacement": 0,
                          "rejection": 0, "structure": 0, "absorption": 0,
                          "data_conf": 0, "composite": 30.0}
        level = float(best[1])
        data_conf = 100 if ('volume' in df and len(df) >= 50) else (60 if len(df) >= 30 else 30)
        distance_atr = abs(price - level) / atr if atr > 0 else float('inf')
        proximity_score = int(max(0, min(100, 100 - (distance_atr * 50 if atr > 0 else 100))))
        eq_highs, eq_lows = self._detect_equal_highs_lows(df)
        is_equal = eq_lows if side == "BUY" else eq_highs
        pool_strength = 45 + (25 if is_equal else 0) + (20 if data_conf == 100 else 10 if data_conf >= 60 else 0)
        evidence = {"pool": pool_strength, "proximity": proximity_score,
                    "sweep": 0, "sweep_age": None, "displacement": 0,
                    "rejection": 0, "structure": 0, "absorption": 0,
                    "data_conf": data_conf, "level": level, "distance_atr": float(distance_atr)}
        swept_high_age, swept_low_age = self._detect_sweep(df, pools)
        sweep_age = swept_low_age if side == "BUY" else swept_high_age
        if sweep_age is None:
            evidence["state"] = "LIQUIDITY_AVAILABLE" if proximity_score >= 60 else "LIQUIDITY_PRESENT"
            evidence["composite"] = float(pool_strength * 0.6 + proximity_score * 0.4)
            return float(max(0, min(100, evidence["composite"]))), evidence
        last = df.iloc[-1]
        rejected = (float(last['close']) > float(last['open'])) if side == "BUY" \
                   else (float(last['close']) < float(last['open']))
        j = min(len(df) - 1 - sweep_age, len(df) - 1)
        try:
            struct_score = self._evaluate_structure(df, side)[0]
        except Exception:
            struct_score = 0
        if side == "BUY":
            disp_move = float(last['close']) - float(df['low'].iloc[j])
        else:
            disp_move = float(df['high'].iloc[j]) - float(last['close'])
        disp_atr = float(disp_move / atr) if atr > 0 else 0.0
        evidence.update({
            "sweep": 100,
            "sweep_age": int(sweep_age),
            "displacement": int(max(0, min(100, disp_atr * 20))),
            "rejection": 84 if rejected else 30,
            "structure": int(struct_score),
            "absorption": self._absorption_evidence(df, side, j)["score"],
            "composite": 0.0,
        })
        is_recent_sweep = sweep_age <= 5
        is_strong_sweep = evidence["displacement"] > 40 and evidence["rejection"] > 60
        if is_recent_sweep and (evidence["displacement"] > 30 or evidence["rejection"] > 50):
            evidence["state"] = "LIQUIDITY_SWEPT"
        else:
            evidence["state"] = "LIQUIDITY_NEAR" if proximity_score >= 60 else "LIQUIDITY_PRESENT"
        evidence["composite"] = float(
            evidence["sweep"] * 0.22 + evidence["displacement"] * 0.20 +
            evidence["structure"] * 0.18 + evidence["absorption"] * 0.12 +
            evidence["rejection"] * 0.12 + evidence["pool"] * 0.09 +
            evidence["proximity"] * 0.07)
        return float(max(0, min(100, evidence["composite"]))), evidence

    def _evaluate_institutional(self, df, side):
        try:
            smart = SmartMoneyEngine.analyze_smart_money(df)
            mom = MomentumFlowEngine.analyze_momentum_flow(df)
        except:
            return 50
        score = 50
        if smart.get('smart_money_dominant', False):
            score += 15
            if (side == "BUY" and smart['institutional_bias'] == "BUY") or \
               (side == "SELL" and smart['institutional_bias'] == "SELL"):
                score += 15
        dist = smart.get('distribution_risk', 0)
        if side == "BUY" and dist < 30:
            score += 10
        elif side == "SELL" and dist > 60:
            score += 10
        acc = smart.get('accumulation_strength', 0)
        if side == "BUY" and acc > 60:
            score += 10
        elif side == "SELL" and acc < 40:
            score += 10
        if mom.get('trend_expansion', False):
            score += 5
        if mom.get('momentum_decay', False):
            score -= 10
        return min(100, max(0, score))

    def _evaluate_structure(self, df, side):
        bos_up, bos_down = self._detect_bos(df)
        struct_shift = self._detect_structure_shift(df)
        score = 50
        struct_type = MarketStructure.NONE
        if side == "BUY":
            if struct_shift == "bullish_shift":
                score = 90
                struct_type = MarketStructure.MSS
            elif bos_up:
                score = 70
                struct_type = MarketStructure.BOS
        else:
            if struct_shift == "bearish_shift":
                score = 90
                struct_type = MarketStructure.MSS
            elif bos_down:
                score = 70
                struct_type = MarketStructure.BOS
        return score, struct_type

    def _evaluate_timing(self, df, side, atr, current_price, entry_price):
        if not is_valid_dataframe(df):
            return 50
        dist_atr = (abs(current_price - entry_price) / atr) if atr > 0 else 0.0
        score = 50
        if dist_atr <= 0.25:
            score += 30
        elif dist_atr <= 0.75:
            score += 15
        elif dist_atr > 1.5:
            score -= 30
        elif dist_atr > 0.75:
            score -= 10
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ > 0:
            if side == "BUY":
                lower_wick = min(last['open'], last['close']) - last['low']
                if lower_wick / range_ > 0.5 and last['close'] > last['open']:
                    score += 20
            else:
                upper_wick = last['high'] - max(last['open'], last['close'])
                if upper_wick / range_ > 0.5 and last['close'] < last['open']:
                    score += 20
        vol_avg = df['volume'].iloc[-10:].mean()
        if vol_avg > 0 and df['volume'].iloc[-1] > 1.5 * vol_avg:
            score += 10
        return min(100, max(0, score))

    def _evaluate_trend_alignment(self, df, side):
        if len(df) < 20:
            return 50
        ema20 = df['close'].ewm(span=20).mean().iloc[-1]
        ema50 = df['close'].ewm(span=50).mean().iloc[-1]
        price = df['close'].iloc[-1]
        score = 50
        if side == "BUY":
            if price > ema20 > ema50:
                score += 25
            elif price > ema20:
                score += 10
            else:
                score -= 20
        else:
            if price < ema20 < ema50:
                score += 25
            elif price < ema20:
                score += 10
            else:
                score -= 20
        return min(100, max(0, score))

    def _evaluate_risk(self, cand, price):
        spread = get_spread_bps(cand.symbol)
        score = 50
        if spread < 0.05:
            score += 20
        elif spread < 0.1:
            score += 10
        elif spread > 0.2:
            score -= 30
        atr_pct = (cand.atr / cand.entry_price) * 100 if cand.entry_price > 0 else 0
        if 0.5 < atr_pct < 2.5:
            score += 10
        elif atr_pct > 4:
            score -= 20
        cfg = cand.ob_cfg or {}
        events = cfg.get("ob_news_events", ())
        if events and cand.news_event_type and cand.news_event_type in events and (cand.news_impact_score or 0) >= 60:
            score -= 25
        if cand.news_risk_level in ("HIGH_RISK", "CRITICAL"):
            score -= 20
        return min(100, max(0, score))

    def _classify_opportunity(self, metrics, struct_type, side, df):
        score = metrics.final_zone_score
        if score >= 85 and struct_type != MarketStructure.NONE:
            return OpportunityType.INSTITUTIONAL_REVERSAL
        elif score >= 70 and struct_type == MarketStructure.BOS:
            return OpportunityType.BREAKOUT_RETEST
        elif score >= 60 and struct_type != MarketStructure.NONE:
            return OpportunityType.TREND_CONTINUATION
        elif side == "BUY" and metrics.institutional_confidence > 70:
            return OpportunityType.ACCUMULATION_ENTRY
        elif side == "SELL" and metrics.institutional_confidence > 70:
            return OpportunityType.DISTRIBUTION_ENTRY
        elif metrics.order_block_quality < 40:
            return OpportunityType.FAKE_BREAKOUT
        elif metrics.order_block_quality < 50:
            return OpportunityType.WEAK_ORDER_BLOCK
        return OpportunityType.LOW_QUALITY

    def _detect_institutional_behaviour(self, df, side):
        try:
            smart = SmartMoneyEngine.analyze_smart_money(df)
            mom = MomentumFlowEngine.analyze_momentum_flow(df)
        except:
            return InstitutionalBehaviour.NEUTRAL
        banker = smart.get('banker_pressure', 50)
        retail = smart.get('retailer_pressure', 50)
        dist = smart.get('distribution_risk', 0)
        acc = smart.get('accumulation_strength', 0)
        if side == "BUY" and banker > retail and dist < 30 and acc > 60:
            return InstitutionalBehaviour.ACCUMULATION
        if side == "SELL" and banker < retail and dist > 50:
            return InstitutionalBehaviour.DISTRIBUTION
        if side == "BUY" and dist > 50 and acc > 50:
            return InstitutionalBehaviour.RE_ACCUMULATION
        if side == "SELL" and dist < 30 and acc > 50:
            return InstitutionalBehaviour.RE_DISTRIBUTION
        return InstitutionalBehaviour.NEUTRAL

    def _detect_trigger_state(self, df, side, atr, entry_price):
        if not is_valid_dataframe(df):
            return "WAITING_TRIGGER"
        ev = {"sweep_quality": "none", "structure_valid": False, "structure_score": 0,
              "rejection_score": 0, "absorption": 0, "trap_risk": 50, "response": 50}
        window = max(1, int(os.getenv("TRIGGER_EVENT_WINDOW_BARS", "3")))
        pools = self._build_liquidity_pools(df)
        swept_high, swept_low = self._detect_sweep(df, pools)
        sweep_shape = (side == "BUY" and swept_low is not None) or (side == "SELL" and swept_high is not None)
        sweep_age = swept_low if side == "BUY" else swept_high
        sweep_class, sweep_pts = "none", 0
        if sweep_shape and sweep_age is not None and sweep_age < window:
            sweep_class, sweep_pts = classify_sweep(df.iloc[:len(df) - sweep_age], side)
        ev["sweep_quality"] = sweep_class if sweep_shape else "none"
        ev["sweep_age"] = sweep_age
        sweep_ok = sweep_shape and sweep_class in ("strong", "weak")
        struct_valid, struct_reasons, struct_score = MSSValidator.validate_structure_shift(df, side, atr)
        ev["structure_valid"] = bool(struct_valid)
        ev["structure_score"] = int(struct_score)
        bos_up, bos_down = self._detect_bos(df)
        struct_shift = self._detect_structure_shift(df)
        bos_ok = (side == "BUY" and bos_up) or (side == "SELL" and bos_down)
        choch_ok = (side == "BUY" and struct_shift == "bullish_shift") or (side == "SELL" and struct_shift == "bearish_shift")
        structure_confirmed = struct_valid or bos_ok or choch_ok
        rejection_ok = False
        rej_valid, rej_reasons = False, []
        for k in range(min(window, len(df))):
            sub = df.iloc[:len(df) - k] if k else df
            if side == "BUY":
                rv, rr = RejectionIntelligence.is_bullish_rejection(sub, atr)
            else:
                rv, rr = RejectionIntelligence.is_bearish_rejection(sub, atr)
            if bool(rv) or candle_rejection(sub, side):
                rejection_ok = True
                rej_valid, rej_reasons = bool(rv), rr
                break
        ev["rejection_score"] = len(rej_reasons) if rej_valid else 0
        displacement_ok = False
        for k in range(min(window, max(0, len(df) - 1))):
            sub = df.iloc[:len(df) - k] if k else df
            if detect_displacement(sub, side, atr, classify_volume(sub),
                                   body_atr_threshold=0.8, volume_expansion_required=False):
                displacement_ok = True
                break
        if sweep_age is not None:
            j = min(len(df) - 1 - sweep_age, len(df) - 1)
            ev["absorption"] = self._absorption_evidence(df, side, j)["score"]
        try:
            _auth, trap_risk, _ad = compute_liquidity_authenticity(df, side, atr)
            ev["trap_risk"] = int(trap_risk)
        except Exception:
            pass
        try:
            resp_score, _rd = evaluate_post_sweep_response(df, side, atr, df['close'].iloc[-1], entry_price)
            ev["response"] = int(resp_score)
        except Exception:
            pass
        dist = abs(df['close'].iloc[-1] - entry_price) / entry_price if entry_price else 1.0
        near_entry = dist < 0.003
        if sweep_ok and structure_confirmed and rejection_ok:
            state = "MSS_CONFIRMED"
        elif sweep_ok and near_entry and rejection_ok:
            state = "LIQUIDITY_SWEEP"
        elif bos_ok and displacement_ok and structure_confirmed:
            state = "BOS_CONFIRMED"
        elif choch_ok and displacement_ok and structure_confirmed:
            state = "CHOCH_CONFIRMED"
        elif sweep_shape and not structure_confirmed:
            state = "MITIGATION"
        elif near_entry and structure_confirmed:
            state = "WAITING_TRIGGER"
        elif near_entry:
            state = "MITIGATION"
        elif displacement_ok:
            state = "DISPLACEMENT"
        else:
            state = "WAITING_TRIGGER"
        ev["trigger"] = state
        ev["rejection_or_displacement"] = bool(rejection_ok or displacement_ok)
        ob_analysis = getattr(self, "_last_ob_analysis", {}) or {}
        ev["ob_sweep_aligned"] = bool(ob_analysis.get("sweep_aligned", False))
        ev["ob_fvg_after_displacement"] = bool(ob_analysis.get("fvg_after_displacement", False))
        ev["ob_pd_aligned"] = bool(ob_analysis.get("pd_aligned", False))
        ev["ob_bos_aligned"] = bool(ob_analysis.get("bos_aligned", False))
        self._last_evidence = ev
        return state

    @staticmethod
    def _confirm_signature(trigger_state, evidence, cand):
        return "|".join([
            str(cand.symbol),
            str(cand.side),
            str(cand.zone_low),
            str(cand.zone_high),
            trigger_state,
            str(evidence.get("sweep_quality", "")),
            str(evidence.get("structure_score", "")),
            str(evidence.get("rejection_score", "")),
            str(evidence.get("absorption", "")),
        ])

    def _ob_synergy(self, df, side, atr, idx, zone_low, zone_high, cfg=None):
        """Weighted confluence enhancements for the causal OB (additive, never
        blocking): sweep-aligned, FVG-after-displacement, premium/discount
        alignment and BOS/CHoCH alignment. Bonus is capped so a high-grade OB
        keeps its ceiling; flags feed the confirmation/decision evidence."""
        flags = {"sweep_aligned": False, "fvg_after_displacement": False,
                 "pd_aligned": False, "bos_aligned": False, "bonus": 0.0}
        if not is_valid_dataframe(df) or atr <= 0 or idx is None:
            return flags
        n = len(df)
        pos = n + idx if idx < 0 else idx
        if pos < 1 or pos >= n:
            return flags
        side = str(side).upper()
        sweep_aligned = False
        try:
            pools = self._build_liquidity_pools(df)
            if (cfg or {}).get("ob_round_number_liq"):
                pools = self._augment_round_pools(df, pools, atr)
            swept_high, swept_low = self._detect_sweep(df, pools)
            sweep_target = swept_low if side == "BUY" else swept_high
            if sweep_target is not None and sweep_target <= 3:
                sweep_aligned = True
        except Exception:
            sweep_aligned = False
        fvg_aligned = False
        leg_start, leg_stop = pos + 1, min(n, pos + 4)
        if leg_stop > leg_start:
            leg_df = df.iloc[leg_start:leg_stop]
            for k in range(1, len(leg_df)):
                prev_row = leg_df.iloc[k - 1]
                row = leg_df.iloc[k]
                if side == "BUY" and float(row['low']) > float(prev_row['high']):
                    fvg_aligned = True
                    break
                if side == "SELL" and float(row['high']) < float(prev_row['low']):
                    fvg_aligned = True
                    break
        mid = 0.0
        if zone_low and zone_high:
            mid = (float(zone_low) + float(zone_high)) / 2.0
        price = float(df['close'].iloc[-1])
        # REAL premium/discount: price must be on the discount side of the 50%
        # midpoint of the LOCAL trading range (last meaningful swing high/low),
        # not merely below the zone's own midpoint. The zone-mid rule is kept
        # as a fallback so price interacting the zone body still counts.
        try:
            pd_extend = int((cfg or {}).get("ob_pd_extend_bars", 0))
            from_idx = max(0, pos - (8 + pd_extend))
            to_idx = min(n, pos + 6)
            rng_hi = float(df['high'].iloc[from_idx:to_idx].max())
            rng_lo = float(df['low'].iloc[from_idx:to_idx].min())
            discount_50 = (rng_hi + rng_lo) / 2.0
        except Exception:
            discount_50 = mid
        pd_aligned = False
        if mid > 0 and discount_50 > 0 and price > 0:
            if side == "BUY":
                # discount = price at/below both the range midpoint and zone mid
                pd_aligned = price <= min(discount_50, mid)
            else:
                pd_aligned = price >= max(discount_50, mid)
        try:
            bos_up, bos_down = self._detect_bos(df)
            struct_shift = self._detect_structure_shift(df)
            bos_aligned = (
                (side == "BUY" and (bos_up or struct_shift == "bullish_shift"))
                or (side == "SELL" and (bos_down or struct_shift == "bearish_shift"))
            )
        except Exception:
            bos_aligned = False
        flags["sweep_aligned"] = bool(sweep_aligned)
        flags["fvg_after_displacement"] = bool(fvg_aligned)
        flags["pd_aligned"] = bool(pd_aligned)
        flags["bos_aligned"] = bool(bos_aligned)
        bonus = 0.0
        if sweep_aligned:
            bonus += 5.0
        if fvg_aligned:
            bonus += 5.0
        if pd_aligned:
            bonus += 4.0
        flags["bonus"] = min(14.0, bonus)
        return flags

    def _augment_round_pools(self, df, pools, atr):
        """Add round-number (psychologically important) liquidity levels to the
        pool list for classes like GOLD where whole numbers act as magnets.
        Tolerance is an ATR fraction around the nearest integer; appended pools
        keep the same (index, level) shape and are pruned to the latest 3."""
        if not is_valid_dataframe(df) or 'low' not in df.columns or 'high' not in df.columns:
            return pools
        tol = max(atr * 0.1, 1e-9)
        out = {
            "high_pools": list(pools.get("high_pools", [])),
            "low_pools": list(pools.get("low_pools", [])),
        }
        hi_seen = {float(p[1]): True for p in out["high_pools"]}
        lo_seen = {float(p[1]): True for p in out["low_pools"]}
        for i in range(max(0, len(df) - 30), len(df)):
            for level in (float(df['high'].iloc[i]), float(df['low'].iloc[i])):
                rounded = round(level)
                if abs(level - rounded) <= tol:
                    if level == float(df['high'].iloc[i]) and rounded not in hi_seen:
                        out["high_pools"].append((i, rounded))
                        hi_seen[rounded] = True
                    elif level == float(df['low'].iloc[i]) and rounded not in lo_seen:
                        out["low_pools"].append((i, rounded))
                        lo_seen[rounded] = True
        out["high_pools"] = out["high_pools"][-3:]
        out["low_pools"] = out["low_pools"][-3:]
        return out

    def _causal_ob_score(self, displacement_atr, vol_ratio, touches, freshness):
        """Unified causal-OB score used for A+/A/B grading: measures the SAME
        causal zone that the entry trades (displacement into the following leg,
        volume expansion, zone touches, freshness)."""
        score = 50.0
        score += min(25.0, max(0.0, (displacement_atr - 0.6) * 22.0))
        score += min(15.0, max(0.0, (vol_ratio - 1.0) * 10.0))
        score += 12.0 if touches <= 1 else 6.0 if touches <= 2 else -8.0
        if freshness <= 10:
            score += 10.0
        elif freshness <= 20:
            score += 5.0
        elif freshness > 40:
            score -= 8.0
        return max(0.0, min(100.0, score))

    @staticmethod
    def _confirm_marker(trigger_state, evidence, bar_count, last_price, atr=0.0):
        """Event-based confirmation marker: changes only when the MARKET evolves
        (new completed bar, new trigger state, fresh sweep target, or a
        meaningful price move). Identical re-polls never inflate the count,
        while frozen bars can no longer freeze a structurally strong candidate."""
        sweep_age = evidence.get("sweep_age", "")
        price_bucket = ""
        if atr and atr > 0:
            price_bucket = str(int((last_price or 0) / atr))
        return "|".join([str(trigger_state), str(bar_count), str(sweep_age), price_bucket])

    def _classify_decision(self, cand):
        side = cand.side
        score = cand.zone_metrics.final_zone_score
        ev = cand.evidence or {}
        sq = ev.get("sweep_quality", "none")
        trap_risk = ev.get("trap_risk", 50)
        absorption = ev.get("absorption", 50)
        response = ev.get("response", 50)
        zm = cand.zone_metrics
        families = {}
        zone_ok = (
            cand.zone_low > 0
            and cand.zone_state in ("ACTIVE", "ENTRY_WINDOW", "RETEST")
            and zm.order_block_quality >= 40
        )
        families["zone"] = zone_ok
        families["liquidity"] = sq in ("strong", "weak")
        families["structure"] = bool(ev.get("structure_valid")) or ev.get("structure_score", 0) >= 3
        families["volume"] = (
            zm.liquidity_quality >= 55
            or absorption >= 60
            or response >= 60
        )
        families["trend"] = (
            zm.trend_alignment >= 55 or zm.institutional_confidence >= 65
        )
        families["confluence"] = (
            (sq in ("strong", "weak") or ev.get("ob_sweep_aligned", False))
            and (ev.get("ob_fvg_after_displacement", False) or ev.get("ob_pd_aligned", False))
        )
        n_families = sum(1 for v in families.values() if v)
        confirmed = [k for k, v in families.items() if v]
        eq = 25.0 + n_families * 15.0
        if sq == "strong":
            eq += 8
        if absorption >= 70:
            eq += 4
        if zm.trigger_state in ("BOS_CONFIRMED", "CHOCH_CONFIRMED"):
            eq += 6
        eq = max(0.0, min(100.0, eq))
        composite = round(0.55 * score + 0.45 * eq, 2)
        trap_veto_threshold = float(os.getenv("TRAP_VETO_THRESHOLD", "0"))
        trap_veto = trap_veto_threshold > 0 and trap_risk >= trap_veto_threshold
        if sq == "fake" or trap_veto or n_families == 0:
            label = "INVALID_" + ("LONG" if side == "BUY" else "SHORT")
        elif n_families >= 3:
            label = "STRONG_" + ("LONG" if side == "BUY" else "SHORT")
        elif n_families == 2:
            label = "MEDIUM_" + ("LONG" if side == "BUY" else "SHORT")
        else:
            label = "WEAK_" + ("LONG" if side == "BUY" else "SHORT")
        cand.decision_reasons = [
            f"families={n_families}({'+'.join(confirmed)})",
            f"composite={composite}", f"evidence_q={eq:.0f}", f"trap_risk={trap_risk}",
            f"sweep={sq}",
        ]
        return label, composite, trap_risk

    def _select_strong_ob(self, df, side, atr, cfg=None):
        zl, zh, zbar, ztouch = self._find_causal_ob_zone(df, side, atr, cfg)
        ob_score = 0.0
        if zl == 0.0 or zh == 0.0:
            return {
                'grade': 'INVALID',
                'score': ob_score,
                'reason': 'No causal OB found',
                'zone_low': 0.0,
                'zone_high': 0.0,
                'freshness': 999,
                'displacement_atr': 0.0,
                'volume_ratio': 0.0,
                'touches': 0
            }
        freshness = len(df) - zbar if zbar > 0 else 999
        price = float(df['close'].iloc[-1])
        n = len(df)
        lookback_start = max(2, n - 35)
        best_disp = 0.0
        for i in range(lookback_start, n - 2):
            base = df.iloc[i]
            if side == "BUY":
                if float(base['close']) >= float(base['open']):
                    continue
                future = df.iloc[i + 1:min(n, i + 4)]
                if future.empty:
                    continue
                disp = float(future['close'].max()) - float(base['high'])
                if abs(i - zbar) < 2:
                    best_disp = max(best_disp, disp / atr)
            else:
                if float(base['close']) <= float(base['open']):
                    continue
                future = df.iloc[i + 1:min(n, i + 4)]
                if future.empty:
                    continue
                disp = float(base['low']) - float(future['close'].min())
                if abs(i - zbar) < 2:
                    best_disp = max(best_disp, disp / atr)
        vol_avg = float(df['volume'].iloc[max(0, zbar - 10):zbar].mean()) if 'volume' in df else 1.0
        vol_disp = float(df['volume'].iloc[zbar + 1:min(zbar + 4, n)].max()) if 'volume' in df and zbar + 1 < n else vol_avg
        vol_ratio = vol_disp / vol_avg if vol_avg > 0 else 1.0
        is_broken = self._is_order_block_broken_from_zone(df, side, zl, zh, atr)
        if is_broken:
            return {
                'grade': 'INVALID',
                'score': ob_score,
                'reason': 'OB broken',
                'zone_low': zl,
                'zone_high': zh,
                'freshness': freshness,
                'displacement_atr': round(best_disp, 2),
                'volume_ratio': round(vol_ratio, 2),
                'touches': ztouch
            }
        synergy = self._ob_synergy(df, side, atr, zbar, zl, zh, cfg)
        ob_score = min(100.0, self._causal_ob_score(best_disp, vol_ratio, ztouch, freshness) + synergy["bonus"])
        fresh_aplus, fresh_a, fresh_b = (cfg or {}).get("ob_fresh_grade", (10, 20, 40))
        require_sweep = bool((cfg or {}).get("ob_require_sweep_aplus", False))
        require_pd = bool((cfg or {}).get("ob_require_pd_aplus", False))
        sweep_ok = (not require_sweep) or bool(synergy.get("sweep_aligned", False))
        pd_ok = (not require_pd) or bool(synergy.get("pd_aligned", False))
        if (ob_score >= 85 and best_disp > 1.5 and freshness <= fresh_aplus
                and vol_ratio > 1.5 and ztouch <= 1 and sweep_ok and pd_ok):
            grade = "A+"
        elif ob_score >= 75 and best_disp > 0.8 and freshness <= fresh_a and vol_ratio > 1.2:
            grade = "A"
        elif ob_score >= 55 and best_disp > 0.4 and freshness <= fresh_b:
            grade = "B"
        else:
            grade = "INVALID"
        return {
            'grade': grade,
            'score': ob_score,
            'reason': f'OB grade {grade} (score={ob_score:.1f}, disp={best_disp:.2f}ATR, fresh={freshness})',
            'zone_low': zl,
            'zone_high': zh,
            'freshness': freshness,
            'displacement_atr': round(best_disp, 2),
            'volume_ratio': round(vol_ratio, 2),
            'touches': ztouch,
            'synergy': synergy
        }

    def _is_order_block_broken_from_zone(self, df, side, zone_low, zone_high, atr):
        if len(df) < 1:
            return False
        price = float(df['close'].iloc[-1])
        if side == "BUY":
            return price < zone_low - atr * 0.15
        else:
            return price > zone_high + atr * 0.15

    def _is_a_grade(self, cand):
        if not cand.roro_signal:
            return False
        if not cand.strong_ob_present:
            return False
        if cand.zone_metrics.final_zone_score < 75:
            return False
        if cand.zone_metrics.trigger_state not in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED"):
            return False
        if cand.zone_state not in ("ENTRY_WINDOW", "RETEST", "ACTIVE"):
            return False
        if cand.decision_label.startswith("INVALID"):
            return False
        if not cand.evidence.get("rejection_or_displacement", False):
            return False
        if cand.zone_metrics.liquidity_quality < 60:
            return False
        return True

    def _check_entry_conditions(self, df, side, atr):
        if df is None or len(df) < 20:
            return False
        adx = compute_adx(df).iloc[-1] if len(df) >= 20 else 0
        if adx < 18:
            return False
        vol_state = classify_volume(df)
        if vol_state not in ("expansion", "spike", "normal"):
            return False
        price = df['close'].iloc[-1]
        zones = get_smart_zones("", df)
        if side == "BUY" and zones.get("buy_zones"):
            zone_price = zones["buy_zones"][0]["price"]
            if abs(price - zone_price) / price > 0.005:
                return False
        elif side == "SELL" and zones.get("sell_zones"):
            zone_price = zones["sell_zones"][0]["price"]
            if abs(price - zone_price) / price > 0.005:
                return False
        return True

    def _update_state(self, cand, price):
        score = cand.zone_metrics.final_zone_score
        trigger = cand.zone_metrics.trigger_state
        if self._is_a_grade(cand):
            min_confirmations = 1
            cand.decision_label = "A_GRADE_FAST"
        else:
            min_confirmations = 2
            if cand.confirmation_count >= 2:
                cand.decision_label = "NORMAL_CONFIRMED"
            else:
                cand.decision_label = "NORMAL_WAITING"
        in_entry_window = cand.zone_state in ("ENTRY_WINDOW", "RETEST", "ACTIVE")
        label, composite, trap_risk = self._classify_decision(cand)
        cand.decision_label = label
        evidence_ok = not label.startswith("INVALID")
        trigger_gate = trigger in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED")
        conf_ok = cand.confirmation_count >= min_confirmations
        # ---- ATOM INTELLIGENCE GATE (Roro Entry -> Atom Approval) -------
        # When the intelligence layer is live AND has evaluated this candidate,
        # READY additionally requires Atom approval (which embeds the TREND
        # verification and the REVERSAL-only Sniper confirmation) and forbids a
        # hard-rejected (Fake/Broken/Stale/Over-mitigated) zone. If the layer is
        # unavailable or the candidate was not evaluated, we fall back to the
        # legacy gating so the engine never regresses.
        if ATOM_INTELLIGENCE_AVAILABLE and cand.atom_intel:
            atom_gate_ok = bool(cand.atom_approved) and not bool(cand.atom_hard_reject)
            atom_gate_status = ("PASS" if atom_gate_ok and not cand.atom_hard_reject
                                else ("HARD_REJECT" if cand.atom_hard_reject else "FAIL"))
        else:
            atom_gate_ok = True
            atom_gate_status = "N/A"
        gates = {
            "confirmation": "PASS" if conf_ok else f"FAIL({cand.confirmation_count}/{min_confirmations})",
            "score": "PASS" if score >= 65 else f"FAIL({score:.1f}<70)",
            "trigger": "PASS" if trigger_gate else f"FAIL({trigger})",
            "zone_window": "PASS" if in_entry_window else f"FAIL({cand.zone_state})",
            "evidence": "PASS" if evidence_ok else f"FAIL({label})",
            "atom": atom_gate_status,
        }
        blocker = "NONE"
        if not conf_ok:
            blocker = "CONFIRMATION"
        elif cand.atom_hard_reject:
            blocker = "ATOM_HARD_REJECT"
        elif not atom_gate_ok:
            blocker = "ATOM"
        elif not trigger_gate:
            blocker = "TRIGGER"
        elif score < 60:
            blocker = "SCORE"
        elif not in_entry_window:
            blocker = "ZONE_WINDOW"
        elif not evidence_ok:
            blocker = "EVIDENCE"
        cand.gate_status = {"gates": gates, "blocker": blocker, "trigger": trigger,
                            "confirmations": cand.confirmation_count, "score": score,
                            "zone_state": cand.zone_state, "label": label,
                            "min_confirmations": min_confirmations}
        if cand.state in (ExecutionState.WAITING_TRIGGER, ExecutionState.GOOD_ZONE) and not cand.confirmation_logged:
            cand.confirmation_logged = True
            log_execution(f"[CONFIRMATION] {cand.symbol} waiting for trigger (zone_state={cand.zone_state})", "INFO")
        confirmed_triggers = ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED")
        if trigger in confirmed_triggers and not cand.expansion_logged:
            cand.expansion_detected_time = time.time()
            cand.expansion_logged = True
            log_execution(f"[EXPANSION] {cand.symbol} detected | trigger={trigger}", "INFO")
            if cand.institutional_analysis_time > 0:
                latency_analysis = cand.institutional_analysis_time - cand.watchlist_entry_time
                latency_expansion = cand.expansion_detected_time - cand.institutional_analysis_time
                log_execution(
                    f"[LATENCY] {cand.symbol}: watchlist->analysis = {latency_analysis:.2f}s, "
                    f"analysis->expansion = {latency_expansion:.2f}s",
                    "INFO"
                )
            else:
                log_execution(f"[LATENCY] {cand.symbol}: institutional_analysis_time missing", "WARN")
        if conf_ok:
            if score >= 75 and trigger_gate and in_entry_window and evidence_ok and atom_gate_ok:
                was_ready = cand.state == ExecutionState.READY
                cand.state = ExecutionState.READY
                cand.decision = "EARLY_ENTRY" if cand.is_a_grade else "ENTRY"
                cand.decision_reasons.append(f"READY: conf={cand.confirmation_count}/{min_confirmations}")
                if not was_ready:
                    self.gate_stats["ready"] += 1
                    if cand.is_a_grade:
                        self.gate_stats["a_grade_ready"] += 1
                    record_gate_event(cand.symbol, "QUEUE", "READY",
                                      f"score={score:.1f} trigger={trigger} "
                                      f"conf={cand.confirmation_count}/{min_confirmations} "
                                      f"grade={'A' if cand.is_a_grade else 'B'}", cand.side)
            elif score >= 70 and trigger == "MITIGATION" and in_entry_window and evidence_ok:
                cand.state = ExecutionState.ENTRY_VALIDATION
                cand.decision = "WAIT_CONFIRMATION"
            elif score >= 55 and in_entry_window:
                cand.state = ExecutionState.GOOD_ZONE
                cand.decision = "WAIT_CONFIRMATION" if evidence_ok else "WEAK"
            else:
                cand.state = ExecutionState.WATCHLIST
                if not in_entry_window:
                    cand.decision = "LATE"
                elif not evidence_ok:
                    cand.decision = "TRAP_RISK"
        else:
            cand.state = ExecutionState.WAITING_TRIGGER

    def _detect_bos(self, df, lookback=5):
        if len(df) < lookback+2:
            return False, False
        recent_high = df['high'].iloc[-lookback-1:-1].max()
        recent_low = df['low'].iloc[-lookback-1:-1].min()
        close = df['close'].iloc[-1]
        return close > recent_high, close < recent_low

    def _detect_structure_shift(self, df):
        if len(df) < 10:
            return None
        if df['high'].iloc[-3] > df['high'].iloc[-6] and df['low'].iloc[-3] > df['low'].iloc[-6]:
            return "bullish_shift"
        if df['high'].iloc[-3] < df['high'].iloc[-6] and df['low'].iloc[-3] < df['low'].iloc[-6]:
            return "bearish_shift"
        return None

    def _build_liquidity_pools(self, df):
        if len(df) < 10:
            return {"high_pools": [], "low_pools": []}
        highs = df['high'].values
        lows = df['low'].values
        sh = [(i, highs[i]) for i in range(2, len(df)-2) if highs[i] == max(highs[i-2:i+3])]
        sl = [(i, lows[i]) for i in range(2, len(df)-2) if lows[i] == min(lows[i-2:i+3])]
        return {"high_pools": sh[-3:], "low_pools": sl[-3:]}

    def _detect_sweep(self, df, pools):
        n = len(df)
        if n < 2:
            return None, None
        opt = []
        for key in ("high_pools", "low_pools"):
            for idx, level in pools.get(key, []):
                for j in range(1, n):
                    if key == "high_pools" and df['high'].iloc[j] > level and df['high'].iloc[j - 1] <= level:
                        opt.append((n - 1 - j, "high"))
                    elif key == "low_pools" and df['low'].iloc[j] < level and df['low'].iloc[j - 1] >= level:
                        opt.append((n - 1 - j, "low"))
        if not opt:
            return None, None
        age, kind = min(opt, key=lambda t: t[0])
        return (age if kind == "high" else None), (age if kind == "low" else None)

    def _detect_equal_highs_lows(self, df, lookback=50):
        if len(df) < lookback:
            return False, False
        sub = df.iloc[-lookback:]
        highs = sub['high'].values
        lows = sub['low'].values
        sh = [highs[i] for i in range(2, len(sub)-2) if highs[i] == max(highs[i-2:i+3])]
        sl = [lows[i] for i in range(2, len(sub)-2) if lows[i] == min(lows[i-2:i+3])]
        def eq(points, tol=0.002):
            if len(points) < 2:
                return False
            avg = sum(points) / len(points)
            return all(abs(p - avg) / avg < tol for p in points)
        eq_high = eq(sh[-3:]) if len(sh) >= 3 else False
        eq_low = eq(sl[-3:]) if len(sl) >= 3 else False
        return eq_high, eq_low

    def get_best_candidate(self) -> Optional[ExecutionCandidate]:
        with self._lock:
            ready = [c for c in self._candidates.values() if c.state == ExecutionState.READY]
            if ready:
                return max(ready, key=lambda c: c.priority_score)
            # Fallback: when no candidate is READY (e.g. the confirmation gate
            # left a structurally strong candidate at WAITING_TRIGGER because a
            # second confirmation requires the frozen-bars signature to change),
            # still offer the strongest in-queue candidate that already has a
            # confirmed trigger. It is NOT opened blindly: it still goes through
            # the full Entry Quality / safety assessment inside
            # PORTFOLIO.open_candidate -> execute_entry, which remains the
            # authority before any real order. Only candidates that are neither
            # invalidated nor already executed/returned qualify.
            eligible = [
                c for c in self._candidates.values()
                if c.state not in (ExecutionState.EXECUTED, ExecutionState.INVALIDATED,
                                   ExecutionState.RETURNED_WATCHLIST)
                and getattr(getattr(c, "zone_metrics", None), "trigger_state", None)
                in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED")
            ]
            if not eligible:
                return None
            return max(eligible, key=lambda c: c.priority_score)

    def _invalidate(self, symbol, reason):
        if symbol in self._candidates:
            cand = self._candidates[symbol]
            if cand.zone_state == "INVALIDATED":
                self.gate_stats["zone_invalidated"] += 1
            cand.state = ExecutionState.INVALIDATED
            self._candidates.pop(symbol, None)
            self.total_rejected += 1
            log_execution(f"[QUEUE] {symbol} invalidated: {reason}", "WARN")

    def _return_to_watchlist(self, symbol, reason):
        if symbol in self._candidates:
            cand = self._candidates[symbol]
            cand.state = ExecutionState.RETURNED_WATCHLIST
            if cand.zone_state == "STALE":
                self.gate_stats["zone_stale"] += 1
            if cand.zone_state in ("STALE", "EXPANDED_AWAY") and cand.zone_low and cand.zone_high:
                stale = MEMORY.setdefault("stale_zone_refs", {})
                key = f"{symbol}:{cand.side}"
                stale[key] = {
                    "zone_low": cand.zone_low,
                    "zone_high": cand.zone_high,
                    "ts": time.time(),
                }
            log_execution(f"[QUEUE] {symbol} returned to Watchlist: {reason}", "WARN")

    def cleanup(self):
        with self._lock:
            now = time.time()
            to_remove = []
            for symbol, cand in self._candidates.items():
                if cand.state in (ExecutionState.EXECUTED, ExecutionState.INVALIDATED, ExecutionState.RETURNED_WATCHLIST):
                    to_remove.append(symbol)
                elif now - cand.added_at > 3600:
                    self.gate_stats["expired"] += 1
                    record_gate_event(symbol, "QUEUE", "EXPIRED",
                                      f"older than 1h, score={cand.priority_score:.1f}", cand.side)
                    if cand.priority_score >= 40:
                        self._return_to_watchlist(symbol, "Expired")
                    to_remove.append(symbol)
            for sym in to_remove:
                self._candidates.pop(sym, None)

    def get_status(self) -> dict:
        with self._lock:
            return {
                'total_candidates': len(self._candidates),
                'discovered': sum(1 for c in self._candidates.values() if c.state == ExecutionState.DISCOVERED),
                'watchlist': sum(1 for c in self._candidates.values() if c.state == ExecutionState.WATCHLIST),
                'good_zone': sum(1 for c in self._candidates.values() if c.state == ExecutionState.GOOD_ZONE),
                'waiting_trigger': sum(1 for c in self._candidates.values() if c.state == ExecutionState.WAITING_TRIGGER),
                'trigger_detected': sum(1 for c in self._candidates.values() if c.state == ExecutionState.TRIGGER_DETECTED),
                'entry_validation': sum(1 for c in self._candidates.values() if c.state == ExecutionState.ENTRY_VALIDATION),
                'ready': sum(1 for c in self._candidates.values() if c.state == ExecutionState.READY),
                'total_evaluations': self.total_evaluations,
                'total_rejected': self.total_rejected,
                'total_executed': self.total_executed,
                'gate_stats': dict(self.gate_stats),
                'candidates': [c.to_dict() for c in self._candidates.values()],
                'best_score': max([c.priority_score for c in self._candidates.values()]) if self._candidates else 0
            }

queue = ExecutionQueue(max_size=QUEUE_MAX_SIZE, re_eval_interval=QUEUE_RE_EVAL_INTERVAL)

def get_ready_candidates():
    if not USE_EXECUTION_QUEUE:
        return []
    candidates = []
    with queue._lock:
        for cand in queue._candidates.values():
            if cand.state == ExecutionState.READY:
                candidates.append({
                    "symbol": cand.symbol,
                    "side": cand.side,
                    "price": cand.price,
                    "sl": cand.stop_loss,
                    "tp1": cand.take_profit_1,
                    "tp2": cand.take_profit_2,
                    "score": cand.priority_score,
                    "atr": cand.atr,
                    "asset_class": cand.asset_class if hasattr(cand, 'asset_class') else "CRYPTO",
                    "trade_id": cand.candidate_id if hasattr(cand, 'candidate_id') else f"{cand.symbol}_{int(time.time())}",
                })
    return candidates

_last_queue_promote = 0
_last_queue_eval = 0

def process_queue_entry():
    if not USE_EXECUTION_QUEUE:
        return
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        return
    best = queue.get_best_candidate()
    if best is None:
        return
    if best.state != ExecutionState.READY:
        return
    price = best.price
    atr = best.atr
    sl, tp1, tp2 = compute_sl_tp(price, best.side, "REVERSAL", atr, None)
    log_execution(f"[QUEUE] Executing best candidate {best.symbol} {best.side} (score={best.priority_score})", "INFO")
    # Thread the Atom classification (TREND / REVERSAL / SNIPER_REVERSAL) through
    # to position management so the open trade is managed by its real thesis
    # type instead of the generic "QUEUE" label (which always normalized to TREND).
    manage_type = getattr(best, "trade_type", None) or "TREND"
    execute_entry(best.side, best.symbol, price, sl, tp1, tp2, best.priority_score, best.original_reason,
                  atr, manage_type, "EXECUTION_QUEUE", classification="INSTITUTIONAL_SNIPER")

# ========== MAIN EXECUTION ==========
if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=safe_main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False, use_reloader=False)