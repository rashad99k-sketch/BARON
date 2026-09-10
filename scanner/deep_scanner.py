"""Dynamic multi-market discovery + continuous institutional watchlist.

Pipeline:
    venue discovery -> lightweight global radar -> TOP-N watchlist
    -> rotating deep analysis -> institutional execution queue.

The scanner discovers and analyzes opportunities but never places orders.
Portfolio/execution remain the only order authorities.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional

import core.engine as E
from news.service import NewsService, news_state_for_side
from strategy.engine import StrategyEngine
from scanner.universe import build_balanced, classify

# MSB-OB is a hard dependency of this module; isolation-safe import keeps
# teardown-order flakiness from previous test sessions from killing the
# deep-scanner module entirely.
try:
    from core.msb_ob import (
        LONG, SHORT, STATUS_ACTIVE, STATUS_TOUCHED, STATUS_MITIGATING,
        STATUS_INVALIDATED, STATUS_EXPIRED, analyze_msb,
        msb_context, rank_zones, temporal_sequence,
    )
except Exception:  # pragma: no cover - defensive fallback for stubbed-parent-package sessions
    import importlib as _il
    _msb = _il.import_module("core.msb_ob")
    LONG = getattr(_msb, "LONG", 1)
    SHORT = getattr(_msb, "SHORT", -1)
    STATUS_ACTIVE = getattr(_msb, "STATUS_ACTIVE", "ACTIVE")
    STATUS_TOUCHED = getattr(_msb, "STATUS_TOUCHED", "TOUCHED")
    STATUS_MITIGATING = getattr(_msb, "STATUS_MITIGATING", "MITIGATING")
    STATUS_INVALIDATED = getattr(_msb, "STATUS_INVALIDATED", "INVALIDATED")
    STATUS_EXPIRED = getattr(_msb, "STATUS_EXPIRED", "EXPIRED")
    analyze_msb = getattr(_msb, "analyze_msb")
    msb_context = getattr(_msb, "msb_context")
    rank_zones = getattr(_msb, "rank_zones")
    temporal_sequence = getattr(_msb, "temporal_sequence")


class DeepScanner:
    ASSET_CLASSES = ("CRYPTO", "GOLD", "OIL", "INDEX", "STOCK")

    def __init__(
        self,
        max_symbols: int = 60,
        *,
        exchange=None,
        market_loader: Optional[Callable[[], dict]] = None,
    ):
        self.watchlist_limit = int(os.getenv("DEEP_WATCHLIST_SIZE", str(max_symbols)))
        # Optional dependency injection keeps tests deterministic without changing
        # the production default, which continues to use the canonical exchange.
        self.exchange = exchange
        self._market_loader = market_loader
        self.radar_symbol_override = self._parse_radar_symbols(os.getenv("RADAR_SYMBOLS"))
        self.radar_symbols = int(os.getenv("DEEP_SCAN_RADAR_SYMBOLS", "240"))
        self.watch_batch_size = int(os.getenv("WATCHLIST_DEEP_BATCH_SIZE", "10"))
        self.watch_interval = float(os.getenv("WATCHLIST_DEEP_INTERVAL_SEC", "20"))
        self.discovery_interval = float(os.getenv("GLOBAL_SCAN_INTERVAL_SEC", "900"))
        self.strategy = StrategyEngine()
        self.news = NewsService()
        self.last_scan = 0.0
        self.last_watch_update = 0.0
        self.last_result: List[dict] = []
        self.last_radar: List[dict] = []
        self.watch_symbols: List[str] = []
        self.watch_cursor = 0
        self.cycle_id = 0
        # Early Institutional Radar: staggered/batched cycle settings. Discovery
        # no longer blocks the main loop for a full serial pass; the universe is
        # consumed in time-boxed batches across the 20-minute window.
        self.radar_batch_size = max(1, int(os.getenv("RADAR_BATCH_SIZE", "20")))
        self.radar_batch_interval = float(os.getenv("RADAR_BATCH_INTERVAL_SEC", "20"))
        self.radar_time_budget_sec = float(os.getenv("RADAR_TIME_BUDGET_SEC", "15"))
        self.radar_cycle_seconds = float(
            os.getenv("RADAR_CYCLE_SECONDS", os.getenv("GLOBAL_SCAN_INTERVAL_SEC", "900"))
        )
        self.radar_max_calls_per_min = int(os.getenv("RADAR_MAX_CALLS_PER_MIN", "60"))
        self.radar_proximity_pct = float(os.getenv("RADAR_PROXIMITY_PCT", "0.0075"))
        self.radar_target = int(os.getenv("RADAR_TARGET_SYMBOLS", "60"))
        self.radar_cycle: Optional[dict] = None
        self._radar_api_call_times: List[float] = []
        self.status = {
            "universe": "INIT",
            "radar": "INIT",
            "watchlist": "INIT",
            "universe_error": None,
            "radar_error": None,
            "radar_cycle": "IDLE",
        }
        self.stats = {
            "universe": 0,
            "radar_scanned": 0,
            "watchlist_target": self.watchlist_limit,
            "watchlist_active": 0,
            "deep_analyzed": 0,
            "queue_candidates": 0,
            "errors": 0,
            "last_discovery": 0.0,
            "last_watch_update": 0.0,
        }

    @staticmethod
    def _parse_radar_symbols(raw: Optional[str]) -> List[str]:
        """Parse the optional RADAR_SYMBOLS override without making it mandatory."""
        if not raw or not str(raw).strip():
            return []
        return [item.strip() for item in str(raw).split(",") if item.strip()]

    def _load_markets(self) -> dict:
        """Load markets through an injectable boundary while preserving E.ex in production."""
        if self._market_loader is not None:
            markets = self._market_loader()
            return markets if isinstance(markets, dict) else {}
        exchange = self.exchange or E.ex
        exchange.load_markets()
        markets = getattr(exchange, "markets", {}) or {}
        return markets if isinstance(markets, dict) else {}

    def _select_radar_rows(self, rows: List[dict]) -> List[dict]:
        """Apply RADAR_SYMBOLS only as an optional filter/override."""
        if not self.radar_symbol_override:
            return rows
        wanted = set(self.radar_symbol_override)
        return [row for row in rows if row.get("symbol") in wanted]

    def _publish_status(self) -> None:
        """Publish explicit scanner state without changing existing list contracts."""
        E.MEMORY["deep_scanner_status"] = dict(self.status)
        E.MEMORY["deep_universe_status"] = self.status["universe"]
        E.MEMORY["deep_radar_status"] = self.status["radar"]

    def _ticker_activity(self) -> Dict[str, float]:
        """One public fetch_tickers call per discovery cycle -> activity map.

        Activity = |24h % change| * 2 + log-scaled 24h quote volume. This is a
        DISCOVERY ranking signal only (never an entry signal): it guarantees the
        instruments that are actually moving cannot be cut by the radar limit.
        A provider failure degrades gracefully to venue-metadata ordering.
        """
        try:
            exchange = self.exchange or E.ex
            tickers = exchange.fetch_tickers()
        except Exception as exc:
            E.log_execution(f"[DEEP] ticker activity unavailable: {exc}", "WARN",
                            debounce_key="ticker_activity_fail", debounce_sec=300)
            return {}
        activity: Dict[str, float] = {}
        for sym, tk in (tickers or {}).items():
            try:
                pct = abs(float(tk.get("percentage") or 0.0))
                qv = float(tk.get("quoteVolume") or 0.0)
                activity[sym] = pct * 2.0 + min(20.0, max(0.0, (qv ** 0.25) / 10.0))
            except Exception:
                continue
        return activity

    def _discover(self) -> List[dict]:
        self.status["universe_error"] = None
        try:
            markets = self._load_markets()
        except Exception as exc:
            self.stats["errors"] += 1
            self.status["universe"] = "PROVIDER_FAILURE"
            self.status["universe_error"] = str(exc)
            self._publish_status()
            E.log_execution(f"[DEEP] market discovery failed: {exc}", "WARN")
            return []

        if not markets:
            self.status["universe"] = "DATA_UNAVAILABLE"
            self._publish_status()
            E.log_execution("[DEEP] market discovery returned no markets", "WARN")
            return []

        # Universe accounting: every loaded instrument is either eligible or has
        # an explicit rejection reason. No silent drops.
        uni_rejects: Dict[str, int] = {}
        eligible_count = 0
        for sym, m in markets.items():
            if not m or m.get("active") is False:
                uni_rejects["inactive"] = uni_rejects.get("inactive", 0) + 1
                continue
            mtype = str(m.get("type", "")).lower()
            if mtype not in {"swap", "future"}:
                key = f"type:{mtype or 'none'}"
                uni_rejects[key] = uni_rejects.get(key, 0) + 1
                continue
            eligible_count += 1

        activity = self._ticker_activity()
        radar_limit = len(markets) if self.radar_symbols <= 0 else min(self.radar_symbols, len(markets))
        rows = build_balanced(markets, radar_limit=radar_limit, activity=activity)
        rows = self._select_radar_rows(rows)
        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["asset_class"]] = counts.get(row["asset_class"], 0) + 1
            tk = activity.get(row["symbol"])
            if tk is not None:
                row["activity"] = round(tk, 2)

        self.stats["universe"] = len(rows)
        self.status["universe"] = "HEALTHY" if rows else "NO_OPPORTUNITY"
        self._publish_status()
        E.MEMORY["deep_universe_counts"] = counts
        E.MEMORY["deep_universe_size"] = len(rows)
        pipeline = E.MEMORY.setdefault("pipeline", {})
        pipeline["universe"] = {
            "loaded": len(markets),
            "eligible": eligible_count,
            "filtered": len(markets) - eligible_count,
            "rejected_by_reason": uni_rejects,
            "selected": len(rows),
            "by_class": counts,
            "activity_ranked": bool(activity),
            "ts": time.time(),
        }
        classes_needed = ("CRYPTO", "STOCK", "INDEX", "METAL", "GOLD", "OIL", "ENERGY", "FOREX")
        for cls in classes_needed:
            n = counts.get(cls, 0)
            E.log_execution(f"[GLOBAL] {cls} discovered={n}",
                            "INFO",
                            debounce_key=f"global_disc_{cls}",
                            debounce_sec=60)
            if n == 0:
                reason = "NO_QUALIFIED_SETUP"
                try:
                    possible = sum(1 for sym, m in markets.items()
                                   if classify(sym, m)[0] == cls and
                                   str(m.get("type", "")).lower() in {"swap", "future"})
                    if possible == 0:
                        reason = "NO_SUPPORTED_LIQUIDITY"
                except Exception:
                    pass
                E.log_execution(f"[GLOBAL] {cls}: {reason}",
                                "INFO",
                                debounce_key=f"global_missing_{cls}",
                                debounce_sec=60)
        return rows

    @staticmethod
    def _zone_context(sym: str, df) -> dict:
        """Return nearest support/resistance context with explicit zone_status.

        A computation failure is reported as ZONE_ERROR, an empty result as
        NO_VALID_ZONE, and missing frames as DATA_UNAVAILABLE — never silently
        folded into a legitimate distance/score.
        """
        status = "OK"
        if df is None or len(df) < 2:
            status = "DATA_UNAVAILABLE"
            zones = {"buy_zones": [], "sell_zones": []}
        else:
            try:
                zones = E.get_smart_zones(sym, df, None)
            except Exception as exc:
                zones = {"buy_zones": [], "sell_zones": []}
                status = "ZONE_ERROR"
                E.log_execution(f"[RADAR] zone computation failed for {sym}: {exc}", "WARN",
                                debounce_key=f"zone_err_{sym}", debounce_sec=300)
            if status == "OK" and not zones.get("buy_zones") and not zones.get("sell_zones"):
                status = "NO_VALID_ZONE"
        result = {
            "zone_status": status,
            "buy_zone": None,
            "sell_zone": None,
            "buy_distance": 999.0,
            "sell_distance": 999.0,
            "buy_strength": 0.0,
            "sell_strength": 0.0,
        }
        if df is None or len(df) < 2:
            return result
        price = float(df["close"].iloc[-1])
        buy = zones.get("buy_zones", [])
        sell = zones.get("sell_zones", [])
        buy_near = min(
            buy,
            key=lambda z: abs(price - float(z["price"])) / price,
            default=None,
        )
        sell_near = min(
            sell,
            key=lambda z: abs(price - float(z["price"])) / price,
            default=None,
        )
        result["buy_zone"] = buy_near
        result["sell_zone"] = sell_near
        result["buy_distance"] = (
            abs(price - float(buy_near["price"])) / price if buy_near else 999.0
        )
        result["sell_distance"] = (
            abs(price - float(sell_near["price"])) / price if sell_near else 999.0
        )
        result["buy_strength"] = float(buy_near.get("strength", 0)) if buy_near else 0.0
        result["sell_strength"] = float(sell_near.get("strength", 0)) if sell_near else 0.0
        return result

    def _radar(self, rows: List[dict]) -> List[dict]:
        """Legacy synchronous whole-universe pass (kept for direct callers and
        tests). The staggered cycle shares the exact same per-row analysis path
        via ``_analyze_radar_row``; nothing about the radar contract changes.
        """
        radar = []
        attempted = 0
        data_failures = 0
        invalid_data = 0
        analysis_failures = 0
        scan_count = len(rows) if self.radar_symbols <= 0 else min(self.radar_symbols, len(rows))
        for row in rows[:scan_count]:
            attempted += 1
            entry, outcome = self._analyze_radar_row(row)
            if outcome == "no_data":
                data_failures += 1
            elif outcome == "invalid":
                invalid_data += 1
            elif outcome == "error":
                analysis_failures += 1
            else:
                radar.append(entry)

        radar = self._rank_radar(radar)
        self.stats["radar_no_data"] = self.stats.get("radar_no_data", 0) + data_failures
        self.stats["radar_errors"] = self.stats.get("radar_errors", 0) + analysis_failures
        pipeline = E.MEMORY.setdefault("pipeline", {})
        pipeline["radar"] = {
            "attempted": attempted,
            "scanned": len(radar),
            "no_ohlcv_data": data_failures,
            "invalid_data": invalid_data,
            "analysis_errors": analysis_failures,
            "ts": time.time(),
        }
        if radar:
            self.status["radar"] = "HEALTHY" if (data_failures == 0 and analysis_failures == 0) else "DEGRADED"
            self.status["radar_error"] = None
        elif attempted == 0:
            self.status["radar"] = "NO_OPPORTUNITY"
            self.status["radar_error"] = None
        elif data_failures == attempted:
            self.status["radar"] = "DATA_UNAVAILABLE"
            self.status["radar_error"] = f"No usable OHLCV for {attempted} candidate(s)"
        else:
            self.status["radar"] = "PROVIDER_FAILURE"
            self.status["radar_error"] = f"{analysis_failures} analysis failure(s)"
        self._publish_status()
        E.log_execution(
            f"[RADAR] pass scanned={attempted} accepted={len(radar)} "
            f"near_ob={sum(1 for r in radar if r.get('near_ob'))}",
            "INFO",
            debounce_key="radar_pass_log",
            debounce_sec=300,
        )
        return radar

    @staticmethod
    def _rank_radar(radar: List[dict]) -> List[dict]:
        """Order radar rows by the composite discovery rank.

        ``radar_rank = radar_score + ob_proximity`` is a *discovery* priority
        (not an entry score): symbols sitting near a strong institutional zone
        outrank pure noise movers of equal volatility.
        """
        radar.sort(
            key=lambda x: (float(x.get("radar_rank", 0.0)), float(x.get("radar_score", 0.0))),
            reverse=True,
        )
        return radar

    def _ob_proximity(self, zones: dict, price: float) -> tuple:
        """Distance of price to the nearest institutional smart zone (pct).

        Returns (proximity 0..2, near_ob bool, prox_side str|None). A zone is
        "near" when price sits within ``RADAR_PROXIMITY_PCT`` % of it, and the
        proximity is weighted by the zone's institutional strength. Discovery
        tier only — never an entry decision.
        """
        if not zones or not price or price <= 0:
            return 0.0, False, None
        present = []
        bd = float(zones.get("buy_distance", 999.0))
        sd = float(zones.get("sell_distance", 999.0))
        if bd < 999.0:
            present.append(("BUY", bd, float(zones.get("buy_strength", 0.0))))
        if sd < 999.0:
            present.append(("SELL", sd, float(zones.get("sell_strength", 0.0))))
        if not present:
            return 0.0, False, None
        side, dist, strength = min(present, key=lambda x: x[1])
        if dist > self.radar_proximity_pct:
            return 0.0, False, None
        prox = min(2.0, 1.0 + min(1.0, strength / 80.0))
        return round(prox, 3), True, side

    def _analyze_radar_row(self, row: dict) -> tuple:
        """Analyze a single radar symbol for the institutional radar tier.

        Returns (entry, outcome) with outcome in {"ok", "no_data", "invalid",
        "error"}. Shared by the synchronous ``_radar`` pass and the staggered
        20-minute cycle so both tiers score identically.
        """
        sym = row["symbol"]
        if getattr(E, "SYMBOL_GUARD", None) is not None and E.SYMBOL_GUARD.is_paused(sym):
            return None, "no_data"
        self._radar_api_call_times.append(time.time())
        try:
            df = E.get_ohlcv_safe(sym, 120)
            if df is None or len(df) < 40:
                return None, "no_data"
            df.symbol = sym
            price = float(df["close"].iloc[-1])
            atr = float(E.compute_atr(df).iloc[-1])
            adx = float(E.compute_adx(df).iloc[-1])
            if price <= 0 or atr <= 0:
                return None, "invalid"

            atr_pct = atr / price * 100
            rf = E.RFEngine(period=20, multiplier=3.5).compute(df)
            vol_ma = float(df["volume"].iloc[-20:].mean()) if "volume" in df else 0.0
            vol_ratio = float(df["volume"].iloc[-1] / vol_ma) if vol_ma > 0 else 1.0
            momentum = E.MomentumFlowEngine.analyze_momentum_flow(df)
            smart = E.SmartMoneyEngine.analyze_smart_money(df)
            # Publish the freshly classified market regime so the Intent
            # Engine's layer-9 adaptive weights consume a live value
            # (evaluate_with_narrative is not reached by the current
            # watch-list-driven pipeline).
            try:
                regime = E.detect_market_regime(df)
                E.MEMORY["regime"] = regime
            except Exception:
                pass  # regime publishing must never break the scan
            zones = self._zone_context(sym, df)

            # Lightweight radar score: discovery only. No entry decision here.
            score = 0.0
            score += min(3.0, max(0.0, (adx - 18.0) / 4.0))
            score += min(2.0, max(0.0, vol_ratio - 0.7))
            score += 1.5 if rf.get("triggered") else max(
                0.0, 1.0 - abs(float(rf.get("distance", 1.0))) / 0.02
            )
            score += 1.5 if momentum.get("trend_expansion") else 0.0
            score += 1.5 if smart.get("smart_money_dominant") else 0.0

            # Discovery is explicitly zone-aware: prefer symbols near a strong
            # support/resistance/order-block proxy, rather than random movers.
            if zones["buy_zone"] is not None and zones["buy_distance"] <= 0.01:
                score += min(2.5, zones["buy_strength"] / 40.0)
            if zones["sell_zone"] is not None and zones["sell_distance"] <= 0.01:
                score += min(2.5, zones["sell_strength"] / 40.0)

            if smart.get("distribution_risk", 0) > 70:
                score -= 2.0

            proximity, near_ob, prox_side = self._ob_proximity(zones, price)

            # Direction is only a watchlist hypothesis. The watchlist re-checks
            # both BUY and SELL continuously before queue promotion.
            candidates = []
            if rf.get("signal") in ("BUY", "SELL"):
                candidates.append(rf["signal"])
            if smart.get("institutional_bias") in ("BUY", "SELL"):
                candidates.append(smart["institutional_bias"])
            if momentum.get("flow_bias") in ("BUY", "SELL"):
                candidates.append(momentum["flow_bias"])

            if zones["buy_distance"] < zones["sell_distance"]:
                candidate_side = "BUY"
            elif zones["sell_distance"] < zones["buy_distance"]:
                candidate_side = "SELL"
            elif candidates:
                candidate_side = max(
                    set(candidates), key=candidates.count
                )
            else:
                candidate_side = "BUY" if float(df["close"].iloc[-1]) >= float(df["close"].iloc[-5]) else "SELL"

            return (
                {
                    "symbol": sym,
                    "asset_class": row["asset_class"],
                    "price": price,
                    "adx": round(adx, 1),
                    "atr_pct": round(atr_pct, 3),
                    "vol_ratio": round(vol_ratio, 2),
                    "rf_signal": rf.get("signal"),
                    "candidate_side": candidate_side,
                    "radar_score": round(max(0.0, score), 3),
                    "ob_proximity": proximity,
                    "near_ob": near_ob,
                    "prox_side": prox_side,
                    "radar_rank": round(max(0.0, score) + proximity, 3),
                    "institutional_bias": smart.get("institutional_bias", "NEUTRAL"),
                    "flow_bias": momentum.get("flow_bias", "NEUTRAL"),
                    "buy_distance": round(zones["buy_distance"] * 100, 3),
                    "sell_distance": round(zones["sell_distance"] * 100, 3),
                    "buy_zone_strength": round(zones["buy_strength"], 1),
                    "sell_zone_strength": round(zones["sell_strength"], 1),
                    "zone_status": zones["zone_status"],
                    "data_quality": "OK" if zones["zone_status"] == "OK" else zones["zone_status"],
                },
                "ok",
            )
        except Exception as exc:
            self.stats["errors"] += 1
            E.log_execution(
                f"[DEEP-RADAR] {sym} failed: {exc}",
                "WARN",
                debounce_key=f"deep_radar_{sym}",
                debounce_sec=300,
            )
            return None, "error"

    def _seed_watchlist(self, radar: List[dict]) -> List[dict]:
        top = radar[: self.watchlist_limit]
        self.cycle_id += 1
        now = time.time()
        active = {}

        for item in top:
            sym = item["symbol"]
            side = item.get("candidate_side", "BUY")
            active[sym] = {
                "symbol": sym,
                "side": side,
                "score": round(float(item["radar_score"]), 2),
                "radar_score": round(float(item["radar_score"]), 2),
                "ob_proximity": float(item.get("ob_proximity", 0.0)),
                "near_ob": bool(item.get("near_ob", False)),
                "prox_side": item.get("prox_side"),
                "radar_rank": round(float(item.get("radar_rank", item["radar_score"])), 3),
                "state": "DETECTED",
                "strength": "WEAK",
                "reasons": ["GLOBAL_RADAR"],
                "trade_type": "TREND",
                "asset_class": item["asset_class"],
                "price": item["price"],
                "rf_signal": item.get("rf_signal"),
                "institutional_bias": item.get("institutional_bias", "NEUTRAL"),
                "flow_bias": item.get("flow_bias", "NEUTRAL"),
                "buy_distance": item.get("buy_distance", 999),
                "sell_distance": item.get("sell_distance", 999),
                "buy_zone_strength": item.get("buy_zone_strength", 0),
                "sell_zone_strength": item.get("sell_zone_strength", 0),
                "deep_score": round(float(item["radar_score"]), 2),
                "deep_analyzed": False,
                "news": {"available": False, "risk": 0, "bias": "NEUTRAL", "headlines": []},
                "cycle_id": self.cycle_id,
                "last_update": now,
            }

        E.MEMORY["watchlist"] = active
        self.watch_symbols = list(active.keys())
        self.watch_cursor = 0
        self.stats["watchlist_active"] = len(active)
        E.MEMORY["watchlist_cycle_id"] = self.cycle_id
        E.MEMORY["watchlist_target"] = self.watchlist_limit
        E.MEMORY["watchlist_active"] = len(active)
        E.MEMORY["watchlist_last_seed"] = now
        pipeline = E.MEMORY.setdefault("pipeline", {})
        pipeline["watchlist"] = {
            "seeded": len(active),
            "target": self.watchlist_limit,
            "cycle_id": self.cycle_id,
            "ts": now,
        }
        return top

    def _radar_budget_ok(self) -> bool:
        """Per-minute API budget guard for radar reads."""
        now = time.time()
        window = [t for t in self._radar_api_call_times if now - t < 60.0]
        self._radar_api_call_times = window
        return len(window) < self.radar_max_calls_per_min

    def start_radar_cycle(self, rows: List[dict]) -> None:
        target = len(rows) if self.radar_symbols <= 0 else min(self.radar_symbols, len(rows))
        self.radar_cycle = {
            "rows": rows,
            "target": target,
            "cursor": 0,
            "started": time.time(),
            "last_batch_start": 0.0,
            "results": [],
            "data_failures": 0,
            "invalid_data": 0,
            "analysis_failures": 0,
            "api_calls": 0,
            "batches_done": 0,
            "near_ob": 0,
            "window_sec": self.radar_cycle_seconds,
            "elapsed": 0.0,
        }
        self.status["radar_cycle"] = "CYCLING"
        E.log_execution(
            f"[RADAR] cycle started target={target} batch={self.radar_batch_size} "
            f"goal={self.radar_target} window={self.radar_cycle_seconds:.0f}s",
            "INFO",
        )

    def _advance_one_batch(self, cyc: dict) -> int:
        """Process one time-boxed batch; guarantees >=1 row so the cycle always
        progresses even under extreme latency. Returns rows processed."""
        start = min(cyc["cursor"], cyc["target"])
        end = min(start + self.radar_batch_size, cyc["target"])
        if start >= cyc["target"]:
            return 0
        t0 = time.time()
        processed = 0
        for i in range(start, end):
            entry, outcome = self._analyze_radar_row(cyc["rows"][i])
            cyc["api_calls"] += 1
            if outcome == "no_data":
                cyc["data_failures"] += 1
            elif outcome == "invalid":
                cyc["invalid_data"] += 1
            elif outcome == "error":
                cyc["analysis_failures"] += 1
            else:
                cyc["results"].append(entry)
                processed += 1
                if entry.get("near_ob"):
                    cyc["near_ob"] += 1
            cyc["cursor"] = i + 1
            if i - start >= self.radar_batch_size - 1:
                break
            if time.time() - t0 >= self.radar_time_budget_sec:
                break
        return processed

    def advance_radar_cycle(self, force: bool = False) -> int:
        """Process the next staggered batch of the active radar cycle.

        - Pacing: at most one batch every ``radar_batch_interval`` seconds.
        - Each batch is time-boxed to ``radar_time_budget_sec`` (never stalls
          the main loop), and always advances at least one row.
        - ``radar_max_calls_per_min`` guards total radar API usage.
        Returns rows processed (0 = nothing advanced this tick).
        """
        cyc = self.radar_cycle
        if cyc is None:
            return 0
        now = time.time()
        if not force and now - cyc.get("last_batch_start", 0.0) < self.radar_batch_interval:
            return 0
        cyc["last_batch_start"] = now
        if not self._radar_budget_ok():
            E.log_execution(
                f"[RADAR] rate budget hit ({self.radar_max_calls_per_min}/min) - batch deferred",
                "WARN",
                debounce_key="radar_rate_defer",
                debounce_sec=30,
            )
            return 0
        tick_start = time.time()
        n = self._advance_one_batch(cyc)
        cyc["batches_done"] += 1
        cyc["elapsed"] = time.time() - cyc["started"]
        if cyc["elapsed"] > cyc["window_sec"]:
            E.log_execution(
                f"[RADAR] cycle overran its {cyc['window_sec']:.0f}s window "
                f"(cursor={cyc['cursor']}/{cyc['target']}) - check pacing/budget",
                "WARN",
                debounce_key="radar_window_overrun",
                debounce_sec=60,
            )
        # Publish progressive discovery counts while the cycle still fills, so
        # the dashboard never reports a zero scan mid-cycle.
        self.stats["radar_scanned"] = len(cyc["results"])
        if n:
            E.log_execution(
                f"[RADAR] batch {cyc['batches_done']} processed={n} "
                f"cursor={cyc['cursor']}/{cyc['target']} "
                f"accepted={len(cyc['results'])} tick={time.time() - tick_start:.1f}s",
                "INFO",
                debounce_key=f"radar_batch_{cyc['batches_done']}",
                debounce_sec=20,
            )
        if cyc["cursor"] >= cyc["target"]:
            self._finalize_radar_cycle(cyc)
        return n

    def _finalize_radar_cycle(self, cyc: dict) -> List[dict]:
        """Sort the finished cycle by radar_rank and seed the top-N watchlist."""
        self.radar_cycle = None
        radar = self._rank_radar(cyc["results"])
        self.last_radar = radar
        pipeline = E.MEMORY.setdefault("pipeline", {})
        pipeline["radar"] = {
            "attempted": cyc["target"],
            "scanned": len(radar),
            "no_ohlcv_data": cyc["data_failures"],
            "invalid_data": cyc["invalid_data"],
            "analysis_errors": cyc["analysis_failures"],
            "ts": time.time(),
        }
        if radar or cyc["target"] == 0:
            top = self._seed_watchlist(radar)
            self.status["watchlist"] = "HEALTHY" if top else "NO_OPPORTUNITY"
        else:
            # Every row died on the data layer: keep the previous watchlist.
            top = self.last_result if self.last_result else []
            self.status["watchlist"] = "PRESERVED_DEGRADED" if top else "UNAVAILABLE"
        self.status["radar_cycle"] = "COMPLETE"
        if not radar:
            self.status["radar"] = "DATA_UNAVAILABLE"
            self.status["radar_error"] = f"No usable OHLCV for {cyc['target']} candidate(s)"
        elif cyc["data_failures"] == 0 and cyc["analysis_failures"] == 0 and cyc["invalid_data"] == 0:
            self.status["radar"] = "HEALTHY"
            self.status["radar_error"] = None
        else:
            self.status["radar"] = "DEGRADED"
            self.status["radar_error"] = None
        self._publish_status()
        self.last_result = top
        self.last_scan = time.time()
        self.stats["radar_scanned"] = len(radar)
        self.stats["last_discovery"] = self.last_scan

        E.MEMORY["deep_radar"] = radar[: self.radar_target]
        E.MEMORY["deep_scanner"] = top[: self.watchlist_limit]
        E.MEMORY["deep_scanner_last_scan"] = self.last_scan
        E.MEMORY["deep_radar_last_scan"] = self.last_scan
        E.MEMORY["deep_discovery_count"] = len(radar)
        E.MEMORY["scanned_count"] = len(radar)
        E.MEMORY["last_scan"] = self.last_scan
        E.log_execution(
            f"[RADAR] cycle complete scanned={cyc['target']} accepted={len(radar)} "
            f"near_ob={cyc['near_ob']} goal={self.radar_target} "
            f"elapsed={cyc['elapsed']:.1f}s "
            f"api_calls={cyc['api_calls']} max_per_min={self.radar_max_calls_per_min}",
            "INFO",
        )
        return top

    def scan(self, force: bool = False) -> List[dict]:
        """Run the whole-venue discovery cycle and seed the TOP-N watchlist.

        Discovery drives a staggered radar cycle instead of a blocking serial
        pass: the first batch is processed here (time-boxed) and the rest fill
        in via ``advance_radar_cycle`` on each watchlist service tick. The
        previous watchlist stays live while a new cycle runs, so the main loop
        is never parked inside a full universe scan.
        """
        if not force and self.last_result and time.time() - self.last_scan < self.discovery_interval:
            return self.last_result

        rows = self._discover()
        if not rows:
            # A transient provider/data failure must not erase a valid watchlist.
            top = self.last_result if self.last_result else []
            self.status["watchlist"] = "PRESERVED_DEGRADED" if top else "UNAVAILABLE"
            self._publish_status()
            self.last_scan = time.time()
            return top

        # Discovery succeeded. A still-running cycle simply keeps filling (it is
        # paced independently); otherwise start a fresh one and run its first
        # batch here — bounded, never the full 240-symbol pass.
        if self.radar_cycle is None:
            self.start_radar_cycle(rows)
            self.advance_radar_cycle(force=True)

        self.last_scan = time.time()
        self.stats["last_discovery"] = self.last_scan
        E.MEMORY["deep_scanner_last_scan"] = self.last_scan
        E.MEMORY["deep_radar_last_scan"] = self.last_scan
        E.MEMORY["last_scan"] = self.last_scan
        E.log_execution(
            f"[DEEP] Discovery complete: universe={len(rows)} cycle={self.status.get('radar_cycle')} "
            f"watchlist={len(self.last_result or [])}",
            "INFO",
        )
        return self.last_result if self.last_result else []

    @staticmethod
    def _fvg_context(df, side: str) -> dict:
        """Lightweight three-candle fair-value-gap context.

        This implements the useful SMC part from Vibe Trading without adding a
        third-party dependency. It is a watchlist evidence signal, never a
        standalone entry trigger.
        """
        if df is None or len(df) < 5:
            return {"present": False, "distance": 999.0, "type": "NONE",
                    "ifvg_present": False, "ifvg_blocking": False,
                    "ifvg_penalty": 0.0, "ifvg_reason": "NO INVERSE FVG",
                    "ifvg_closest": None}

        price = float(df["close"].iloc[-1])
        found = None
        for i in range(len(df) - 1, 1, -1):
            a = df.iloc[i - 2]
            c = df.iloc[i]
            if float(c["low"]) > float(a["high"]):
                low, high, typ = float(a["high"]), float(c["low"]), "BULLISH"
            elif float(c["high"]) < float(a["low"]):
                low, high, typ = float(c["high"]), float(a["low"]), "BEARISH"
            else:
                continue
            if (side == "BUY" and typ == "BULLISH") or (side == "SELL" and typ == "BEARISH"):
                distance = 0.0 if low <= price <= high else min(
                    abs(price - low) / price, abs(price - high) / price
                )
                found = {"present": True, "distance": distance, "type": typ, "low": low, "high": high}
                break
        found = found or {"present": False, "distance": 999.0, "type": "NONE"}
        # Phase-3 (G7): attach the inverse-FVG warning payload (warning only —
        # the block/penalty decisions belong to the queue and entry gate).
        try:
            _atr_local = float(E.compute_atr(df).iloc[-1]) if len(df) > 14 else 0.0
            _ifvg = E.ifvg_warning_payload(side, df, _atr_local, price)
            found["ifvg_present"] = bool(_ifvg.get("has_inverse", False))
            found["ifvg_blocking"] = bool(_ifvg.get("blocking", False))
            found["ifvg_penalty"] = float(_ifvg.get("penalty", 0.0))
            found["ifvg_reason"] = _ifvg.get("reason", "NO INVERSE FVG")
            found["ifvg_closest"] = _ifvg.get("closest")
        except Exception:
            found["ifvg_present"] = False
            found["ifvg_blocking"] = False
            found["ifvg_penalty"] = 0.0
            found["ifvg_reason"] = "IFVG UNVAILABLE"
            found["ifvg_closest"] = None
        return found

    @staticmethod
    def _orderbook_imbalance(ob) -> float:
        """Top-5 bid/ask depth imbalance in [-1, 1]."""
        try:
            bids = ob.get("bids", [])[:5]
            asks = ob.get("asks", [])[:5]
            bid_qty = sum(float(x[1]) for x in bids if len(x) >= 2)
            ask_qty = sum(float(x[1]) for x in asks if len(x) >= 2)
            total = bid_qty + ask_qty
            return (bid_qty - ask_qty) / total if total > 0 else 0.0
        except Exception:
            return 0.0

    def _analyze_symbol(self, entry: dict) -> dict | None:
        sym = entry["symbol"]
        asset = entry.get("asset_class", "CRYPTO")
        try:
            if getattr(E, "SYMBOL_GUARD", None) is not None and E.SYMBOL_GUARD.is_paused(sym):
                return None
            df = E.get_ohlcv_safe(sym, 150)
            if df is None or len(df) < 60:
                return None
            df.symbol = sym
            ob = E.get_orderbook_cached(sym, limit=10)
            news = self.news.assess(sym, asset)
            analysis_age = max(0.0, time.time() - float(entry.get("last_update", time.time()) or 0))
            data_age = 0.0
            try:
                if "timestamp" in df.columns:
                    ts_last = df["timestamp"].iloc[-1]
                    ts_last = ts_last.timestamp() if hasattr(ts_last, "timestamp") else float(ts_last) / 1000.0
                    data_age = max(0.0, time.time() - ts_last)
            except Exception:
                data_age = 0.0
            data_quality = "OK"
            if data_age > 900:
                data_quality = "STALE"
            elif ob is None:
                data_quality = "DEGRADED_NO_ORDERBOOK"
            ob_imbalance = self._orderbook_imbalance(ob)

            analyses = []
            for side in ("BUY", "SELL"):
                analysis = self.strategy.analyze(sym, side, df, ob)
                analysis["df"] = df
                try:
                    ti = E.analyze_setup(df, side, float(df["close"].iloc[-1]), float(E.compute_atr(df).iloc[-1]), sym) if getattr(E, "TRADE_INTELLIGENCE_AVAILABLE", False) else {}
                except Exception:
                    ti = {}
                analysis["trade_intelligence"] = ti
                score = float(analysis.get("score", 0.0))
                if ti:
                    ti_score = float(ti.get("score", 0.0))
                    score += max(0.0, min(2.5, (ti_score - 45.0) / 20.0))
                    if ti.get("behaviour") == "ACCUMULATION" and side == "BUY":
                        score += 0.75
                    if ti.get("behaviour") in ("DISTRIBUTION", "DISTRIBUTION_RISK") and side == "SELL":
                        score += 0.75
                score += float(entry.get("radar_score", 0.0)) * 0.75

                fvg = self._fvg_context(df, side)
                if fvg.get("present") and float(fvg.get("distance", 999)) <= 0.005:
                    score += 0.75
                # Phase-3 (G7): inverse-FVG penalty at the watchlist stage. An
                # exhausted FVG that flipped polarity argues against the thesis;
                # the entry gate/queue apply the enforceable parts downstream.
                if fvg.get("ifvg_present"):
                    score -= float(fvg.get("ifvg_penalty", 0.0)) * 0.75
                if (side == "BUY" and ob_imbalance >= 0.15) or (side == "SELL" and ob_imbalance <= -0.15):
                    score += 0.50

                if news.bias == "BULLISH" and side == "BUY":
                    score += 0.5
                elif news.bias == "BEARISH" and side == "SELL":
                    score += 0.5
                score -= float(news.risk) / 20.0
                analysis["watch_score"] = max(0.0, score)
                analyses.append(analysis)

            best = max(analyses, key=lambda x: float(x.get("watch_score", 0.0)))
            trade_intel = best.get("trade_intelligence") or {}
            narrative = best.get("narrative") or {}
            score = float(best.get("watch_score", 0.0))
            reasons = []
            for key, label in (
                ("sweep", "Liquidity Sweep"),
                ("choch_bos", "BOS/CHoCH"),
                ("retest", "OB/Zone Retest"),
                ("rejection", "Rejection"),
                ("displacement", "Displacement"),
                ("volume_confirmation", "Volume"),
                ("rf_alignment", "RF"),
            ):
                if narrative.get(key):
                    reasons.append(label)
            if trade_intel:
                if trade_intel.get("phase"):
                    reasons.append(f"PHASE:{trade_intel['phase']}")
                if trade_intel.get("behaviour") not in (None, "NEUTRAL"):
                    reasons.append(f"ZONE:{trade_intel['behaviour']}")
                if trade_intel.get("timing") in ("MICRO_PULLBACK_ENTRY", "RETEST_ENTRY"):
                    reasons.append(trade_intel["timing"])

            state = "DETECTED"
            if narrative.get("retest"):
                state = "RETEST"
            if narrative.get("rejection"):
                state = "REJECTION"
            if narrative.get("displacement"):
                state = "DISPLACEMENT"
            if narrative.get("sweep") and narrative.get("choch_bos") and narrative.get("retest") and narrative.get("rejection"):
                state = "CONFIRMED"

            if news.risk >= float(os.getenv("NEWS_RISK_BLOCK", "80")):
                state = "NEWS_RISK"
            if fvg.get("present") and float(fvg.get("distance", 999)) <= 0.005:
                reasons.append("FVG")
            if (best["side"] == "BUY" and ob_imbalance >= 0.15) or (best["side"] == "SELL" and ob_imbalance <= -0.15):
                reasons.append("LOB Imbalance")

            # ---- STRONG Tier (5 gates) ------------------------------------
            # STRONG is a tier, not "score >= 8": (1) Watch score >= 8,
            # (2) clear side, (3) causal OB grade >= B, (4) Volume Validation,
            # (5) no opposing A/A+ OB nearby / no severe distribution-against-
            # BUY or accumulation-against-SELL. MEDIUM stays a PRE-STRONG tier
            # that already promotes institutional analysis into the radar.
            strength = "WEAK"
            strong_gate = {"score": False, "direction": False, "ob": False,
                           "volume": False, "conflict": False}
            strong_caution: list = []
            strong_reason = ""
            ob_grade_here = "NONE"
            ob_vpa_here: dict = {}
            obev = {}
            try:
                _atr_here = float(E.compute_atr(df).iloc[-1]) if len(df) > 14 else 0.0
                _cls = E.AssetBehaviorProfile.resolve_asset_class(sym)
                obev = E.queue._select_strong_ob(
                    df, str(best.get("side", "")), _atr_here,
                    E.AssetBehaviorProfile.ob_config(_cls)) or {}
                if isinstance(obev, dict):
                    ob_grade_here = str(obev.get("grade", "NONE"))
                    _v = obev.get("vpa") or {}
                    ob_vpa_here = _v if isinstance(_v, dict) else {}
            except Exception:
                ob_grade_here, ob_vpa_here = "NONE", {}
            if score >= 8:
                strong_gate["score"] = True
            if str(best.get("side", "")) in ("BUY", "SELL"):
                strong_gate["direction"] = True
            if ob_grade_here in ("A+", "A", "B"):
                strong_gate["ob"] = True
            if ob_vpa_here.get("under_attack"):
                strong_gate["volume"] = False
                strong_caution.append("OB_UNDER_ATTACK")
            else:
                try:
                    _vt, _vd = E.volume_validation(
                        df, str(best.get("side", "")), _atr_here,
                        float(obev.get("displacement_atr", 0.0)) if isinstance(obev, dict) else 0.0,
                        float(ob_vpa_here.get("volume_ratio", 1.0)),
                        ob_vpa_here.get("retest") or {})
                except Exception:
                    _vt, _vd = "NO_CONFIRMATION", {}
                strong_gate["volume"] = bool(_vt) and _vt in (
                    "VOLUME_CONFIRMED", "DISPLACEMENT_VOLUME_CONFIRMED", "ABSORPTION_CONFIRMED")
                if not strong_gate["volume"]:
                    strong_caution.append(f"VOLUME:{_vt}")
            _conflict = ob_vpa_here.get("opposing_conflict") or {}
            if isinstance(_conflict, dict) and _conflict.get("present"):
                strong_gate["conflict"] = False
                strong_caution.append(f"OPPOSING_{str(_conflict.get('side', 'OB'))}_NEARBY")
            else:
                try:
                    _opp_eff = E.effort_result(
                        df, "SELL" if str(best.get("side", "")) == "BUY" else "BUY",
                        _atr_here, lookback=3) or {}
                    if str(best.get("side", "")) == "BUY" and _opp_eff.get("status") == "WEAK_RESULT":
                        strong_gate["conflict"] = False
                        strong_caution.append("SEVERE_DISTRIBUTION")
                    elif str(best.get("side", "")) == "SELL" and _opp_eff.get("status") == "WEAK_RESULT":
                        strong_gate["conflict"] = False
                        strong_caution.append("SEVERE_ACCUMULATION")
                    else:
                        strong_gate["conflict"] = True
                except Exception:
                    strong_gate["conflict"] = True
            is_strong = all(strong_gate.values())
            if is_strong:
                strength = "STRONG"
            elif score >= 5:
                strength = "MEDIUM"
            else:
                strength = "WEAK"
            _retest_h = str((ob_vpa_here.get("retest") or {}).get("status", "HEALTHY"))
            strong_reason = (
                f"OB_G:{ob_grade_here} OB_VOL:{float(ob_vpa_here.get('volume_ratio', 1.0)):.1f}x "
                f"DISP:{float(obev.get('displacement_atr', 0.0)):.2f}ATR "
                f"E/R:{ob_vpa_here.get('effort_result', '?')} "
                f"VOL:{_vt if strong_gate['volume'] else 'NO_CONFIRMATION'} "
                f"SWEEP:{1 if 'Liquidity Sweep' in reasons else 0} "
                f"BOS:{1 if 'BOS/CHoCH' in reasons else 0} "
                f"RETEST:{'HEALTHY' if _retest_h == 'HEALTHY' else _retest_h} "
                f"OPPOSING:{'NONE' if strong_gate['conflict'] else ','.join(strong_caution)}"
            )
            smart = best.get("smart_money") or {}
            momentum = best.get("momentum") or {}
            intent_details = best.get("intent_details") or {}
            fvg = self._fvg_context(df, best["side"])
            if fvg.get("ifvg_present"):
                if fvg.get("ifvg_blocking"):
                    reasons.append("IFVG Warning")
                elif float(fvg.get("ifvg_penalty", 0.0)) > 0:
                    reasons.append("IFVG")

            # Attach the canonical engine-owned zone for the winning side.
            zone_payload = None
            zone_status = "NO_VALID_ZONE"
            try:
                zmap = E.get_smart_zones(sym, df, ob)
                side_zones = zmap.get("buy_zones") if best["side"] == "BUY" else zmap.get("sell_zones")
                if side_zones:
                    z = side_zones[0]
                    zone_payload = {
                        "side": best["side"],
                        "price": float(z.get("price", z.get("level", 0.0))),
                        "strength": float(z.get("strength", 0.0)),
                        "type": z.get("type", "ZONE"),
                        "reaction_count": int((z.get("details") or {}).get("reaction_count", 0)),
                        "institutional_score": float((z.get("details") or {}).get("institutional_score", 0.0)),
                    }
                    zone_status = "OK"
            except Exception as exc:
                zone_status = "ZONE_ERROR"
                self.stats["errors"] += 1
                E.log_execution(
                    f"[WATCHLIST] {sym} zone attach failed: {exc}",
                    "WARN",
                    debounce_key=f"watch_zone_{sym}",
                    debounce_sec=300,
                )

            msb_payload = {"error": "MSB_UNAVAILABLE", "zones": [], "msb_events": [], "market": None}
            msb_side_active = False
            msb_ctx = None
            primary_zone = None
            secondary_zone = None
            primary_msb_event = None
            try:
                msb_result = analyze_msb(df, sym)
                if not msb_result.get("error"):
                    msb_payload = msb_result
                    side_int = LONG if best["side"] == "BUY" else SHORT
                    primary_zone, secondary_zone = rank_zones(msb_result["zones"], side_int)
                    msb_side_active = primary_zone is not None
                    if msb_side_active:
                        reasons.append("MSB")
                    # Locate the MSB event that created the primary zone
                    if primary_zone is not None:
                        cz = primary_zone.get("created_at", 0)
                        for ev in msb_result["msb_events"]:
                            if int(ev.index) == cz and ev.direction == side_int:
                                primary_msb_event = {"direction": ev.direction,
                                                     "price": ev.price,
                                                     "index": ev.index}
                                break
                        if primary_msb_event is None and msb_result["msb_events"]:
                            ev = msb_result["msb_events"][-1]
                            primary_msb_event = {"direction": ev.direction,
                                                 "price": ev.price,
                                                 "index": ev.index}
            except Exception as exc:
                # MSB evidence must never break the deep scan; report and continue.
                self.stats["errors"] += 1
                E.log_execution(
                    f"[WATCHLIST] {sym} MSB evidence failed: {exc}",
                    "WARN",
                    debounce_key=f"watch_msb_{sym}",
                    debounce_sec=120,
                )
            # Canonical institutional context: replaces the placeholder. Uses
            # the existing queue engine's evaluators only; no new scoring
            # dimension and no READY shortcut.
            try:
                if primary_zone is not None:
                    atr_for_ctx = 0.0
                    try:
                        atrSeries = E.compute_atr(df)
                        atr_for_ctx = float(atrSeries.iloc[-1])
                    except Exception:
                        atr_for_ctx = 0.0
                    if atr_for_ctx > 0:
                        ctx_obj = msb_context(
                            df, sym, side_int, E.queue,
                            zone=primary_zone,
                            msb_event=primary_msb_event,
                            news_state=news_state_for_side(
                                news, best["side"], float(os.getenv("NEWS_RISK_BLOCK", "80"))
                            ),
                            atr=atr_for_ctx,
                        )
                        if ctx_obj is not None:
                            msb_ctx = ctx_obj.to_dict()
                        seq = temporal_sequence(
                            df, sym, side_int, E.queue,
                            zone=primary_zone, msb_event=primary_msb_event,
                            atr=atr_for_ctx,
                        )
                        if seq is not None:
                            if msb_ctx is not None:
                                msb_ctx["temporal"] = seq.to_dict()
                            else:
                                msb_ctx = {"temporal": seq.to_dict()}
            except Exception as exc:
                self.stats["errors"] += 1
                E.log_execution(
                    f"[WATCHLIST] {sym} MSB context failed: {exc}",
                    "WARN",
                    debounce_key=f"watch_msb_ctx_{sym}",
                    debounce_sec=120,
                )

            # Fast institutional precursor bridge: the DeepScanner already has
            # the freshest closed-candle evidence used to render the Watchlist.
            # Publish that evidence into the canonical Institutional Zone registry
            # immediately after this symbol is analyzed; do not wait for the legacy
            # one-by-one InstitutionalRadar refresh cycle. This is analysis-only and
            # can never create an execution order.
            precursor_evidence = set()
            if narrative.get("sweep") or float(best.get("liq_score", analysis.get("liq_score", 0)) or 0) >= 70:
                precursor_evidence.add("SWEEP")
            if narrative.get("displacement") or bool(analysis.get("displacement")):
                precursor_evidence.add("DISPLACEMENT")
            if narrative.get("choch_bos"):
                precursor_evidence.add("CHOCH_BOS")
                precursor_evidence.add("BOS")
            if msb_side_active:
                precursor_evidence.add("MSB_MSS")
            if narrative.get("retest") or trade_intel.get("timing") in ("MICRO_PULLBACK_ENTRY", "RETEST_ENTRY"):
                precursor_evidence.add("OB_RETEST")
            if zone_payload is not None and float(zone_payload.get("strength", 0) or 0) >= 50:
                precursor_evidence.add("OB_ZONE")
            if fvg.get("present"):
                precursor_evidence.add("FVG")
            if abs(ob_imbalance) >= 0.15 and ((best["side"] == "BUY" and ob_imbalance > 0) or (best["side"] == "SELL" and ob_imbalance < 0)):
                precursor_evidence.add("IMBALANCE")
            if narrative.get("rejection"):
                precursor_evidence.add("REJECTION")
            aligned_smart = str(smart.get("institutional_bias", "NEUTRAL")).upper() == best["side"]
            if aligned_smart:
                precursor_evidence.add("SMART_MONEY")
            flow_bias = str(momentum.get("flow_bias", "NEUTRAL")).upper()
            if flow_bias == best["side"]:
                precursor_evidence.add("MOMENTUM_ACCELERATION")
            if bool(momentum.get("trend_expansion")):
                precursor_evidence.add("BOOST")
            if fvg.get("ifvg_present") and not fvg.get("ifvg_blocking"):
                precursor_evidence.add("IFVG")

            entry.update(
                {
                    "side": best["side"],
                    "price": best["price"],
                    "score": round(score, 3),
                    "deep_score": round(score, 3),
                    "narrative_score": round(float(best.get("narrative_score", 0)), 3),
                    "intent_score": round(float(best.get("intent_score", 0)), 2),
                    "intent_status": best.get("intent_status", "NEUTRAL"),
                    "intent_details": intent_details,
                    "state": state,
                    "strength": strength,
                    "strong_gate": strong_gate,
                    "strong_caution": strong_caution,
                    "strong_reason": strong_reason,
                    "strong_status": ("STRONG" if is_strong else "CAUTION" if strong_caution and score >= 5 else "WEAK"),
                    "ob_grade": ob_grade_here,
                    "ob_vpa": ob_vpa_here,
                    "pre_strong": str(strength).upper() == "MEDIUM" and score >= 5,
                    "reasons": reasons or ["Deep Analysis"],
                    "trade_type": "REVERSAL" if (narrative.get("sweep") or narrative.get("retest")) else "TREND",
                    "smart_money_bias": smart.get("institutional_bias", "NEUTRAL"),
                    "smart_money_bias_detailed": smart.get("institutional_bias_detailed", "NEUTRAL"),
                    "distribution_risk": round(float(smart.get("distribution_risk", 0)), 1),
                    "accumulation": round(float(smart.get("accumulation_strength", 0)), 1),
                    "momentum_expansion": bool(momentum.get("trend_expansion")),
                    "momentum_decay": bool(momentum.get("momentum_decay")),
                    "exhaustion_risk": round(float(momentum.get("exhaustion_risk", 0)), 1),
                    "continuation_strength": round(float(momentum.get("continuation_strength", 0)), 1),
                    "narrative": narrative,
                    "trade_intelligence": trade_intel,
                    "trade_style": trade_intel.get("trade_style", "SCALP") if trade_intel else "SCALP",
                    "entry_timing": trade_intel.get("timing", "WAIT_RETEST") if trade_intel else "WAIT_RETEST",
                    "market_phase": trade_intel.get("phase", "UNKNOWN") if trade_intel else "UNKNOWN",
                    "zone_behaviour": trade_intel.get("behaviour", "NEUTRAL") if trade_intel else "NEUTRAL",
                    "market_session": trade_intel.get("evidence", {}).get("session", {}) if trade_intel else {},
                    "smart_money": smart,
                    "momentum": momentum,
                    "news": news.as_dict(),
                    "news_risk": float(news.risk),
                    "news_bias": news.bias,
                    "news_state": news_state_for_side(
                        news, best["side"], float(os.getenv("NEWS_RISK_BLOCK", "80"))
                    ),
                    "zone": zone_payload,
                    "zone_status": zone_status,
                    "msb": msb_payload,
                    "msb_active": msb_side_active,
                    "msb_context": msb_ctx,
                    "msb_primary_zone": primary_zone,
                    "msb_secondary_zone": secondary_zone,
                    "orderbook_imbalance": round(ob_imbalance, 4),
                    "fvg": fvg,
                    "deep_analyzed": True,
                    "analysis_age": round(analysis_age, 1),
                    "data_age": round(data_age, 1),
                    "data_quality": data_quality,
                    "last_update": time.time(),
                    # Canonical early-warning fields. MEDIUM/STRONG + precursor
                    # evidence enters Institutional Zone Analysis; it does not
                    # imply READY or execution.
                    "institutional_precursor_evidence": sorted(precursor_evidence),
                    "pre_expansion_evidence": sorted(precursor_evidence),
                    "pre_expansion_state": (
                        "PRE_EXPANSION_LONG" if best["side"] == "BUY" else "PRE_EXPANSION_SHORT"
                    ) if strength in ("MEDIUM", "STRONG") and len(precursor_evidence) >= 1 else entry.get("pre_expansion_state"),
                    "pre_expansion_confidence": round(min(100.0, 25.0 + len(precursor_evidence) * 10.0), 1),
                    "pre_expansion": {
                        **(entry.get("pre_expansion", {}) or {}),
                        "phase": (trade_intel.get("phase") if trade_intel else None) or (
                            "EARLY_EXPANSION" if len(precursor_evidence) >= 1 else "NEUTRAL"
                        ),
                        "zone": (zone_payload or {}).get("type") if zone_payload else None,
                        "zone_quality": float((zone_payload or {}).get("strength", 0) or 0) / 100.0,
                        "indicator_alignment": (
                            "BULLISH" if best["side"] == "BUY" else "BEARISH"
                        ) if precursor_evidence else "NEUTRAL",
                    },
                }
            )

            # Immediate dynamic promotion: this is the hand-off the Dashboard
            # represents as INSTITUTIONAL ZONE ANALYSIS — Dynamic Candidates.
            # It runs on the same fresh Watchlist result, so there is no extra
            # 90-second radar wait and no dependency on the old main_loop_sniper.
            # Every MEDIUM/STRONG deep-analyzed symbol is routed to the
            # institutional resolver WITHOUT waiting for precursor evidence:
            # the resolver's own quality gates (institutional precursor keys,
            # hypothesis, phase, state) decide whether the symbol actually
            # enters the registry. This makes the MEDIUM -> INSTITUTIONAL hop
            # latency-free instead of gated on opinion accumulation.
            try:
                radar = getattr(self, "_institutional_radar", None)
                if radar is None and hasattr(E, "InstitutionalRadar"):
                    radar = E.InstitutionalRadar()
                    self._institutional_radar = radar
                if radar is not None and str(entry.get("strength", "WEAK")).upper() in ("MEDIUM", "STRONG"):
                    radar._update_a_grade_status(entry)
                    radar._sync_institutional_zone_registry(sym, entry)
                    if entry.get("institutional_zone_active"):
                        E.log_execution(
                            f"[PROMOTION] {sym} {strength} + precursor="
                            f"{len(precursor_evidence)} -> INSTITUTIONAL_ZONE_ANALYSIS",
                            "SUCCESS",
                            debounce_key=f"inst_promotion_{sym}", debounce_sec=30,
                        )
            except Exception as exc:
                self.stats["errors"] += 1
                E.log_execution(
                    f"[PROMOTION] {sym} fast institutional bridge failed: {exc}",
                    "WARN",
                    debounce_key=f"inst_promotion_error_{sym}", debounce_sec=120,
                )
            return entry
        except Exception as exc:
            self.stats["errors"] += 1
            E.log_execution(
                f"[WATCHLIST] {sym} deep analysis failed: {exc}",
                "WARN",
                debounce_key=f"watch_deep_{sym}",
                debounce_sec=120,
            )
            return None

    def monitor_watchlist(self, force: bool = False) -> List[dict]:
        """Continuously deep-analyze a rotating batch of active watchlist symbols."""
        now = time.time()
        if not force and now - self.last_watch_update < self.watch_interval:
            return []

        watch = E.MEMORY.get("watchlist", {})
        if not isinstance(watch, dict) or not watch:
            self.stats["watchlist_active"] = 0
            return []

        self.watch_symbols = [s for s in self.watch_symbols if s in watch]
        if not self.watch_symbols:
            self.watch_symbols = list(watch.keys())
            self.watch_cursor = 0

        batch = []
        for _ in range(min(self.watch_batch_size, len(self.watch_symbols))):
            if not self.watch_symbols:
                break
            sym = self.watch_symbols[self.watch_cursor % len(self.watch_symbols)]
            self.watch_cursor = (self.watch_cursor + 1) % len(self.watch_symbols)
            batch.append(sym)

        updated = []
        for sym in batch:
            result = self._analyze_symbol(watch[sym])
            if result:
                watch[sym] = result
                updated.append(result)

        # MEDIUM-free-latency hand-off: every MEDIUM/STRONG symbol that has
        # already been deep-analyzed ANYWHERE in the active watchlist is routed
        # to Institutional Zone Analysis in the SAME scanner cycle. A symbol
        # must NOT wait for its own rotating deep-batch turn (batch_size
        # symbols per tick over potentially hundreds of watchlist entries).
        swept = self._fast_institutional_sweep(watch)
        self.stats["institutional_sweep_promoted"] = (
            self.stats.get("institutional_sweep_promoted", 0) + swept
        )

        # Keep the watchlist dynamic: stale entries are removed only after they
        # have been rechecked, while the next global cycle can replace them.
        self.stats["deep_analyzed"] = self.stats.get("deep_analyzed", 0) + len(updated)
        self.stats["watchlist_active"] = len(watch)
        self.stats["last_watch_update"] = now
        E.MEMORY["watchlist"] = watch
        E.MEMORY["watchlist_active"] = len(watch)
        E.MEMORY["watchlist_last_update"] = now
        E.MEMORY["watchlist_deep_analyzed"] = self.stats["deep_analyzed"]
        pipeline = E.MEMORY.setdefault("pipeline", {})
        wl_pipe = pipeline.setdefault("watchlist", {})
        wl_pipe["active"] = len(watch)
        wl_pipe["analyzed_total"] = self.stats["deep_analyzed"]
        wl_pipe["last_batch"] = len(updated)
        wl_pipe["last_update"] = now

        # Keep dashboard deep scanner as the current ranked watchlist snapshot.
        ranked = sorted(
            watch.values(),
            key=lambda x: float(x.get("score", 0)),
            reverse=True,
        )
        E.MEMORY["deep_scanner"] = ranked[: self.watchlist_limit]
        self.last_watch_update = now
        return updated

    def _fast_institutional_sweep(self, watch: dict) -> int:
        """Route every MEDIUM/STRONG deep-analyzed watchlist entry to the
        Institutional Zone Analysis resolver immediately, regardless of which
        rotating batch (or lack of it) produced the entry.

        This is the latency-free path for MEDIUM assets: the moment a symbol is
        deep-analyzed with MEDIUM (or STRONG) strength it is handed to the
        institutional resolver in the same scanner cycle — without waiting for
        its own turn in the rotating ``watch_batch`` and without waiting for
        incremental precursor-evidence growth. The resolver's own gates
        (institutional precursor keys, hypothesis, phase, state) still decide
        whether the symbol qualifies for the registry. Analysis-only; it can
        never create an execution order.
        """
        radar = getattr(self, "_institutional_radar", None)
        if radar is None and hasattr(E, "InstitutionalRadar"):
            try:
                radar = E.InstitutionalRadar()
            except Exception:
                radar = None
            self._institutional_radar = radar
        if radar is None:
            return 0
        promoted = 0
        for sym, entry in watch.items():
            if not isinstance(entry, dict) or not entry.get("deep_analyzed"):
                continue
            if str(entry.get("strength", "WEAK")).upper() not in ("MEDIUM", "STRONG"):
                continue
            if entry.get("institutional_zone_active"):
                continue
            try:
                radar._update_a_grade_status(entry)
                radar._sync_institutional_zone_registry(sym, entry)
            except Exception as exc:
                self.stats["errors"] += 1
                E.log_execution(
                    f"[PROMOTION] {sym} fast institutional sweep failed: {exc}",
                    "WARN",
                    debounce_key=f"inst_sweep_error_{sym}", debounce_sec=120,
                )
                continue
            if entry.get("institutional_zone_active"):
                promoted += 1
                E.log_execution(
                    f"[PROMOTION] {sym} {entry.get('strength')} swept -> "
                    f"INSTITUTIONAL_ZONE_ANALYSIS",
                    "SUCCESS",
                    debounce_key=f"inst_sweep_{sym}", debounce_sec=30,
                )
        return promoted

    def top(self, limit: int = 6) -> List[dict]:
        self.monitor_watchlist(force=True)
        ranked = sorted(
            E.MEMORY.get("watchlist", {}).values(),
            key=lambda x: float(x.get("score", 0)),
            reverse=True,
        )
        return ranked[: max(1, int(limit))]
