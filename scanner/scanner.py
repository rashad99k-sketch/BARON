"""Market discovery and scanner subsystem.

The scanner imports the core namespace after the core module is fully loaded.
"""
import core.engine as E
# Make the core runtime namespace available to legacy scanner functions without duplicating state.
globals().update({k:v for k,v in vars(E).items() if not k.startswith('__')})

# ========== RF SCANNER ==========
def get_usdt_perp_symbols():
    try:
        ex.load_markets()
        markets = ex.markets
        symbols = []
        for s, market in markets.items():
            if "USDT" in str(s).upper() and (market.get('swap') or market.get('future')) and market.get('active', True):
                symbols.append(s)
        return symbols[:300]
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
# ========== SMART INSTITUTIONAL ENTRY ENGINE ==========
def check_institutional_entry(symbol, side, df, ob, atr, price):
    # --- NEW: Institutional Intent Gatekeeper ---
    intent_score, intent_status, intent_details = InstitutionalIntentEngine.detect(df, ob, symbol)
    if intent_score < 75:
        log_execution(f"[INTENT] {symbol} {side} intent score {intent_score} < 75 – abort.", "WARN")
        return False, None, f"Intent score {intent_score}"
    MEMORY[f"intent_{symbol}"] = intent_details
    log_execution(f"[INTENT] {symbol} {side} score={intent_score} status={intent_status}", "SUCCESS")

    # --- Phase-3 (G7): IFVG warning gate ---
    # Direct entries INSIDE an inverse (polarity-exhausted) FVG are rejected:
    # price is about to trade into a zone whose old support flipped to supply
    # (or vice versa). A through-carry (price already beyond the far side, or a
    # BROKEN retest consuming the inversion) is NOT blocked here; the queue
    # penalty in get_opportunity handles proximity-weighting downstream.
    if IFVG_ENABLED and atr:
        _ifvg = ifvg_warning_payload(side, df, atr, price)
        _zone = _ifvg.get("closest") or {}
        _zb, _zt = _zone.get("bottom"), _zone.get("top")
        _inside = bool(_zb is not None and _zt is not None and _zb <= price <= _zt)
        if _inside and _ifvg.get("has_inverse") and _zone.get("retest") != "BROKEN":
            log_execution(
                f"[IFVG] {symbol} {side} entry blocked: price inside inverse "
                f"{_zone.get('side')} FVG zone@bar{_zone.get('bar')} (retest={_zone.get('retest')})",
                "WARN",
            )
            return False, None, "IFVG_WARNING: price inside inverse FVG zone"

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

# Zone analysis lives exclusively in core.engine (canonical implementations
# compute_zone_strength / get_smart_zones). This module receives them through
# the core-namespace import above; any local copies previously here diverged
# and were removed to keep a single zone-analysis contract.

def build_smart_zone_map(symbol, df, ob=None):
    return E.get_smart_zones(symbol, df, ob)

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

def _safe_numeric(value, default=50.0):
    try:
        if isinstance(value, bool):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def promote_to_queue():
    """Prepare early institutional candidates for live execution re-evaluation.

    MEDIUM/STRONG never means "enter now". The mandatory intermediate stage is
    Institutional Zone Analysis. A candidate may be admitted to the queue when
    it is PREPARED_FOR_ENTRY (a genuine precursor cluster) or A-GRADE_READY.
    Queue admission is preparation only; live trigger, zone, ATOM, portfolio,
    risk and execution gates remain authoritative.
    """
    if not USE_EXECUTION_QUEUE:
        return 0

    watchlist = MEMORY.get("watchlist", {})
    registry = MEMORY.get("institutional_zone_analysis", {})
    if not isinstance(watchlist, dict) or not isinstance(registry, dict):
        return 0

    reject_reasons = {}
    def _rej(reason):
        reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    checked = eligible = 0
    promoted = 0
    for sym, zentry in list(registry.items()):
        entry = watchlist.get(sym)
        if not isinstance(entry, dict):
            _rej("missing_watchlist_entry")
            continue
        checked += 1
        if not entry.get("deep_analyzed"):
            _rej("not_deep_analyzed")
            continue
        if not entry.get("institutional_zone_active"):
            _rej("institutional_zone_inactive")
            continue
        is_a_grade = bool(entry.get("a_grade_ready"))
        precursor_count = int(entry.get("precursor_count", 0) or 0)
        if precursor_count <= 0:
            precursor_count = len(set(entry.get("pre_expansion_evidence", []) or []) & {
                "SWEEP", "DISPLACEMENT", "CHOCH_BOS", "BOS", "SMC_MSS", "MSB_MSS",
                "OB_ZONE", "OB_RETEST", "FVG", "IMBALANCE", "LOB_IMBALANCE",
                "REJECTION", "MBLC", "RO_RO_INSTITUTIONAL_FLOW", "SMART_MONEY",
                "MOMENTUM_ACCELERATION", "BOOST", "SHOCK", "IFVG"
            })
        is_prepared = bool(entry.get("institutional_prepared")) and precursor_count >= 2
        if not is_a_grade and not is_prepared:
            _rej("not_prepared_or_a_grade")
            continue

        # Promotion is structural/institutional only. News may contribute to
        # the risk score below, but it is never the reason a candidate becomes
        # execution-eligible. Keep the evidence explicit so the promotion
        # contract cannot silently regress into a generic score/news gate.
        analysis = entry.get("analysis", {}) or {}
        structural_evidence = entry.get("structural_evidence") or {
            "liquidity": bool(analysis.get("liq_score", entry.get("liquidity_score", 0)) >= 60),
            "structure": bool(analysis.get("struct_score", 0) >= 60),
            "order_block": str(analysis.get("ob_grade", "NONE")).upper() in {"A", "A+", "B"},
            "displacement": bool(entry.get("displacement", False)),
            "retest": bool(entry.get("retest", False)),
        }
        structural_hits = sum(1 for value in structural_evidence.values() if bool(value))
        # A-GRADE keeps the full structural qualification. PREPARED_FOR_ENTRY
        # is deliberately earlier: its precursor cluster is the qualification
        # for queue preparation, while the queue performs the fresh structural
        # and trigger checks before READY/ENTRY.
        if is_a_grade and structural_hits < 2:
            _rej("insufficient_structural_evidence")
            continue
        if not is_a_grade and structural_hits < 1 and precursor_count < 2:
            _rej("insufficient_prepared_evidence")
            continue
        entry["structural_evidence"] = structural_evidence
        entry["structural_evidence_count"] = structural_hits

        phase = str((entry.get("pre_expansion") or {}).get("phase", "NEUTRAL")).upper()
        if phase in {"OVEREXTENDED", "EXHAUSTION", "INVALIDATED"}:
            _rej("late_or_invalidated")
            continue
        if sym in queue._candidates:
            _rej("already_in_queue")
            continue
        eligible += 1

        df = get_ohlcv_safe(sym, 100)
        if df is None or len(df) < 30:
            _rej("no_fresh_data")
            continue
        price = float(df["close"].iloc[-1])
        atr = float(compute_atr(df).iloc[-1]) if len(df) > 14 else price * 0.01
        if price <= 0 or atr <= 0:
            _rej("invalid_price_atr")
            continue
        ob = get_orderbook_cached(sym, limit=10)
        side = str(entry.get("side", "BUY")).upper()
        qcfg = queue._resolve_ob_cfg(sym)

        max_ext_atr = float(os.getenv("QUEUE_MAX_EXTENSION_ATR", "1.5"))
        zl_c, zh_c, _zb_c, _zt_c = queue._find_causal_ob_zone(df, side, atr, qcfg)
        if zl_c and zh_c:
            dist_atr = abs(price - (zl_c + zh_c) / 2.0) / atr
            if dist_atr > max_ext_atr:
                _rej("entry_window_missed")
                E.record_gate_event(sym, "PROMOTION", "ENTRY_WINDOW_MISSED",
                                    f"price {dist_atr:.2f} ATR from causal OB mid; stays in institutional analysis", side)
                continue

        stale = MEMORY.get("stale_zone_refs", {}).get(f"{sym}:{side}")
        if stale:
            zl, zh, _zb, _zt = queue._find_causal_ob_zone(df, side, atr, qcfg)
            if zl and zh and abs(zl - stale.get("zone_low", 0)) < atr * 0.5 and abs(zh - stale.get("zone_high", 0)) < atr * 0.5:
                _rej("stale_zone_guard")
                continue

        sl, tp1, tp2 = compute_sl_tp(price, side, "REVERSAL", atr, df)
        intent_score = _safe_numeric(entry.get("intent_score", 0), 0)
        if intent_score <= 0:
            intent_score, _, _ = InstitutionalIntentEngine.detect(df, ob, sym)

        zone_strength = _safe_numeric(entry.get("zone_strength", 0), 0)
        if zone_strength <= 0:
            try:
                zones = get_smart_zones(sym, df, ob)
                zlist = zones.get("buy_zones", []) if side == "BUY" else zones.get("sell_zones", [])
                if zlist:
                    zone_strength = _safe_numeric(zlist[0].get("strength", 0), 0) * 10.0
            except Exception:
                zone_strength = 50.0
        if zone_strength <= 0:
            zone_strength = 50.0

        analysis = entry.get("analysis", {}) or {}
        metrics = ZoneMetrics(
            order_block_quality=_safe_numeric(analysis.get("ob_score", entry.get("ob_score", 50))),
            zone_strength=zone_strength,
            liquidity_quality=_safe_numeric(analysis.get("liq_score", entry.get("liquidity_score", 50))),
            institutional_confidence=min(100.0, max(0.0, intent_score)),
            structure_alignment=min(100.0, _safe_numeric(analysis.get("struct_score", 50))),
            entry_timing=_safe_numeric(entry.get("entry_timing_score", analysis.get("entry_timing_score", 50)), 50),
            trend_alignment=_safe_numeric(entry.get("trend_alignment", 50), 50),
            risk_score=max(0.0, 100.0 - _safe_numeric(entry.get("news_risk", 0), 0)),
        )

        _ifvg_ctx = ifvg_warning_payload(side, df, atr, price)
        if _ifvg_ctx.get("blocking") and _ifvg_ctx.get("has_inverse"):
            metrics.ifvg_warning = "BLOCK"
            metrics.ifvg_penalty = round(float(_ifvg_ctx.get("penalty", 0.0)) * 100.0 * 0.30, 2)
        elif _ifvg_ctx.get("has_inverse"):
            metrics.ifvg_warning = "WATCH"

        zl, zh, zbar, ztouch = queue._find_causal_ob_zone(df, side, atr, qcfg)
        entry_anchor = price
        if zl and zh:
            entry_anchor = zl if side == "BUY" else zh

        candidate = ExecutionCandidate(
            symbol=sym, side=side, price=price, entry_price=entry_anchor,
            stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, atr=atr, df=df, ob=ob,
            zone_metrics=metrics, original_score=_safe_numeric(entry.get("score", 0), 0),
            original_reason=("INSTITUTIONAL_ZONE_A_GRADE" if is_a_grade else "INSTITUTIONAL_PREPARED"),
            signal_type=("institutional_zone_a_grade" if is_a_grade else "institutional_prepared"),
            zone_low=zl or 0.0, zone_high=zh or 0.0, zone_origin_bar=zbar,
            zone_touches=ztouch, zone_created_at=time.time() if zl and zh else 0.0,
            zone_state="ACTIVE", ob_cfg=qcfg
        )
        candidate.state = ExecutionState.DISCOVERED
        candidate.priority_score = metrics.final_zone_score
        candidate.is_a_grade = is_a_grade
        candidate.strong_ob_present = str(analysis.get("ob_grade", "NONE")).upper() in {"A", "A+"}
        candidate.ob_grade = str(analysis.get("ob_grade", "NONE"))
        candidate.roro_signal = bool(analysis.get("roro_signal", False))
        candidate.watchlist_entry_time = _safe_numeric(entry.get("watchlist_entry_time", 0), 0)
        candidate.institutional_analysis_time = _safe_numeric(entry.get("institutional_analysis_time", 0), 0)
        candidate.trade_type = entry.get("trade_type", "REVERSAL")
        candidate.trade_style = entry.get("trade_style", "SCALP")
        candidate.entry_timing = entry.get("entry_timing", "WAIT_RETEST")
        candidate.market_phase = entry.get("market_phase", "UNKNOWN")
        candidate.zone_behaviour = entry.get("zone_behaviour", "NEUTRAL")
        candidate.trade_intelligence = entry.get("trade_intelligence", {}) or {}

        if queue.add_candidate(candidate):
            promoted += 1
            entry["queue_promoted_at"] = time.time()
            admission_state = "A_GRADE_ADMITTED" if is_a_grade else "PREPARED_ADMITTED"
            entry["queue_state"] = admission_state
            zentry["queue_state"] = admission_state
            stage_label = "A-GRADE" if is_a_grade else "PREPARED_FOR_ENTRY"
            log_execution(f"[QUEUE] Institutional Zone -> Execution Queue: {sym} {side} {stage_label}", "SUCCESS")

    MEMORY["watchlist_queue_promotions"] = MEMORY.get("watchlist_queue_promotions", 0) + promoted
    pipeline = MEMORY.setdefault("pipeline", {})
    pipeline["promotion"] = {
        "checked": checked, "eligible": eligible, "promoted": promoted,
        "skipped_entry_window": reject_reasons.get("entry_window_missed", 0),
        "rejected_by_reason": reject_reasons,
        "source": "institutional_zone_analysis", "requires": "PREPARED_OR_A_GRADE",
        "ts": time.time(),
    }
    return promoted

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

# MEMORY is already defined at the top

SNIPER_MODE = True
CANDIDATE_SCAN_INTERVAL = 15

