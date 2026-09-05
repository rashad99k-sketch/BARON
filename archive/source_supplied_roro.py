#!/usr/bin/env python3
# ====================================================================
# RF LIQUIDITY ENGINE v28 – INSTITUTIONAL INTENT EDITION
# [PRODUCTION READY] Institutional Discovery + Dynamic Execution Queue
# ====================================================================
# ENHANCEMENTS (2026-07-29):
# - Institutional Intent Engine (9-layer gatekeeper)
# - Dynamic Trade Management (adaptive SL, multi-stage TP, runner)
# - Watchlist Priority Manager
# - Execution Queue Dynamic Priority
# - Dashboard extensions (Intent, Lifecycle, Probability)
# ====================================================================

import os
import time
import json
import threading
import traceback
import math
import gc
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional, Any
from enum import Enum
from collections import deque
import queue as qlib
import copy

import ccxt
import pandas as pd
import numpy as np
from flask import Flask, jsonify, request
import requests

# ========== FALLBACK LOGGING ==========
if 'log_execution' not in dir():
    def log_execution(msg, level="INFO", debounce_key=None, debounce_sec=60):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}")
        try:
            if 'DASHBOARD_STATE' in globals() and DASHBOARD_STATE is not None:
                DASHBOARD_STATE["logs"].append(f"[{ts}] {msg}")
                if level == "ERROR":
                    DASHBOARD_STATE["errors"].append(f"[{ts}] {msg}")
        except:
            pass

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
        regime = MEMORY.get("regime", "RANGE")
        weights = InstitutionalIntentEngine._get_regime_weights(regime)
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
        details['regime'] = regime
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
PAPER_MODE = os.getenv("PAPER_MODE", "True") == "False"
MODE_LIVE = bool(API_KEY and API_SECRET) and not PAPER_MODE

DEFAULT_SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
INTERVAL = os.getenv("INTERVAL", "15m")
LEVERAGE = 10

USE_PPE = True

# === EXECUTION QUEUE CONFIGURATION (NEW) ===
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
BALANCE_SAFETY_FACTOR = 0.98
INSUFFICIENT_MARGIN_COOLDOWN_SEC = 60

SCAN_INTERVAL = 900
WATCHLIST_REFRESH = 300
RADAR_COOLDOWN_SEC = 1800
LAST_ENTRY_PER_SYMBOL = {}

INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = None

ex = ccxt.bingx({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

def normalize_symbol(symbol):
    if not symbol.endswith(":USDT"):
        return f"{symbol}:USDT"
    return symbol

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
        print(color_text(f"fetch_ohlcv error for {symbol}: {e}", YELLOW))
        return None

def fetch_ohlcv_htf(symbol, timeframe='1h', limit=200):
    try:
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
    return safe_api_call(ex.fetch_ticker, normalize_symbol(symbol))

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

def get_ohlcv_safe(symbol, limit=120, htf=False):
    ttl = 15 if (STATE.get("open") or TRADE_STATE["in_position"]) else 30
    if htf:
        ttl = max(ttl, 45)
    cache_key = f"ohlcv_{symbol}_{INTERVAL}_{limit}_htf" if htf else f"ohlcv_{symbol}_{INTERVAL}_{limit}"
    cached = cache_get("ohlcv", ttl, cache_key)
    if cached is not None:
        if len(cached) >= 100:
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
def fetch_position(symbol):
    if PAPER_MODE:
        return None
    try:
        sym = normalize_symbol(symbol)
        if hasattr(ex, 'fetch_positions'):
            positions = safe_api_call(ex.fetch_positions, [sym])
        elif hasattr(ex, 'fetch_open_positions'):
            positions = safe_api_call(ex.fetch_open_positions, [sym])
        else:
            return None
        if not positions:
            return None
        for pos in positions:
            pos_sym = pos.get('symbol', '')
            if normalize_symbol(symbol) in pos_sym and float(pos.get('contracts', 0)) > 0:
                return pos
        return None
    except Exception as e:
        log_execution(f"[POS_SYNC] fetch_position error: {e}", "ERROR")
        return None

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
        threading.Thread(target=self._process, daemon=True).start()

    def subscribe(self, event_type, handler):
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def emit(self, event_type, data=None):
        self._queue.put((event_type, data))

    def _process(self):
        while self._running:
            try:
                event_type, data = self._queue.get(timeout=0.1)
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

    def fetch_live_snapshot(self, symbol):
        if PAPER_MODE:
            return self._paper_snapshot(symbol)
        try:
            pos = fetch_position(symbol)
            if pos is None:
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
            # Also check if position reduced (for reduceOnly)
            pos = fetch_position(symbol)
            if pos is not None:
                # If we are closing, we expect qty to decrease
                # But we can't infer from here; we just wait.
                pass
            time.sleep(0.5)
        except Exception as e:
            log_execution(f"[ORDER_VERIFY] Error: {e}", "WARN")
            time.sleep(0.5)
    return False, 0

# ========== FIXED: close_partial with robust verification ==========
def close_partial(ratio):
    global _closing_in_progress
    if _closing_in_progress:
        log_execution("[CLOSE_PARTIAL] Already closing, skipping", "WARN")
        return
    _closing_in_progress = True
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
        order = safe_api_call(ex.create_order, sym, "market", side, qty_precise, params={"reduceOnly": True})
        if order is None:
            log_execution("[CLOSE_PARTIAL] Order creation failed (None)", "ERROR")
            return
        order_id = order.get('id')
        if not order_id:
            log_execution("[CLOSE_PARTIAL] No order ID returned", "ERROR")
            return

        filled, filled_qty = verify_order_filled(symbol, order_id, side, qty_precise, timeout=10)
        if filled:
            # Now confirm that the position actually decreased
            # Wait a bit for exchange to update
            time.sleep(1)
            pos = fetch_position(symbol)
            if pos is None:
                # Position gone, meaning we closed all? But partial should leave some.
                log_execution("[CLOSE_PARTIAL] Position vanished unexpectedly", "WARN")
                # Treat as full close
                STATE["open"] = False
                TRADE_STATE["in_position"] = False
                DASHBOARD_STATE["live_trade_mode"] = False
                finalize_trade_with_reality(symbol)
                return
            current_qty = float(pos.get('contracts', 0))
            # We expect current_qty to be roughly remaining_qty - filled_qty
            expected_remaining = STATE["remaining_qty"] - filled_qty
            if abs(current_qty - expected_remaining) < 0.0001 * expected_remaining:
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
            # Attempt to close via market order again? maybe retry later.
    except Exception as e:
        log_execution(f"[CLOSE_PARTIAL] Error: {traceback.format_exc()}", "ERROR")
    finally:
        _closing_in_progress = False

# ========== FIXED: close_position_full with robust verification ==========
def close_position_full():
    global _closing_in_progress
    if _closing_in_progress:
        log_execution("[CLOSE] Already closing, skipping", "WARN")
        return False
    _closing_in_progress = True
    try:
        if PAPER_MODE:
            paper["position"] = None
            STATE["open"] = False
            TRADE_STATE["in_position"] = False
            DASHBOARD_STATE["live_trade_mode"] = False
            finalize_trade_with_reality(STATE["current_symbol"] if STATE.get("current_symbol") else DEFAULT_SYMBOL)
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

        # Retry up to 3 times to close
        for attempt in range(3):
            order = safe_api_call(ex.create_order, sym, "market", side, qty_precise, params={"reduceOnly": True})
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
                # Verify position is zero
                time.sleep(1)  # give exchange time to update
                pos = fetch_position(symbol)
                if pos is None:
                    # Position gone, good.
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
                        # Update remaining qty to current_qty for next attempt
                        qty_to_close = current_qty
                        qty_precise = float(ex.amount_to_precision(sym, qty_to_close))
                        continue
            else:
                log_execution(f"[CLOSE] Order did not fill (attempt {attempt+1})", "ERROR")
                time.sleep(1)
                continue

        # If we exit loop without success, try a last resort: close via exchange position close? (some exchanges support)
        log_execution("[CLOSE] All close attempts failed. Attempting emergency close via position close.", "ERROR")
        try:
            # Some exchanges have explicit position close endpoint, but not all.
            # We'll use a market order with reduceOnly again but with no quantity check.
            order = safe_api_call(ex.create_order, sym, "market", side, qty_precise, params={"reduceOnly": True})
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

# ========== FIXED: apply_50_50_profit_engine without overriding trail_active ==========
def apply_50_50_profit_engine(df, idx, price, atr, side, entry, state, roe_pct, trade_state=None):
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
    
    if not state.get("trail_active", False) and roe_pct >= 1.5:
        state["sl"] = entry
        if side == "BUY":
            state["trail_stop"] = price - 1.2 * atr
        else:
            state["trail_stop"] = price + 1.2 * atr
        state["trail_active"] = True
        log_execution(f"[PPE] {state['symbol']} Stage1: BE SL, trail_active @ 1.2xATR (ROE={roe_pct:.2f}%)", "INFO")
        return "HOLD", state["sl"], state["trail_stop"]
    
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
        self.lifecycle_state = TradeLifecycleState.OPEN_PENDING_CONFIRMATION
        self.event_bus.emit("lifecycle_change", TradeLifecycleState.OPEN_PENDING_CONFIRMATION)
        log_execution(f"[LIFECYCLE] Trade open requested for {symbol} {side}", "INFO")

    def set_entry_atr(self, entry_atr):
        STATE["entry_atr"] = entry_atr
        base_sl_mult = 1.6
        if STATE["side"] == "BUY":
            STATE["synthetic_sl"] = STATE["entry"] - entry_atr * base_sl_mult
        else:
            STATE["synthetic_sl"] = STATE["entry"] + entry_atr * base_sl_mult
        log_execution(f"[SL_FIXED] Initial SL set to {STATE['synthetic_sl']:.4f} based on entry ATR={entry_atr:.4f}", "INFO")

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

    def _apply_runner_defense(self, roe: float, peak_roe: float, drawdown: float,
                               exit_warning: int, continuation_prob: float,
                               trail_mult: float) -> float:
        mult = trail_mult
        if STATE.get("tp1_hit", False):
            mult *= 0.9
            if roe > 50:
                mult *= 0.85
            if drawdown > 12:
                mult *= 0.7
            if exit_warning >= 3:
                mult *= 0.6
            if continuation_prob < 0.55:
                mult *= 0.8
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

            self.last_heavy_calc_ts = now
        else:
            adx_now = STATE.get("adx_live", 20.0)
            plus_di = STATE.get("di_plus_live", 20.0)
            minus_di = STATE.get("di_minus_live", 20.0)
            smart_money = STATE.get("smart_money", {})
            momentum = STATE.get("momentum_flow", {})
            trade_state = STATE.get("trade_state", "RANGE_CHOP")
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
                tp1_price = entry * (1 + 0.04) if side == "BUY" else entry * (1 - 0.04)
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
                                                        base_trail_mult)
            STATE["smart_trail_mult"] = adjusted_mult

            tp2_pct = 0.05
            if continuation_eval.continuation_probability > 0.8:
                tp2_pct = 0.08
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
        trail_mult = max(0.5, min(4.5, trail_mult))

        if roe > 1.5:
            if not STATE.get("trail_activated", False):
                STATE["trail_activated"] = True
                STATE["trail_stop"] = synthetic_sl
                log_execution(f"[TRAIL] Activated with multiplier {trail_mult}", "INFO")
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
                df_live, idx, mark_price, atr, side, entry, state_ppe, roe, trade_state=trade_state
            )
            STATE["synthetic_sl"] = new_sl
            STATE["trail_stop"] = new_trail
            if state_ppe.get("trail_active", False):
                STATE["trail_activated"] = state_ppe["trail_active"]
            if state_ppe.get("tp1_hit", False):
                STATE["tp1_hit"] = state_ppe["tp1_hit"]
            if state_ppe.get("runner_mode", False):
                STATE["runner_mode"] = state_ppe["runner_mode"]
            STATE["max_price"] = state_ppe["max_price"]
            STATE["min_price"] = state_ppe["min_price"]
            if state_ppe.get("profit_lock_activated", False):
                STATE["profit_lock_activated"] = state_ppe["profit_lock_activated"]
            STATE["trail_tightened"] = state_ppe["trail_tightened"]
            TRADE_STATE["trail_on"] = STATE["trail_activated"]
            TRADE_STATE["tp1_hit"] = STATE["tp1_hit"]
            if action == "EXIT":
                close_position_full()
                self.event_bus.emit("lifecycle_change", TradeLifecycleState.CLOSED)
                DASHBOARD_STATE["live_trade_mode"] = False
                return

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
    if df is None or len(df) < period+1:
        return pd.Series([0.0]*len(df))
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

def compute_adx(df, period=14):
    if df is None or len(df) < period*2:
        return pd.Series([0.0]*len(df))
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
    return adx

def compute_rsi(df, period=14):
    if df is None or len(df) < period+1:
        return pd.Series([50.0]*len(df))
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
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def macd_first_flip(hist):
    if len(hist) < 2:
        return False
    return hist.iloc[-2] < 0 and hist.iloc[-1] > 0

def volume_pressure_real(df, window=20, threshold=1.2):
    if len(df) < window + 1:
        return False
    vol = df['volume']
    mean = vol.rolling(window).mean().iloc[-1]
    std = vol.rolling(window).std().iloc[-1]
    if std == 0:
        return False
    z = (vol.iloc[-1] - mean) / std
    return z > threshold

def flow_engine(df):
    if len(df) < 2:
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
    if len(df) < 1 or atr <= 0:
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
    if len(df) < period + 1:
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
    if len(df) < 2:
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
    if len(df) < 1:
        return False
    candle = df.iloc[-1]
    return is_pinbar(candle, atr, side)

def advanced_detect_scenario(df, side, atr, volume_state):
    if len(df) < 3:
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
    if len(df) < lookback+2:
        return False, False
    recent_high = df['high'].iloc[-lookback-1:-1].max()
    recent_low = df['low'].iloc[-lookback-1:-1].min()
    current_close = df['close'].iloc[-1]
    bos_up = current_close > recent_high
    bos_down = current_close < recent_low
    return bos_up, bos_down

def detect_scenario(df):
    if len(df) < 30:
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
    if len(df) < 10:
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
    highs = df['high'].iloc[-lb:]
    lows  = df['low'].iloc[-lb:]
    if highs.max() == highs.min() or lows.max() == lows.min():
        return False, False
    eqh = (highs.max() - highs.min()) / highs.mean() < tol
    eql = (lows.max() - lows.min()) / lows.mean() < tol
    return eqh, eql

def detect_sweep_v2(df, side):
    if len(df) < 22:
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
    if len(df) < 1:
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
    if len(df) < 21:
        return False
    avg_vol = df['volume'].iloc[-21:-1].mean()
    last_vol = df['volume'].iloc[-1]
    return last_vol >= 1.5 * avg_vol

def is_late_entry(df, side):
    if len(df) < 6:
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
    if len(df) < 2:
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
    if len(df) < 2:
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

def volume_engine(df):
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
    if len(df) < 3:
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
    if len(df) < 20:
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
    "entry_atr": 0.0
}
paper = {"balance": 10000.0, "position": None}
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

def log_execution(msg, level="INFO", debounce_key=None, debounce_sec=60):
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

def open_position(side, amount, symbol):
    global _ACTIVE_TRADE
    sym = normalize_symbol(symbol)
    with _TRADE_LOCK:
        if _ACTIVE_TRADE:
            log_execution("[OPEN] Another trade already in progress, skipping", "WARN")
            return None
        _ACTIVE_TRADE = True
    try:
        set_leverage(symbol, LEVERAGE)
        amount = float(ex.amount_to_precision(sym, amount))
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
        order = safe_api_call(ex.create_order, sym, "market", side.lower(), amount, params={"leverage": LEVERAGE})
        if order:
            log_execution(f"[OPEN] Order filled: {side} {amount} {symbol} @ {price}", "SUCCESS")
            return order
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
        order = safe_api_call(ex.create_order, sym, "market", close_side, amount, params={"reduceOnly": True})
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
        if side == "BUY":
            pnl_pct = (mark_price - entry) / entry * 100
        else:
            pnl_pct = (entry - mark_price) / entry * 100
        pnl_usdt = pnl_pct / 100 * entry * STATE["qty"]
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
    tg_close(STATE["current_symbol"], pnl_pct, (time.time() - STATE["entry_time"])/60, STATE["side"])
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
    return pnl_usdt, pnl_pct

def dynamic_spread_tolerance(symbol):
    df = get_ohlcv_safe(symbol, 50)
    if df is None:
        return MAX_SPREAD_PERCENT_DEFAULT
    atr = compute_atr(df).iloc[-1]
    price = df['close'].iloc[-1]
    atr_pct = (atr/price)*100 if price>0 else 0.5
    if atr_pct > 2.0:
        return MAX_SPREAD_PERCENT_VOLATILE
    return MAX_SPREAD_PERCENT_DEFAULT

# ========== RF SCANNER ==========
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

# ========== SMART SCANNER v2 ==========
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

# ========== INSTITUTIONAL LIQUIDITY NARRATIVE ENGINE ==========
def equal_levels_points(highs, lows, tolerance=0.002):
    eq_highs = []
    eq_lows = []
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
    for cl in high_clusters:
        if len(cl) >= 2:
            eq_highs.extend([sum(cl)/len(cl)])
    for cl in low_clusters:
        if len(cl) >= 2:
            eq_lows.extend([sum(cl)/len(cl)])
    return eq_highs, eq_lows

def find_swing_points(df, window=3):
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
    sub = df.iloc[-lookback:]
    sh, sl = find_swing_points(sub, window=2)
    return equal_levels_points(sh, sl)

def detect_order_block(df, side, lookback=4):
    if len(df) < lookback+2:
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
    if len(df) < 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if curr['low'] > prev['high'] * (1+threshold):
        return ("bullish", prev['high'], curr['low'])
    elif curr['high'] < prev['low'] * (1-threshold):
        return ("bearish", curr['high'], prev['low'])
    return None

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

def smart_opportunity_selection():
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
            narrative, nscore = evaluate_liquidity_narrative(df, ob, atr, side)
            smart_money = SmartMoneyEngine.analyze_smart_money(df)
            momentum = MomentumFlowEngine.analyze_momentum_flow(df)
            total_confidence_adjust = 0
            if smart_money["smart_money_dominant"] and smart_money["institutional_bias"] == side:
                total_confidence_adjust += 15
            if momentum["trend_expansion"] and momentum["flow_bias"] == side:
                total_confidence_adjust += 10
            if smart_money["distribution_risk"] > 70:
                total_confidence_adjust -= 15
            if momentum["momentum_decay"]:
                total_confidence_adjust -= 12
            if momentum["exhaustion_risk"] > 70:
                total_confidence_adjust -= 10
            adjusted_nscore = nscore + (total_confidence_adjust / 10)
            record_watchlist_entry(sym, side, narrative, adjusted_nscore, smart_money, momentum)
            if adjusted_nscore < 7:
                continue
            zones = get_smart_zones(sym, df, ob)
            zone_strength = 0
            if side == "BUY" and zones["buy_zones"]:
                zone_strength = zones["buy_zones"][0]["strength"]
            elif side == "SELL" and zones["sell_zones"]:
                zone_strength = zones["sell_zones"][0]["strength"]
            total = adjusted_nscore + zone_strength * 0.5
            if total > best_score:
                best_score = total
                best_setup = (sym, side, total, narrative, zones, df, ob, atr)
        except Exception:
            continue
    if best_setup and best_score >= 9:
        sym, side, score, narrative, zones, df, ob, atr = best_setup
        price = df['close'].iloc[-1]
        leg_class = "REVERSAL"
        sl, tp1, tp2 = compute_sl_tp(price, side, leg_class, atr, df)
        reason_str = f"INST_SWEEP+CHOCH+RETEST | nscore={score:.1f}"
        ok = execute_entry(side, sym, price, sl, tp1, tp2, score, reason_str, atr,
                           trade_type="INSTITUTIONAL", entry_type="NARRATIVE", classification="SNIPER")
        if ok:
            return True
    return False

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
        "last_update": now
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

def cleanup_watchlist(ttl=300):
    now = time.time()
    if "watchlist" not in MEMORY:
        return
    expired = [sym for sym, v in MEMORY["watchlist"].items() if now - v["last_update"] > ttl]
    for sym in expired:
        del MEMORY["watchlist"][sym]

# ========== VWAP ENGINE ==========
def compute_vwap(df):
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
    if df is None or len(df) < period*2:
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

# ========== SMART INSTITUTIONAL ENTRY ENGINE ==========
def check_institutional_entry(symbol, side, df, ob, atr, price):
    # --- NEW: Institutional Intent Gatekeeper ---
    intent_score, intent_status, intent_details = InstitutionalIntentEngine.detect(df, ob, symbol)
    if intent_score < 75:
        log_execution(f"[INTENT] {symbol} {side} intent score {intent_score} < 75 – abort.", "WARN")
        return False, None, f"Intent score {intent_score}"
    MEMORY[f"intent_{symbol}"] = intent_details
    log_execution(f"[INTENT] {symbol} {side} score={intent_score} status={intent_status}", "SUCCESS")

    # --- Original pipeline continues ---
    reasons = []
    pools = build_liquidity_pools(df)
    swept_high, swept_low = detect_sweep(df, pools)
    sweep_ok = (side == "BUY" and swept_low) or (side == "SELL" and swept_high)
    if not sweep_ok:
        return False, None, "No liquidity sweep"
    reasons.append("Sweep")
    zones = get_smart_zones(symbol, df, ob)
    zone_ok = False
    zone_price = None
    if side == "BUY":
        if zones["buy_zones"] and zones["buy_zones"][0]["strength"] >= 5:
            zone_price = zones["buy_zones"][0]["price"]
            if abs(price - zone_price) / price < 0.003:
                zone_ok = True
                reasons.append(f"Buy zone {zone_price:.4f} (strength {zones['buy_zones'][0]['strength']})")
    else:
        if zones["sell_zones"] and zones["sell_zones"][0]["strength"] >= 5:
            zone_price = zones["sell_zones"][0]["price"]
            if abs(price - zone_price) / price < 0.003:
                zone_ok = True
                reasons.append(f"Sell zone {zone_price:.4f} (strength {zones['sell_zones'][0]['strength']})")
    if not zone_ok:
        fvg = detect_fvg(df)
        if side == "BUY" and fvg and fvg[0] == "bullish":
            if price >= fvg[1] and price <= fvg[2]:
                zone_ok = True
                reasons.append("Bullish FVG")
        elif side == "SELL" and fvg and fvg[0] == "bearish":
            if price >= fvg[1] and price <= fvg[2]:
                zone_ok = True
                reasons.append("Bearish FVG")
    if not zone_ok:
        ob_level = detect_order_block(df, side)
        if side == "BUY" and ob_level:
            if abs(price - ob_level["low"]) / price < 0.003:
                zone_ok = True
                reasons.append("Bullish OB")
        elif side == "SELL" and ob_level:
            if abs(price - ob_level["high"]) / price < 0.003:
                zone_ok = True
                reasons.append("Bearish OB")
    if not zone_ok:
        return False, None, "No strong zone tap"
    struct_shift = detect_structure_shift(df)
    bos_up, bos_down = detect_bos(df)
    choch_ok = False
    if side == "BUY" and (struct_shift == "bullish_shift" or bos_up):
        choch_ok = True
        reasons.append("Bullish MSS/CHoCH")
    elif side == "SELL" and (struct_shift == "bearish_shift" or bos_down):
        choch_ok = True
        reasons.append("Bearish MSS/CHoCH")
    if sweep_ok and not choch_ok:
        return False, None, "Reversal requires MSS/CHoCH confirmation"
    elif not sweep_ok and not choch_ok:
        reasons.append("No MSS/CHoCH (trend continuation, optional)")
    rejection_ok = candle_rejection(df, side)
    vol_state = classify_volume(df)
    displacement_ok = detect_displacement(df, side, atr, vol_state, body_atr_threshold=0.8, volume_expansion_required=False)
    if not (rejection_ok or displacement_ok):
        return False, None, "No rejection/displacement candle"
    if rejection_ok:
        reasons.append("Rejection candle")
    if displacement_ok:
        reasons.append("Displacement")
    volume_ok = vol_state in ("expansion", "spike")
    if not volume_ok:
        return False, None, "No volume expansion"
    reasons.append(f"Volume {vol_state}")
    adx_series = compute_adx(df)
    if len(adx_series) < 3:
        return False, None, "Insufficient ADX data"
    adx_now = adx_series.iloc[-1]
    adx_prev = adx_series.iloc[-2]
    adx_slope = adx_now - adx_prev
    plus_di, minus_di, _, _ = get_di_components(df)
    di_spread = (plus_di - minus_di) if side == "BUY" else (minus_di - plus_di)
    if adx_now < 18:
        return False, None, f"ADX too low ({adx_now:.1f})"
    if adx_now > 50:
        if adx_slope > 0 and di_spread > 8:
            reasons.append(f"Strong trend ADX={adx_now:.1f} slope={adx_slope:.1f} DI_spread={di_spread:.1f}")
        else:
            return False, None, f"Exhaustion risk: ADX>50 but slope={adx_slope:.1f} DI_spread={di_spread:.1f}"
    elif adx_now > 35:
        if adx_slope > 0:
            reasons.append(f"Strong trend ADX={adx_now:.1f} slope={adx_slope:.1f}")
        else:
            return False, None, f"ADX high but falling slope ({adx_now:.1f} slope={adx_slope:.1f})"
    else:
        if adx_slope > 0:
            reasons.append(f"Healthy ADX={adx_now:.1f} rising")
        else:
            return False, None, f"ADX not rising ({adx_now:.1f} slope={adx_slope:.1f})"
    rf = RFEngine(20, 3.5).compute(df)
    if rf["signal"] != side:
        return False, None, f"RF signal {rf['signal']} does not match {side}"
    if abs(rf["distance"]) > 0.003:
        return False, None, f"RF distance {rf['distance']:.4f} too far"
    reasons.append("RF aligned")
    if zone_price:
        move_from_zone = abs(price - zone_price) / zone_price * 100
        if move_from_zone > 0.5:
            return False, None, f"Price moved {move_from_zone:.2f}% from zone, too late"
    last_candle = df.iloc[-1]
    candle_range_pct = (last_candle['high'] - last_candle['low']) / last_candle['close'] * 100
    if candle_range_pct > 1.5 * (atr / price * 100):
        return False, None, "Large displacement candle already occurred, too late"
    reason_str = " | ".join(reasons)
    return True, "INSTITUTIONAL_SNIPER", reason_str

# ========== DECISION FUNCTIONS ==========
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

def near_key_zone(df, price):
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    for s in supports:
        if abs(price - s) / price < 0.003:
            return True
    for r in resistances:
        if abs(price - r) / price < 0.003:
            return True
    return False

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
        rf_engine = RFEngine(period=20, multiplier=3.5)
        rf = rf_engine.compute(df)
        if not rf["triggered"]:
            continue
        side = rf["signal"]
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

def log_execution(msg, level="INFO", debounce_key=None, debounce_sec=60):
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

# ===== NEW: Store Intent for Symbol =====
def store_intent_for_symbol(symbol):
    """Fetch OHLCV and orderbook, run InstitutionalIntentEngine.detect, store in MEMORY."""
    try:
        df = get_ohlcv_safe(symbol, 100)
        if df is None or not validate_dataframe(df, 30):
            return
        ob = get_orderbook_cached(symbol, limit=10)
        intent_score, intent_status, intent_details = InstitutionalIntentEngine.detect(df, ob, symbol)
        if intent_score >= 0:
            MEMORY[f"intent_{symbol}"] = {
                "score": intent_score,
                "status": intent_status,
                "details": intent_details
            }
            log_execution(f"[INTENT] Stored for {symbol}: score={intent_score}, status={intent_status}", "INFO", debounce_key=f"intent_store_{symbol}", debounce_sec=60)
    except Exception as e:
        log_execution(f"[INTENT_STORE] Error for {symbol}: {e}", "WARN")

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
                # Store intent for this symbol
                store_intent_for_symbol(sym)
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
                # Refresh intent
                store_intent_for_symbol(sym)
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

def compute_zone_strength(df, level, zone_type, atr, ob):
    price = df['close'].iloc[-1]
    dist_pct = abs(price - level) / price
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

def build_smart_zone_map(symbol, df, ob=None):
    atr = compute_atr(df).iloc[-1]
    supports, resistances = get_clustered_zones(df, lookback=120, cluster_pct=0.002)
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
    return {"buy_zones": buy_zones, "sell_zones": sell_zones}

def get_smart_zones(symbol, df, ob):
    key = f"smart_zones_{symbol}"
    cached = MEMORY.get(key)
    if cached and time.time() - cached.get("ts", 0) < 90:
        return cached["data"]
    zones = build_smart_zone_map(symbol, df, ob)
    MEMORY[key] = {"data": zones, "ts": time.time()}
    return zones

# ========== NEW LIQUIDITY DISCOVERY LAYER ==========
class FreshLiquidityRadar:
    @staticmethod
    def compute_liquidity_score(df):
        if len(df) < 30:
            return 0.0, {}
        score = 0.0
        details = {}
        vol = df['volume']
        vol_accel = vol.iloc[-5:].mean() / (vol.iloc[-10:-5].mean() + 1e-9)
        vol_accel_score = min(2.0, vol_accel - 1.0) if vol_accel > 1.0 else 0.0
        score += vol_accel_score * 2
        details["vol_accel"] = round(vol_accel, 2)
        vol_ratio = vol.iloc[-1] / vol.iloc[-20:].mean()
        vol_exp_score = min(1.5, vol_ratio - 0.8) if vol_ratio > 0.8 else 0.0
        score += vol_exp_score * 1.5
        details["vol_ratio"] = round(vol_ratio, 2)
        atr = compute_atr(df)
        atr_ratio = atr.iloc[-1] / atr.iloc[-20:].mean()
        atr_exp_score = min(1.5, atr_ratio - 0.9) if atr_ratio > 0.9 else 0.0
        score += atr_exp_score * 1.5
        details["atr_ratio"] = round(atr_ratio, 2)
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ > 0:
            body_ratio = body / range_
            displacement = 1.0 if body_ratio > 0.6 else 0.0
            score += displacement * 1.0
            details["displacement"] = displacement
        sweep_count = 0
        for i in range(-5, 0):
            sub_df = df.iloc[:i] if i < 0 else df
            if len(sub_df) >= 2:
                pools = build_liquidity_pools(sub_df)
                swept_h, swept_l = detect_sweep(sub_df, pools)
                if swept_h or swept_l:
                    sweep_count += 1
        sweep_score = min(2.0, sweep_count / 3.0)
        score += sweep_score * 2
        details["sweep_count"] = sweep_count
        adx = compute_adx(df)
        if len(adx) >= 5:
            adx_slope = adx.iloc[-1] - adx.iloc[-4]
            if adx_slope > 0:
                score += min(1.5, adx_slope / 5) * 1.0
                details["adx_slope"] = round(adx_slope, 2)
        final_score = min(10.0, score)
        return final_score, details

    @staticmethod
    def scan(symbols, limit=15):
        candidates = []
        for sym in symbols:
            try:
                df = get_ohlcv_safe(sym, 60)
                if df is None or not validate_dataframe(df, 30):
                    continue
                price = df['close'].iloc[-1]
                atr = compute_atr(df).iloc[-1]
                atr_pct = (atr / price) * 100 if price > 0 else 0
                if atr_pct < 0.2:
                    continue
                score, details = FreshLiquidityRadar.compute_liquidity_score(df)
                if score >= 3.0:
                    candidates.append({
                        "symbol": sym,
                        "score": round(score, 2),
                        "details": details
                    })
            except Exception:
                continue
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

# ========== SECTOR CLASSIFICATION & LEADER SELECTION ==========
SECTOR_MAP = {
    "AI": ["FET", "AGIX", "OCEAN", "RNDR", "TAO", "WLD", "PHB", "CTXC", "NMR", "ORAI"],
    "MEME": ["DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "MEME", "BABYDOGE", "ELON", "SAMO"],
    "LAYER1": ["BTC", "ETH", "SOL", "BNB", "ADA", "AVAX", "TON", "DOT", "ATOM", "NEAR", "ICP", "APT", "SUI", "KAS", "ALGO", "XLM", "VET", "HBAR", "FTM", "EGLD"],
    "LAYER2": ["MATIC", "ARB", "OP", "METIS", "BOBA", "LRC", "SKL", "IMX", "ZK", "POL"],
    "DEFI": ["UNI", "AAVE", "MKR", "COMP", "CRV", "LDO", "SNX", "BAL", "1INCH", "SUSHI", "CAKE", "RUNE", "ENJ", "YFI"],
    "GAMING": ["SAND", "MANA", "GALA", "AXS", "ILV", "YGG", "MAGIC", "PRIME", "GHST", "ALICE", "WAXP", "CROWN"],
    "INFRASTRUCTURE": ["LINK", "GRT", "FIL", "AR", "STORJ", "ANKR", "GNO", "LPT", "HNT", "THETA"],
    "RWA": ["ONDO", "CFG", "RIO", "LNDX", "PRO", "BTRST", "DUSK", "TRU"],
    "PAYMENT": ["XRP", "XLM", "ALGO", "NANO", "XDC", "AMP", "ACH"],
    "PRIVACY": ["ZEC", "XMR", "DASH", "KEEP", "NU", "SCRT", "NYM"],
    "STORAGE": ["FIL", "AR", "STORJ", "BLZ", "SIA", "BTT"]
}

def get_sector(symbol):
    base = symbol.replace("/USDT", "").upper()
    for sector, keywords in SECTOR_MAP.items():
        if any(kw in base for kw in keywords):
            return sector
    return "OTHER"

def get_volume_growth(sym):
    df = get_ohlcv_safe(sym, 30)
    if df is None or len(df) < 20:
        return 0.0
    vol = df['volume']
    recent_avg = vol.iloc[-5:].mean()
    older_avg = vol.iloc[-20:-5].mean()
    if older_avg == 0:
        return 0.0
    return (recent_avg / older_avg) - 1.0

def get_price_momentum(sym):
    df = get_ohlcv_safe(sym, 30)
    if df is None or len(df) < 20:
        return 0.0
    return (df['close'].iloc[-1] - df['close'].iloc[-5]) / df['close'].iloc[-5] * 100

def select_sector_leaders():
    sectors = set(SECTOR_MAP.keys())
    leaders = []
    for sector in sectors:
        symbols_in_sector = [s for s in get_usdt_perp_symbols() if get_sector(s) == sector][:20]
        if not symbols_in_sector:
            continue
        best = None
        best_score = -1e9
        for sym in symbols_in_sector:
            vol_growth = get_volume_growth(sym)
            momentum = get_price_momentum(sym)
            score = vol_growth * 10 + momentum
            if score > best_score:
                best_score = score
                best = sym
        if best:
            leaders.append({"symbol": best, "score": round(best_score, 2), "sector": sector})
    leaders.sort(key=lambda x: x["score"], reverse=True)
    return leaders[:5]

# ========== WATCHLIST ROTATION ENGINE ==========
class WatchlistRotation:
    def __init__(self, symbols_40):
        self.symbols = symbols_40
        self.batch_size = 6
        self.current_index = 0
        self.last_rotate = time.time()
        self.rotation_interval = 30

    def get_next_batch(self):
        batch = []
        for i in range(self.batch_size):
            idx = (self.current_index + i) % len(self.symbols)
            batch.append(self.symbols[idx])
        self.current_index = (self.current_index + self.batch_size) % len(self.symbols)
        self.last_rotate = time.time()
        return batch

    def should_rotate(self):
        return time.time() - self.last_rotate >= self.rotation_interval

def build_40_symbol_universe():
    strong_set = set()
    for c in MEMORY.get("scanner_v2_buy", []) + MEMORY.get("scanner_v2_sell", []):
        strong_set.add(c["symbol"])
    for c in MEMORY.get("radar_top5", []):
        strong_set.add(c["symbol"])
    for c in MEMORY.get("rf_watchlist", []):
        strong_set.add(c["symbol"])
    strong_list = list(strong_set)[:20]
    all_symbols = get_usdt_perp_symbols()
    fresh_radar = FreshLiquidityRadar.scan(all_symbols, limit=20)
    fresh_list = [c["symbol"] for c in fresh_radar if c["symbol"] not in strong_set][:15]
    sector_leaders = select_sector_leaders()
    leader_list = [l["symbol"] for l in sector_leaders if l["symbol"] not in strong_set and l["symbol"] not in fresh_list][:5]
    universe = strong_list + fresh_list + leader_list
    seen = set()
    unique_universe = []
    for sym in universe:
        if sym not in seen:
            seen.add(sym)
            unique_universe.append(sym)
    if len(unique_universe) < 40:
        extra = [s for s in all_symbols if s not in seen][:40 - len(unique_universe)]
        unique_universe.extend(extra)
    return unique_universe[:40]

# ========== EXECUTION QUEUE INTEGRATION (NEW) ==========
# Data structures for the queue
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
    BOS = "BOS"              # Break of Structure
    CHOCH = "CHOCH"          # Change of Character
    MSS = "MSS"              # Market Structure Shift
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

    @property
    def final_zone_score(self) -> float:
        weights = {
            'order_block_quality': 0.20,
            'zone_strength': 0.18,
            'liquidity_quality': 0.15,
            'institutional_confidence': 0.15,
            'structure_alignment': 0.12,
            'entry_timing': 0.10,
            'trend_alignment': 0.05,
            'risk_score': 0.05
        }
        score = 0.0
        for attr, w in weights.items():
            score += getattr(self, attr, 50) * w
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

    original_score: float = 0.0
    original_reason: str = ""
    signal_type: str = ""

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'side': self.side,
            'entry_price': self.entry_price,
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
            'institutional': self.zone_metrics.institutional_confidence,
            'structure': self.zone_metrics.structure_alignment,
            'timing': self.zone_metrics.entry_timing,
            'trend': self.zone_metrics.trend_alignment,
            'risk': self.zone_metrics.risk_score,
            'evaluation_count': self.evaluation_count,
            'last_update': self.last_evaluated
        }

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

    def add_candidate(self, candidate: ExecutionCandidate) -> bool:
        with self._lock:
            if len(self._candidates) >= self._max_size:
                # Remove lowest priority candidate that is not READY
                lowest = min(
                    [(s, c) for s, c in self._candidates.items() if c.state != ExecutionState.READY],
                    key=lambda x: x[1].priority_score,
                    default=None
                )
                if lowest:
                    self._candidates.pop(lowest[0])
                    self.total_rejected += 1
                else:
                    return False  # cannot add more

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
                df = data_fetcher(symbol)
                if df is None or len(df) < 30:
                    self._invalidate(symbol, "Insufficient data")
                    continue

                current_price = df['close'].iloc[-1]
                atr = compute_atr(df).iloc[-1] if len(df) > 14 else current_price * 0.01
                cand.atr = atr

                # 1. Reject if price extended >1.5 ATR from entry zone
                if self._is_extended(cand, current_price):
                    self._return_to_watchlist(symbol, "Price extended")
                    continue

                # 2. Check if order block broken
                if self._is_order_block_broken(cand, current_price):
                    self._invalidate(symbol, "Order block broken")
                    continue

                # Evaluate dimensions
                ob_score, ob_type = self._evaluate_order_block(df, cand.side, atr)
                zone_score = self._evaluate_zone_strength(df, cand.side, atr, cand.entry_price)
                liq_score = self._evaluate_liquidity(df, cand.side, atr)
                inst_score = self._evaluate_institutional(df, cand.side)
                struct_score, struct_type = self._evaluate_structure(df, cand.side)
                timing_score = self._evaluate_timing(df, cand.side, atr, current_price, cand.entry_price)
                trend_score = self._evaluate_trend_alignment(df, cand.side)
                risk_score = self._evaluate_risk(cand, current_price)

                # Detect trigger state
                trigger_state = self._detect_trigger_state(df, cand.side, atr, cand.entry_price)

                metrics = ZoneMetrics(
                    order_block_quality=ob_score,
                    zone_strength=zone_score,
                    liquidity_quality=liq_score,
                    institutional_confidence=inst_score,
                    structure_alignment=struct_score,
                    entry_timing=timing_score,
                    trend_alignment=trend_score,
                    risk_score=risk_score,
                    trigger_state=trigger_state
                )

                opp_type = self._classify_opportunity(metrics, struct_type, cand.side, df)
                behaviour = self._detect_institutional_behaviour(df, cand.side)

                cand.zone_metrics = metrics
                cand.opportunity_type = opp_type
                cand.market_structure = struct_type
                cand.institutional_behaviour = behaviour
                cand.last_evaluated = time.time()
                cand.evaluation_count += 1
                cand.priority_score = metrics.final_zone_score

                self._update_state(cand, current_price)

                self.total_evaluations += 1

                # If priority too low, return to watchlist
                if cand.priority_score < 30:
                    self._return_to_watchlist(symbol, "Score too low")

    # ---------- Evaluation methods ----------
    def _is_extended(self, cand, price):
        return abs(price - cand.entry_price) > cand.atr * 1.5

    def _is_order_block_broken(self, cand, price):
        if cand.side == "BUY":
            return price < cand.entry_price - cand.atr * 0.8
        else:
            return price > cand.entry_price + cand.atr * 0.8

    def _evaluate_order_block(self, df, side, atr):
        if len(df) < 1:
            return 30, OrderBlockQuality.WEAK
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ == 0:
            return 30, OrderBlockQuality.WEAK

        if side == "BUY":
            lower_wick = min(last['open'], last['close']) - last['low']
            ratio = lower_wick / range_
            if ratio > 0.6 and last['close'] > last['open']:
                return 90, OrderBlockQuality.FRESH
            elif ratio > 0.4:
                return 70, OrderBlockQuality.TESTED
            else:
                return 50, OrderBlockQuality.WEAK
        else:
            upper_wick = last['high'] - max(last['open'], last['close'])
            ratio = upper_wick / range_
            if ratio > 0.6 and last['close'] < last['open']:
                return 90, OrderBlockQuality.FRESH
            elif ratio > 0.4:
                return 70, OrderBlockQuality.TESTED
            else:
                return 50, OrderBlockQuality.WEAK

    def _evaluate_zone_strength(self, df, side, atr, entry_price):
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

    def _evaluate_liquidity(self, df, side, atr):
        pools = self._build_liquidity_pools(df)
        swept_high, swept_low = self._detect_sweep(df, pools)
        stop_hunt, hunt_side = self._detect_stop_hunt(df)
        eq_highs, eq_lows = self._detect_equal_highs_lows(df)

        score = 50
        if side == "BUY":
            if swept_low: score += 25
            if eq_lows: score += 10
            if stop_hunt and hunt_side == "BUY": score += 20
        else:
            if swept_high: score += 25
            if eq_highs: score += 10
            if stop_hunt and hunt_side == "SELL": score += 20
        return min(100, max(0, score))

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
        dist = abs(current_price - entry_price) / entry_price
        score = 50
        if dist < 0.005:
            score += 30
        elif dist < 0.015:
            score += 15
        elif dist > 0.03:
            score -= 30

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
        """Determine the trigger state based on institutional price action."""
        # Check for sweep
        pools = self._build_liquidity_pools(df)
        swept_high, swept_low = self._detect_sweep(df, pools)
        sweep_ok = (side == "BUY" and swept_low) or (side == "SELL" and swept_high)

        # Check BOS / CHoCH
        bos_up, bos_down = self._detect_bos(df)
        struct_shift = self._detect_structure_shift(df)
        bos_ok = (side == "BUY" and bos_up) or (side == "SELL" and bos_down)
        choch_ok = (side == "BUY" and struct_shift == "bullish_shift") or (side == "SELL" and struct_shift == "bearish_shift")

        # Check rejection / engulfing
        rejection_ok = candle_rejection(df, side)
        # Check displacement
        vol_state = classify_volume(df)
        displacement_ok = detect_displacement(df, side, atr, vol_state, body_atr_threshold=0.8, volume_expansion_required=False)

        # Check if price is near entry (zone mitigation)
        dist = abs(df['close'].iloc[-1] - entry_price) / entry_price
        near_entry = dist < 0.003

        # Determine state
        if sweep_ok and (bos_ok or choch_ok) and rejection_ok:
            return "MSS_CONFIRMED"
        elif sweep_ok and near_entry and rejection_ok:
            return "LIQUIDITY_SWEEP"
        elif bos_ok and displacement_ok:
            return "BOS_CONFIRMED"
        elif choch_ok and displacement_ok:
            return "CHOCH_CONFIRMED"
        elif sweep_ok and not (bos_ok or choch_ok):
            return "MITIGATION"
        elif near_entry and (bos_ok or choch_ok):
            return "WAITING_TRIGGER"
        elif near_entry:
            return "MITIGATION"
        elif displacement_ok:
            return "DISPLACEMENT"
        else:
            return "WAITING_TRIGGER"

    def _update_state(self, cand, price):
        score = cand.zone_metrics.final_zone_score
        trigger = cand.zone_metrics.trigger_state

        if score >= 85 and trigger in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED"):
            cand.state = ExecutionState.READY
        elif score >= 70 and trigger == "MITIGATION":
            cand.state = ExecutionState.ENTRY_VALIDATION
        elif score >= 70:
            cand.state = ExecutionState.WAITING_TRIGGER
        elif score >= 55:
            cand.state = ExecutionState.GOOD_ZONE
        else:
            cand.state = ExecutionState.WATCHLIST

    # ---------- Helper structure detection (using existing functions) ----------
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
        sh = [highs[i] for i in range(2, len(df)-2) if highs[i] == max(highs[i-2:i+3])]
        sl = [lows[i] for i in range(2, len(df)-2) if lows[i] == min(lows[i-2:i+3])]
        return {"high_pools": sh[-3:], "low_pools": sl[-3:]}

    def _detect_sweep(self, df, pools):
        if len(df) < 2:
            return False, False
        last, prev = df.iloc[-1], df.iloc[-2]
        swept_high = any(last['high'] > h and prev['high'] <= h for h in pools['high_pools'])
        swept_low = any(last['low'] < l and prev['low'] >= l for l in pools['low_pools'])
        return swept_high, swept_low

    def _detect_stop_hunt(self, df):
        pools = self._build_liquidity_pools(df)
        swept_high, swept_low = self._detect_sweep(df, pools)
        last = df.iloc[-1]
        if swept_high and last['close'] < last['high']:
            return True, "SELL"
        if swept_low and last['close'] > last['low']:
            return True, "BUY"
        return False, None

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

    # ---------- Queue management ----------
    def get_best_candidate(self) -> Optional[ExecutionCandidate]:
        with self._lock:
            ready = [c for c in self._candidates.values() if c.state == ExecutionState.READY]
            if not ready:
                return None
            return max(ready, key=lambda c: c.priority_score)

    def _invalidate(self, symbol, reason):
        if symbol in self._candidates:
            self._candidates[symbol].state = ExecutionState.INVALIDATED
            self._candidates.pop(symbol, None)
            self.total_rejected += 1
            log_execution(f"[QUEUE] {symbol} invalidated: {reason}", "WARN")

    def _return_to_watchlist(self, symbol, reason):
        if symbol in self._candidates:
            self._candidates[symbol].state = ExecutionState.RETURNED_WATCHLIST
            log_execution(f"[QUEUE] {symbol} returned to Watchlist: {reason}", "WARN")

    def cleanup(self):
        with self._lock:
            now = time.time()
            to_remove = []
            for symbol, cand in self._candidates.items():
                if cand.state in (ExecutionState.EXECUTED, ExecutionState.INVALIDATED, ExecutionState.RETURNED_WATCHLIST):
                    to_remove.append(symbol)
                elif now - cand.added_at > 3600:  # 1 hour expiry
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
                'candidates': [c.to_dict() for c in self._candidates.values()],
                'best_score': max([c.priority_score for c in self._candidates.values()]) if self._candidates else 0
            }

# Global queue instance
queue = ExecutionQueue(max_size=QUEUE_MAX_SIZE, re_eval_interval=QUEUE_RE_EVAL_INTERVAL)
_last_queue_promote = 0
_last_queue_eval = 0

# ========== GLOBAL DISCOVERY SCANNER (NEW) ==========
def global_discovery_scan():
    """Scan entire market every 20 minutes, update watchlist with top candidates."""
    log_execution("[DISCOVERY] Starting global discovery scan...", "INFO")
    start_time = time.time()
    all_symbols = get_usdt_perp_symbols()[:200]
    candidates = []

    # 1. Smart Scanner v2 (existing)
    buy, sell = smart_scanner_v2()
    for b in buy[:5]:
        candidates.append({"symbol": b["symbol"], "score": b["score"], "side": "BUY", "source": "scanner_v2"})
        store_intent_for_symbol(b["symbol"])
    for s in sell[:5]:
        candidates.append({"symbol": s["symbol"], "score": s["score"], "side": "SELL", "source": "scanner_v2"})
        store_intent_for_symbol(s["symbol"])

    # 2. RF Scanner
    rf_candidates = scan_market_rf(top_n=20)
    for r in rf_candidates[:10]:
        side = r.get("rf_signal")
        if side in ("BUY", "SELL"):
            candidates.append({"symbol": r["symbol"], "score": r["score"]*10, "side": side, "source": "rf"})
            store_intent_for_symbol(r["symbol"])

    # 3. Fresh Liquidity Radar
    fresh = FreshLiquidityRadar.scan(all_symbols, limit=15)
    for f in fresh:
        candidates.append({"symbol": f["symbol"], "score": f["score"]*2, "side": "BUY", "source": "fresh"})
        candidates.append({"symbol": f["symbol"], "score": f["score"]*2, "side": "SELL", "source": "fresh"})
        store_intent_for_symbol(f["symbol"])

    # 4. Random discovery (10% of symbols)
    random.shuffle(all_symbols)
    for sym in all_symbols[:10]:
        if not any(c["symbol"] == sym for c in candidates):
            candidates.append({"symbol": sym, "score": 0, "side": "BUY", "source": "random"})
            candidates.append({"symbol": sym, "score": 0, "side": "SELL", "source": "random"})
            store_intent_for_symbol(sym)

    # Sort by score, keep top 40
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = candidates[:40]

    # Update watchlist (MEMORY["watchlist"])
    for item in top_candidates:
        sym = item["symbol"]
        side = item["side"]
        narrative = {
            "sweep": False,
            "choch_bos": False,
            "retest": False,
            "rejection": False,
            "displacement": False,
            "volume_confirmation": False,
            "rf_alignment": False
        }
        if item["source"] == "scanner_v2":
            narrative["sweep"] = True
        elif item["source"] == "rf":
            narrative["rf_alignment"] = True
        elif item["source"] == "fresh":
            narrative["volume_confirmation"] = True
        record_watchlist_entry(sym, side, narrative, item["score"])
        # Already stored intent above; but ensure it's stored for all
        store_intent_for_symbol(sym)

    # Also update radar_top5 for compatibility
    radar_top = [{"symbol": c["symbol"], "score": c["score"]} for c in top_candidates[:5]]
    MEMORY["radar_top5"] = radar_top

    elapsed = time.time() - start_time
    log_execution(f"[DISCOVERY] Scan completed in {elapsed:.1f}s, {len(top_candidates)} candidates added to watchlist.", "INFO")

def promote_to_queue():
    """Scan watchlist and add high-potential symbols to the execution queue."""
    if not USE_EXECUTION_QUEUE:
        return
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        return

    watchlist = []
    # Combine from multiple watchlist sources
    for source in (MEMORY.get("watchlist", {}).values(),
                   MEMORY.get("rf_watchlist", []),
                   MEMORY.get("scanner_v2_buy", []),
                   MEMORY.get("scanner_v2_sell", [])):
        if isinstance(source, dict):
            # if it's a dict of entries
            for item in source.values():
                if isinstance(item, dict) and "symbol" in item:
                    watchlist.append(item)
        elif isinstance(source, list):
            for item in source:
                if isinstance(item, dict) and "symbol" in item:
                    watchlist.append(item)

    best_per_symbol = {}
    for item in watchlist:
        sym = item.get('symbol')
        if not sym:
            continue
        score = item.get('score', 0)
        side = item.get('side', 'BUY')
        if sym not in best_per_symbol or score > best_per_symbol[sym]['score']:
            best_per_symbol[sym] = {'score': score, 'side': side, 'source': item.get('source', 'unknown')}

    sorted_items = sorted(best_per_symbol.items(), key=lambda x: x[1]['score'], reverse=True)

    for sym, data in sorted_items[:30]:
        if sym in queue._candidates:
            continue

        df = get_ohlcv_safe(sym, 100)
        if df is None or len(df) < 30:
            continue

        price = df['close'].iloc[-1]
        atr = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
        ob = get_orderbook_cached(sym, limit=10)

        side = data.get('side', 'BUY')
        sl, tp1, tp2 = compute_sl_tp(price, side, "REVERSAL", atr, df)

        # --- NEW: Use IntentScore for candidate priority ---
        intent_score, _, _ = InstitutionalIntentEngine.detect(df, ob, sym)
        metrics = ZoneMetrics()
        candidate = ExecutionCandidate(
            symbol=sym,
            side=side,
            price=price,
            entry_price=price,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            atr=atr,
            df=df,
            ob=ob,
            zone_metrics=metrics,
            original_score=data.get('score', 0),
            original_reason=data.get('reason', 'Watchlist promotion'),
            signal_type=data.get('source', 'watchlist')
        )
        candidate.priority_score = intent_score  # Set priority to intent score
        queue.add_candidate(candidate)
        log_execution(f"[QUEUE] Promoted {sym} {side} from watchlist (Intent: {intent_score:.1f})", "INFO", debounce_key=f"promote_{sym}", debounce_sec=60)

def process_queue_entry():
    """Select best candidate and attempt entry via existing execute_entry."""
    if not USE_EXECUTION_QUEUE:
        return
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        return

    best = queue.get_best_candidate()
    if best is None:
        return

    if best.priority_score < 80:
        return

    log_execution(f"[QUEUE] Attempting entry for {best.symbol} {best.side} (Score: {best.priority_score:.1f})", "INFO")
    success = execute_entry(
        best.side,
        best.symbol,
        best.price,
        best.stop_loss,
        best.take_profit_1,
        best.take_profit_2,
        best.original_score,
        f"QUEUE: {best.opportunity_type.value} (Zone Score: {best.zone_metrics.final_zone_score})",
        best.atr,
        best.opportunity_type.value,
        "EXECUTION_QUEUE",
        best.opportunity_type.value
    )
    if success:
        with queue._lock:
            if best.symbol in queue._candidates:
                queue._candidates[best.symbol].state = ExecutionState.EXECUTED
        queue.total_executed += 1
        log_execution(f"[QUEUE] Trade executed for {best.symbol}", "SUCCESS")

# ========== FLASK DASHBOARD ==========
app = Flask(__name__)

def update_position_dashboard(symbol, side, entry, qty, pnl=0.0):
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

def clear_position_dashboard():
    DASHBOARD_STATE["position"] = None

def render_live_supervisor_panel():
    return """
    <div id="rf-live-panel" style="display:none;" class="rf-live-supervisor">
      <div class="rf-live-header">
        <span class="rf-live-title">🧠 RF v28 Fixed Live Supervisor</span>
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
    
    # ===== NEW PANELS: INTENT ENGINE & DYNAMIC TRADE =====
    intent_panel_html = """
    <div class="section smart-layer">
      <div class="title">🔮 Institutional Intent Engine (9 Layers)</div>
      <div class="grid" style="grid-template-columns: repeat(4,1fr);">
        <div class="card">Score<div id="intent-score" class="green">-</div></div>
        <div class="card">Status<div id="intent-status">-</div></div>
        <div class="card">Liquidity<div id="intent-liq">-</div></div>
        <div class="card">Absorption<div id="intent-abs">-</div></div>
        <div class="card">Volatility<div id="intent-vol">-</div></div>
        <div class="card">Flow<div id="intent-flow">-</div></div>
        <div class="card">Structure<div id="intent-struct">-</div></div>
        <div class="card">Momentum<div id="intent-mom">-</div></div>
        <div class="card">Volume<div id="intent-vol-ctx">-</div></div>
        <div class="card">Narrative<div id="intent-narr">-</div></div>
        <div class="card">Regime Weights<div id="intent-weights">-</div></div>
      </div>
    </div>
    """
    
    dynamic_trade_panel_html = """
    <div class="section smart-layer">
      <div class="title">⚡ Dynamic Trade Management</div>
      <div class="grid" style="grid-template-columns: repeat(4,1fr);">
        <div class="card">Current ROE<div id="dyn-roe">-</div></div>
        <div class="card">Trailing Active<div id="dyn-trail">❌</div></div>
        <div class="card">TP1 Hit<div id="dyn-tp1">❌</div></div>
        <div class="card">TP2 Hit<div id="dyn-tp2">❌</div></div>
        <div class="card">Runner Active<div id="dyn-runner">❌</div></div>
        <div class="card">Drawdown<div id="dyn-dd">0.0%</div></div>
        <div class="card">Lifecycle<div id="dyn-lifecycle">-</div></div>
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
    
    # Execution Queue Panel (NEW)
    queue_panel_html = """
    <div class="section smart-layer" id="queue-panel" style="display: none;">
        <div class="title">🎯 EXECUTION QUEUE – Institutional Zone Analysis</div>
        <div id="queue-summary" class="grid" style="grid-template-columns: repeat(6,1fr); margin-bottom:10px;">
            <div class="card">Total<div id="q-total">0</div></div>
            <div class="card">Ready<div id="q-ready" class="green">0</div></div>
            <div class="card">Waiting Trigger<div id="q-waiting" class="orange">0</div></div>
            <div class="card">Good Zone<div id="q-good-zone" class="blue">0</div></div>
            <div class="card">Returned<div id="q-returned" class="grey">0</div></div>
            <div class="card">Best Score<div id="q-best-score">0</div></div>
        </div>
        <div id="queue-table" style="max-height:400px; overflow-y:auto; font-size:12px;">
            <table style="width:100%; border-collapse:collapse; background:#0f1724; border-radius:8px; overflow:hidden;">
                <thead>
                    <tr style="background:#1a2332; color:#9ca3af; text-align:center;">
                        <th>Symbol</th><th>Side</th><th>Score</th><th>OB</th><th>Zone</th><th>Liq</th><th>Inst</th>
                        <th>Struct</th><th>Timing</th><th>Trend</th><th>Risk</th><th>Trigger</th><th>Type</th><th>State</th>
                    </tr>
                </thead>
                <tbody id="queue-body">
                    <!-- rows populated by JavaScript -->
                </tbody>
            </table>
        </div>
    </div>
    """
    
    supervisor_panel_html = render_live_supervisor_panel()
    
    html = f"""
<!DOCTYPE html>
<html><head><title>RF v28 Fixed Live Supervisor</title>
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
.blue{{color:#3498db}}
.orange{{color:#f1c40f}}
.grey{{color:#95a5a6}}
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
<div class="header">🔥 RF v28 Fixed Live Supervisor</div>
{decision_panel_html}
{scanner_v2_section}
{queue_panel_html}
{supervisor_panel_html}
{intent_panel_html}
{dynamic_trade_panel_html}
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
    if (cachedData && (now - lastFetch) < 5000) {{
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
              <br><small>Last update: ${{lastUpdate}} ${{extraInfo}}</small>
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
    // Execution Queue panel (NEW)
    if (d.queue && d.queue.enabled !== false) {{
        document.getElementById("queue-panel").style.display = "block";
        document.getElementById("q-total").innerText = d.queue.total;
        document.getElementById("q-ready").innerText = d.queue.ready;
        document.getElementById("q-waiting").innerText = (d.queue.candidates || []).filter(c => c.state === "WAITING_TRIGGER").length;
        document.getElementById("q-good-zone").innerText = (d.queue.candidates || []).filter(c => c.state === "GOOD_ZONE" || c.state === "ENTRY_VALIDATION").length;
        document.getElementById("q-returned").innerText = (d.queue.candidates || []).filter(c => c.state === "RETURNED_WATCHLIST").length;
        document.getElementById("q-best-score").innerText = d.queue.best_score ? d.queue.best_score.toFixed(1) : "0";
        let body = document.getElementById("queue-body");
        body.innerHTML = "";
        (d.queue.candidates || []).slice(0, 15).forEach(c => {{
            let tr = document.createElement("tr");
            tr.style.borderBottom = "1px solid #2c3e50";
            let stateColor = "";
            if (c.state === "READY") stateColor = "#2ecc71";
            else if (c.state === "ENTRY_VALIDATION") stateColor = "#3498db";
            else if (c.state === "WAITING_TRIGGER") stateColor = "#f1c40f";
            else if (c.state === "GOOD_ZONE") stateColor = "#3498db";
            else if (c.state === "MITIGATION") stateColor = "#e67e22";
            else if (c.state === "INVALIDATED" || c.state === "RETURNED_WATCHLIST") stateColor = "#95a5a6";
            else stateColor = "#ecf0f1";
            let triggerState = c.trigger_state || "WAITING_TRIGGER";
            let triggerColor = triggerState === "MSS_CONFIRMED" ? "#2ecc71" :
                               triggerState === "LIQUIDITY_SWEEP" ? "#3498db" :
                               triggerState === "BOS_CONFIRMED" ? "#9b59b6" :
                               triggerState === "CHOCH_CONFIRMED" ? "#1abc9c" :
                               triggerState === "MITIGATION" ? "#f1c40f" :
                               "#95a5a6";
            tr.innerHTML = `
                <td><b>${{c.symbol}}</b></td>
                <td style="color:${{c.side === 'BUY' ? '#2ecc71' : '#e74c3c'}}">${{c.side}}</td>
                <td style="font-weight:bold; color:${{c.zone_score >= 80 ? '#2ecc71' : c.zone_score >= 60 ? '#f1c40f' : '#e74c3c'}}">${{c.zone_score.toFixed(1)}}</td>
                <td>${{c.ob_score.toFixed(0)}}</td>
                <td>${{c.zone_strength.toFixed(0)}}</td>
                <td>${{c.liquidity.toFixed(0)}}</td>
                <td>${{c.institutional.toFixed(0)}}</td>
                <td>${{c.structure.toFixed(0)}}</td>
                <td>${{c.timing.toFixed(0)}}</td>
                <td>${{c.trend.toFixed(0)}}</td>
                <td>${{c.risk.toFixed(0)}}</td>
                <td style="color:${{triggerColor}}; font-weight:bold;">${{triggerState}}</td>
                <td style="font-size:10px;">${{c.opportunity_type}}</td>
                <td style="color:${{stateColor}}; font-weight:bold;">${{c.state}}</td>
            `;
            body.appendChild(tr);
        }});
    }} else {{
        document.getElementById("queue-panel").style.display = "none";
    }}
    // === NEW: Intent Engine Panel ===
    if (d.intent_engine) {{
        const ie = d.intent_engine;
        document.getElementById("intent-score").innerText = ie.score || 0;
        document.getElementById("intent-status").innerText = ie.status || "NEUTRAL";
        if (ie.details) {{
            document.getElementById("intent-liq").innerText = ie.details.liquidity_score || "-";
            document.getElementById("intent-abs").innerText = ie.details.absorption_score || "-";
            document.getElementById("intent-vol").innerText = ie.details.volatility_score || "-";
            document.getElementById("intent-flow").innerText = ie.details.flow_score || "-";
            document.getElementById("intent-struct").innerText = ie.details.structure_score || "-";
            document.getElementById("intent-mom").innerText = ie.details.momentum_score || "-";
            document.getElementById("intent-vol-ctx").innerText = ie.details.volume_score || "-";
            document.getElementById("intent-narr").innerText = ie.details.narrative || "-";
            if (ie.details.regime_weights) {{
                const w = ie.details.regime_weights;
                if (w) {{
                    let wStr = `Liq:${{w.liquidity}} Abs:${{w.absorption}} Vol:${{w.volatility}} Fl:${{w.institutional_flow}} Struct:${{w.structure}} Mom:${{w.momentum}} VolCtx:${{w.volume}} Narr:${{w.narrative}}`;
                    document.getElementById("intent-weights").innerText = wStr;
                }}
            }}
        }}
    }}
    // === NEW: Dynamic Trade Management ===
    if (d.dynamic_trade) {{
        const dt = d.dynamic_trade;
        document.getElementById("dyn-roe").innerHTML = dt.roe?.toFixed(2) + "%" || "0.00%";
        document.getElementById("dyn-trail").innerHTML = dt.trailing_active ? "✅" : "❌";
        document.getElementById("dyn-tp1").innerHTML = dt.tp1_hit ? "✅" : "❌";
        document.getElementById("dyn-tp2").innerHTML = dt.tp2_hit ? "✅" : "❌";
        document.getElementById("dyn-runner").innerHTML = dt.runner_active ? "✅" : "❌";
        document.getElementById("dyn-dd").innerHTML = dt.drawdown?.toFixed(1) + "%" || "0.0%";
        document.getElementById("dyn-lifecycle").innerText = dt.lifecycle || "N/A";
    }}
}}
async function manualTrade(side){{ const r=await fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:side}})}}); const res=await r.json(); alert(res.message); }}
async function manualClose(){{ const r=await fetch('/close',{{method:'POST'}}); const res=await r.json(); alert(res.message); }}
setInterval(fetchData, 6000);
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
setInterval(loadDecision, 6000);
loadDecision();
fetchData();
</script>
</body></html>
"""
    return html

@app.route("/data")
def data():
    cached = cache_get("dashboard", 5)
    if cached is not None:
        return jsonify(safe_json(cached))
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
                "delay_tp1": STATE.get("delay_tp1", False)
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
                "delay_tp1": STATE.get("delay_tp1", False)
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

        # --- NEW: Intent Engine data ---
        intent_data = {}
        # Get current symbol or first radar symbol
        target_symbol = STATE.get("current_symbol")
        if not target_symbol:
            # pick first from radar_top5 or default
            radar = MEMORY.get("radar_top5", [])
            if radar:
                target_symbol = radar[0].get("symbol")
            else:
                target_symbol = DEFAULT_SYMBOL

        # Retrieve stored intent for that symbol
        if target_symbol:
            intent_data = MEMORY.get(f"intent_{target_symbol}", {})
            # If no data, try to compute on the fly
            if not intent_data:
                store_intent_for_symbol(target_symbol)
                intent_data = MEMORY.get(f"intent_{target_symbol}", {})
        # Fallback: if still empty, show a neutral placeholder
        if not intent_data:
            intent_data = {"score": 0, "status": "NEUTRAL", "details": {}}

        # --- NEW: Dynamic Trade data ---
        dynamic_trade_data = {}
        if STATE.get("open") and "dynamic_manager" in STATE:
            mgr = STATE["dynamic_manager"]
            dynamic_trade_data = {
                "roe": mgr.calculate_roe(STATE.get("mark_price", STATE["entry"])),
                "trailing_active": mgr.trailing_activated,
                "tp1_hit": mgr.tp1_hit,
                "tp2_hit": mgr.tp2_hit,
                "runner_active": mgr.runner_active,
                "drawdown": mgr.drawdown,
                "lifecycle": mgr.lifecycle
            }
        else:
            # No trade: default values
            dynamic_trade_data = {
                "roe": 0.0,
                "trailing_active": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "runner_active": False,
                "drawdown": 0.0,
                "lifecycle": "N/A"
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
            "last_live_refresh": DASHBOARD_STATE.get("last_live_refresh", time.time()),
            "intent_engine": intent_data,
            "dynamic_trade": dynamic_trade_data,
            **live_data
        }
        # Add queue status
        if USE_EXECUTION_QUEUE:
            queue_status = queue.get_status()
            payload['queue'] = {
                'enabled': True,
                'total': queue_status['total_candidates'],
                'ready': queue_status['ready'],
                'best_score': queue_status['best_score'],
                'candidates': queue_status['candidates'][:15]
            }
        else:
            payload['queue'] = {'enabled': False}
        
        safe_payload = safe_json(payload)
        cache_set("dashboard", safe_payload)
        return jsonify(safe_payload), 200
    except Exception as e:
        log_execution(f"/data error: {traceback.format_exc()}", "ERROR")
        return jsonify({"error": str(e)}), 200

@app.route("/queue")
def queue_endpoint():
    if not USE_EXECUTION_QUEUE:
        return jsonify({"enabled": False})
    status = queue.get_status()
    return jsonify(safe_json(status))

@app.route("/decision")
def decision_endpoint():
    decisions = MEMORY.get("decision_log", [])[-50:]
    return jsonify({"data": decisions})

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
    CACHE["ohlcv"]["value"].clear()
    CACHE["ticker"]["value"].clear()
    CACHE["orderbook"]["value"].clear()
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
    print(color_text(f"🔥 RF v28 Professional (FIXED) ({mode}) - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", BOLD))
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
    if USE_EXECUTION_QUEUE:
        qstat = queue.get_status()
        print(f"🎯 EXECUTION QUEUE: {qstat['total_candidates']} candidates, {qstat['ready']} ready, best score: {qstat['best_score']:.1f}")
    print("="*70 + "\n")

def print_rf_dashboard():
    print("\n" + color_text("=== RF TRIGGER CANDIDATES (Top 20) ===", MAGENTA))
    for item in MEMORY.get("rf_dashboard", [])[:20]:
        signal_icon = "🟢" if item["signal"] == "BUY" else "🔴" if item["signal"] == "SELL" else "⚪"
        print(f"{item['icon']} {signal_icon} {item['symbol']} | {item['status']} | score={item['score']:.2f} | ADX={item['adx']:.1f} | RSI={item['rsi']:.1f}")
    print("")

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
        log_execution(f"Smart Scanner v2 error: {traceback.format_exc()}", "ERROR")

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
        log_execution(f"[SCANNER] Error updating institutional flow: {traceback.format_exc()}", "ERROR")

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
            log_execution(f"[LIVE_UPDATER] Error: {traceback.format_exc()}", "ERROR")
        time.sleep(5)

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
    "decision_log": []
}

SNIPER_MODE = True
CANDIDATE_SCAN_INTERVAL = 15

def sync_position_with_exchange(symbol):
    try:
        if hasattr(ex, 'fetch_positions'):
            positions = safe_api_call(ex.fetch_positions, [normalize_symbol(symbol)])
        elif hasattr(ex, 'fetch_open_positions'):
            positions = safe_api_call(ex.fetch_open_positions, [normalize_symbol(symbol)])
        else:
            return None
        if not positions:
            return None
        for pos in positions:
            pos_sym = pos.get('symbol', pos.get('info', {}).get('symbol', ''))
            if normalize_symbol(symbol) in pos_sym and float(pos.get('contracts', 0)) > 0:
                return pos
        return None
    except Exception as e:
        log_execution(f"[SYNC] error: {e}", "ERROR")
        return None

def get_realized_pnl(symbol, limit=100):
    try:
        trades = safe_api_call(ex.fetch_my_trades, normalize_symbol(symbol), limit=limit)
        if not trades:
            return 0.0, 0.0
        buy_value = 0.0
        sell_value = 0.0
        for trade in trades:
            side = trade['side'].lower()
            qty = trade['amount']
            price = trade['price']
            cost = qty * price
            if side == 'buy':
                buy_value += cost
            elif side == 'sell':
                sell_value += cost
        pnl = sell_value - buy_value
        balance = get_balance_safe()
        pnl_pct = (pnl / balance * 100) if balance > 0 else 0.0
        return pnl, pnl_pct
    except Exception as e:
        log_execution(f"[SYNC] error in get_realized_pnl: {e}", "ERROR")
        return 0.0, 0.0

def validate_position_state(local_pos, symbol):
    real_pos = sync_position_with_exchange(symbol)
    if real_pos is None:
        return None
    return local_pos

def sync_all_states():
    if PAPER_MODE:
        MEMORY["position_status"] = "OPEN" if STATE.get("open") else "CLOSED"
        MEMORY["total_pnl"] = PERF.get("total_pnl_usdt", 0.0)
        MEMORY["total_pnl_pct"] = PERF.get("total_pnl_pct", 0.0) * 100
        return
    symbol = STATE.get("current_symbol") if STATE.get("open") else (TRADE_STATE.get("symbol") if TRADE_STATE.get("in_position") else None)
    if not symbol:
        real_pos = sync_position_with_exchange(DEFAULT_SYMBOL)
        if real_pos is None:
            MEMORY["position_status"] = "CLOSED"
            MEMORY["current_position"] = None
        else:
            MEMORY["position_status"] = "OPEN"
            MEMORY["current_position"] = real_pos
        real_pnl, real_pnl_pct = get_realized_pnl(DEFAULT_SYMBOL)
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
    real_pnl, real_pnl_pct = get_realized_pnl(symbol)
    MEMORY["total_pnl"] = real_pnl
    MEMORY["total_pnl_pct"] = real_pnl_pct
    if "total_pnl_usdt" in PERF:
        PERF["total_pnl_usdt"] = real_pnl
        PERF["total_pnl_pct"] = real_pnl_pct / 100

def execute_entry(side, symbol, price, sl, tp1, tp2, score, reason, atr_val, trade_type, entry_type, classification):
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        log_execution(f"[ENTRY] Already in position, skipping {symbol}", "WARN")
        return False
    free_bal = get_free_balance_safe() if not PAPER_MODE else paper["balance"]
    usable_balance = free_bal * BALANCE_SAFETY_FACTOR
    if PAPER_MODE:
        balance = paper["balance"]
    else:
        balance = usable_balance

    if classification == "SNIPER" or classification == "INSTITUTIONAL_SNIPER":
        margin_percent = 0.40
        trade_type_label = "STRONG"
    elif classification == "TREND":
        margin_percent = 0.30
        trade_type_label = "NORMAL"
    elif classification == "LOW":
        margin_percent = 0.15
        trade_type_label = "LOW_CONF"
    else:
        margin_percent = 0.30
        trade_type_label = "NORMAL"

    margin = balance * margin_percent
    notional = margin * LEVERAGE
    qty = notional / price
    log_execution(f"[SIZING]\nFree USDT: {free_bal:.2f}\nUsable (x{BALANCE_SAFETY_FACTOR}): {balance:.2f}\nType: {trade_type_label}\nMargin: {margin:.2f}\nLeverage: {LEVERAGE}X\nNotional: {notional:.2f}\nFinal Qty: {qty:.6f}", "INFO")

    df = get_ohlcv_safe(symbol, 100)
    plus_di, minus_di, _, _ = get_di_components(df) if df is not None else (None, None, None, None)
    di_dominance = False
    if plus_di is not None and minus_di is not None:
        di_dominance = (side == "BUY" and plus_di > minus_di) or (side == "SELL" and minus_di > plus_di)
    weak_pullback = False
    if df is not None:
        last = df.iloc[-1]
        if side == "BUY":
            if last['close'] < last['open'] and abs(last['close'] - last['open']) < atr_val * 0.3:
                weak_pullback = True
        else:
            if last['close'] > last['open'] and abs(last['close'] - last['open']) < atr_val * 0.3:
                weak_pullback = True
    structure_aligned = False
    struct_shift = detect_structure_shift(df) if df is not None else None
    if side == "BUY" and struct_shift == "bullish_shift":
        structure_aligned = True
    elif side == "SELL" and struct_shift == "bearish_shift":
        structure_aligned = True
    counter_displacement = 0.0
    if df is not None:
        last = df.iloc[-1]
        if side == "SELL" and last['close'] > last['open']:
            body = abs(last['close'] - last['open'])
            if body > atr_val * 0.6:
                counter_displacement = body / atr_val
        elif side == "BUY" and last['close'] < last['open']:
            body = abs(last['close'] - last['open'])
            if body > atr_val * 0.6:
                counter_displacement = body / atr_val
    market_state = {
        "adx": compute_adx(df).iloc[-1] if df is not None else 20.0,
        "regime": MEMORY.get("regime", "UNKNOWN"),
        "di_dominance": di_dominance,
        "weak_pullback": weak_pullback,
        "structure_aligned": structure_aligned,
        "counter_displacement": counter_displacement,
        "trend_health": trend_engine.get_trend_health(df, side) if df is not None else 5
    }
    narrative = {"classification": classification}
    entry_context = {"price": price, "atr": atr_val}
    thesis = _thesis_engine.build_thesis(symbol, side, trade_type, market_state, narrative, entry_context)
    STATE["trade_thesis"] = thesis.__dict__

    regime_class = MarketRegimeClassifier.classify(df) if df is not None else "UNKNOWN"
    di_spread = abs(plus_di - minus_di) if plus_di is not None else 0
    location_quality = "mid"
    initial_conf = ConfidenceEngine.calculate_initial_confidence(score, narrative.get("narrative_score", 0), regime_class, market_state["adx"], di_spread, location_quality)

    if df is not None:
        smart_money = SmartMoneyEngine.analyze_smart_money(df)
        momentum = MomentumFlowEngine.analyze_momentum_flow(df)
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

    # --- NEW: add intent score to confidence ---
    intent_info = MEMORY.get(f"intent_{symbol}", {})
    intent_score = intent_info.get("score", 0)
    if intent_score >= 85:
        initial_conf += 15
    elif intent_score >= 75:
        initial_conf += 10
    initial_conf = min(100, initial_conf)

    STATE["current_confidence"] = initial_conf
    STATE["market_regime"] = regime_class

    if PAPER_MODE:
        paper["position"] = {"side": side, "entry": price, "qty": qty, "remaining_qty": qty}
        STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty, "remaining_qty": qty,
            "sl": sl, "current_symbol": symbol, "tp1_done": False, "trail_activated": False,
            "peak": 0.0, "atr": atr_val, "entry_time": time.time(), "entry_reasons": [reason],
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
            "entry_atr": atr_val
        })
        TRADE_STATE.update({
            "in_position": True, "symbol": symbol, "side": side, "entry": price, "qty": qty,
            "tp1_hit": False, "trail_on": False, "last_update_ts": time.time()
        })
        _live_manager.start_trade(symbol, side, price, qty, sl, tp1, tp2)
        _live_manager.set_entry_atr(atr_val)
        # Create dynamic manager for paper mode
        STATE["dynamic_manager"] = DynamicTradeManager(
            symbol, side, price, qty, atr_val, sl, tp1, tp2
        )
        update_position_dashboard(symbol, side, price, qty)
        log_execution(f"PAPER {entry_type} {side} {qty:.6f} @ {price} | {trade_type_label} | {reason}", "SUCCESS")
        tg_entry(side, symbol, price, sl, tp1, score, reason, entry_type)
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
    order = open_position(side, qty, symbol)
    if order:
        STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty, "remaining_qty": qty,
            "sl": sl, "current_symbol": symbol, "tp1_done": False, "trail_activated": False,
            "peak": 0.0, "atr": atr_val, "entry_time": time.time(), "entry_reasons": [reason],
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
            "entry_atr": atr_val
        })
        TRADE_STATE.update({
            "in_position": True, "symbol": symbol, "side": side, "entry": price, "qty": qty,
            "tp1_hit": False, "trail_on": False, "last_update_ts": time.time()
        })
        _live_manager.start_trade(symbol, side, price, qty, sl, tp1, tp2)
        _live_manager.set_entry_atr(atr_val)
        # Create dynamic manager
        STATE["dynamic_manager"] = DynamicTradeManager(
            symbol, side, price, qty, atr_val, sl, tp1, tp2
        )
        update_position_dashboard(symbol, side, price, qty)
        log_execution(f"LIVE {entry_type} {side} {qty:.6f} @ {price} | {trade_type_label} | {reason}", "SUCCESS")
        tg_entry(side, symbol, price, sl, tp1, score, reason, entry_type)
        time.sleep(1)
        sync_position_state(symbol)
        return True
    else:
        return False

# ========== MAIN LOOP ==========
def main_loop_sniper():
    global INSUFFICIENT_MARGIN_COOLDOWN_UNTIL, _last_queue_promote, _last_queue_eval
    last_scan = 0
    last_scanner_v2 = 0
    last_radar_scan = 0
    last_radar_refresh = 0
    last_candidate_scan = 0
    last_flow_update = 0
    last_universe_build = 0
    last_discovery_scan = 0
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
    updater_thread.start()

    # Initialize queue timers
    _last_queue_promote = time.time()
    _last_queue_eval = time.time()

    while True:
        try:
            now = time.time()
            sync_all_states()

            # ---- Global Discovery Scan (every 20 min) ----
            if now - last_discovery_scan > GLOBAL_SCAN_INTERVAL:
                global_discovery_scan()
                last_discovery_scan = now

            # ---- Watchlist Priority Update (every 5 min) ----
            if now - last_priority_update > 300:
                WatchlistPriorityManager.update_priorities()
                last_priority_update = now

            # ---- Execution Queue integration ----
            if USE_EXECUTION_QUEUE:
                # Promote candidates from watchlist periodically
                if now - _last_queue_promote > QUEUE_PROMOTE_INTERVAL:
                    promote_to_queue()
                    _last_queue_promote = now

                # Re-evaluate all candidates
                if now - _last_queue_eval > QUEUE_RE_EVAL_INTERVAL:
                    queue.re_evaluate_all(lambda sym: get_ohlcv_safe(sym, 100))
                    _last_queue_eval = now

                # Attempt to execute best candidate (only if not in position)
                if not (STATE.get("open") or TRADE_STATE.get("in_position")):
                    process_queue_entry()

                # Cleanup expired candidates periodically
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
    while True:
        try:
            main_loop()
        except Exception as e:
            tb = traceback.format_exc()
            print(f"CRITICAL EXCEPTION: {tb}")
            try:
                log_execution(f"CRITICAL EXCEPTION: {tb}", "ERROR")
            except Exception as log_err:
                print(f"Failed to log: {log_err}")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=safe_main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False, use_reloader=False)