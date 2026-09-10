"""Runtime orchestration, exchange reconciliation and portfolio supervision.

v2: Routes all trade operations through TradeExecutionCoordinator.
The coordinator is the single authority for open/close/modify.
Risk guard reads from the coordinator's immutable closure log.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import requests

import core.engine as E
import scanner.scanner as S
from portfolio.manager import PortfolioManager
from portfolio.allocator import GlobalAssetAllocator
from portfolio.coordinator import TradeExecutionCoordinator
from scanner.deep_scanner import DeepScanner

# Compatibility exports: legacy modules still call these names through E.
globals().update({k: v for k, v in vars(E).items() if not k.startswith("__")})
globals().update({k: v for k, v in vars(S).items() if not k.startswith("__")})

MAX_OPEN_POSITIONS = max(1, int(os.getenv("MAX_OPEN_POSITIONS", "6")))
PORTFOLIO = PortfolioManager(MAX_OPEN_POSITIONS, E)
COORDINATOR = PORTFOLIO.coordinator  # Single execution authority
ALLOCATOR = GlobalAssetAllocator(PORTFOLIO, E)
DEEP_SCANNER = DeepScanner()


def sync_position_with_exchange(symbol):
    """Compatibility sync that distinguishes PAUSED/ERROR from NOT_FOUND."""
    try:
        if hasattr(E, "fetch_position_status"):
            pos, status = E.fetch_position_status(symbol)
            if status == "PAUSED":
                log_execution(f"[SYNC] {symbol} paused on venue; preserving local state", "WARN", debounce_key=f"runtime_sync_paused_{symbol}", debounce_sec=60)
                return None
            return pos
        if hasattr(ex, "fetch_positions"):
            positions = safe_api_call(ex.fetch_positions)
        elif hasattr(ex, "fetch_open_positions"):
            positions = safe_api_call(ex.fetch_open_positions)
        else:
            return None
        if not positions:
            return None
        for pos in positions:
            pos_sym = pos.get("symbol", pos.get("info", {}).get("symbol", ""))
            if normalize_symbol(symbol) == normalize_symbol(pos_sym) and float(pos.get("contracts", 0)) > 0:
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
        buy_value = sell_value = 0.0
        for trade in trades:
            qty = float(trade.get("amount", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            cost = qty * price
            if str(trade.get("side", "")).lower() == "buy":
                buy_value += cost
            elif str(trade.get("side", "")).lower() == "sell":
                sell_value += cost
        pnl = sell_value - buy_value
        balance = get_balance_safe()
        return pnl, (pnl / balance * 100) if balance > 0 else 0.0
    except Exception as e:
        log_execution(f"[SYNC] error in get_realized_pnl: {e}", "ERROR")
        return 0.0, 0.0


def validate_position_state(local_pos, symbol):
    return local_pos if sync_position_with_exchange(symbol) is not None else None


def sync_all_states():
    """Compatibility sync for single-state callers.

    The production loop uses PORTFOLIO.manage_all(), which isolates state per
    symbol. This function remains for dashboard/manual API compatibility.
    """
    if E.PAPER_MODE:
        MEMORY["position_status"] = "OPEN" if STATE.get("open") else "CLOSED"
        MEMORY["total_pnl"] = PERF.get("total_pnl_usdt", 0.0)
        MEMORY["total_pnl_pct"] = PERF.get("total_pnl_pct", 0.0) * 100
        return

    symbols = PORTFOLIO.symbols()
    if symbols:
        PORTFOLIO.manage_all()
        MEMORY["position_status"] = "OPEN"
        MEMORY["current_positions"] = PORTFOLIO.snapshot()
    else:
        real_pos = sync_position_with_exchange(DEFAULT_SYMBOL)
        MEMORY["position_status"] = "OPEN" if real_pos else "CLOSED"
        MEMORY["current_position"] = real_pos
        real_pnl, real_pnl_pct = get_realized_pnl(DEFAULT_SYMBOL)
        MEMORY["total_pnl"] = real_pnl
        MEMORY["total_pnl_pct"] = real_pnl_pct


def _publish_portfolio_dashboard(dashboard_module):
    positions = PORTFOLIO.snapshot()
    E.DASHBOARD_STATE["positions"] = positions
    perf = E.PERF if isinstance(getattr(E, "PERF", None), dict) else {}
    total_pnl = float(perf.get("total_pnl_usdt", 0.0) or 0.0)
    trades = int(perf.get("trades", 0) or 0)
    wins = int(perf.get("wins", 0) or 0)
    balance = float(E.STATE.get("initial_balance", 0.0) or 0.0)
    if balance <= 0:
        try:
            balance = float(E.get_balance_safe() or 0.0)
        except Exception:
            balance = 0.0
    E.DASHBOARD_STATE["portfolio"] = {
        "open_positions": len(positions),
        "max_positions": PORTFOLIO.max_positions,
        "capacity": max(0, PORTFOLIO.max_positions - len(positions)),
        "asset_classes": {},
        "risk": PORTFOLIO.risk_snapshot(),
        "total_pnl": total_pnl,
        "equity": balance + total_pnl,
        "balance": balance,
        "margin": float(E.STATE.get("margin", 0.0) or 0.0),
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": (wins / trades * 100) if trades else 0.0,
        "last_update": time.time(),
    }
    for pos in positions:
        base = str(pos.get("symbol", "")).upper()
        cls = "CRYPTO"
        if any(x in base for x in ("XAU", "GOLD")): cls = "GOLD"
        elif any(x in base for x in ("WTI", "BRENT", "OIL", "CRUDE")): cls = "OIL"
        elif any(x in base for x in ("SPX", "SP500", "NAS", "US30", "DAX", "FTSE", "CAC", "NIKKEI")): cls = "INDEX"
        elif str(pos.get("asset_class", "")).upper() in ("GOLD", "OIL", "INDEX", "CRYPTO", "STOCK"):
            cls = str(pos.get("asset_class", "")).upper()
        E.DASHBOARD_STATE["portfolio"]["asset_classes"][cls] = E.DASHBOARD_STATE["portfolio"]["asset_classes"].get(cls, 0) + 1
    if positions:
        primary = positions[0]
        E.DASHBOARD_STATE["position"] = primary
    else:
        E.DASHBOARD_STATE["position"] = None


def _run_discovery():
    """Run the 20-minute global discovery cycle.

    Legacy RF/Smart scanners remain auxiliary evidence providers. The canonical
    market-wide candidate source is DeepScanner, which seeds the dynamic
    50-60-symbol watchlist and never executes orders.
    """
    try:
        run_scanner_v2()
    except Exception as exc:
        log_execution(f"[SCANNER] v2: {exc}", "WARN")

    try:
        cands = scan_market_rf(top_n=40)
        MEMORY["rf_candidates"] = cands
        # Rebuild the RF dashboard from the candidates just scanned (shape-
        # compatible with the legacy build_rf_dashboard output) without a
        # second market scan.
        MEMORY["rf_dashboard"] = [
            {
                "symbol": c.get("symbol"),
                "status": c.get("status", "PROXIMITY"),
                "icon": "🔔" if c.get("rf_triggered") else "📡",
                "score": c.get("score", 0),
                "adx": c.get("adx", 0),
                "rsi": c.get("rsi", 0),
                "atrp": c.get("atrp", 0),
                "signal": c.get("rf_signal") or "N/A",
            }
            for c in (cands or [])
        ]
        MEMORY["rf_last_scan"] = time.time()
    except Exception as exc:
        log_execution(f"[SCANNER] RF auxiliary: {exc}", "WARN")

    try:
        candidates = DEEP_SCANNER.scan(force=True)
        MEMORY["top_candidates"] = candidates[:60]
        MEMORY["scanned_count"] = DEEP_SCANNER.stats.get("radar_scanned", 0)
        MEMORY["last_scan"] = DEEP_SCANNER.last_scan
    except Exception as exc:
        log_execution(f"[SCANNER] dynamic deep discovery: {traceback.format_exc()}", "ERROR")


def _service_watchlist_and_queue():
    """Continuously analyze watchlist, promote mature setups and re-evaluate queue."""
    # Early Institutional Radar: keep the staggered 20-minute discovery cycle
    # advancing one time-boxed batch per service tick (never a blocking pass).
    try:
        DEEP_SCANNER.advance_radar_cycle()
    except Exception as exc:
        log_execution(f"[RADAR] advance error: {exc}", "WARN")

    try:
        updated = DEEP_SCANNER.monitor_watchlist()
        if updated:
            log_execution(
                f"[WATCHLIST] Deep batch analyzed={len(updated)} | "
                f"active={len(MEMORY.get('watchlist', {}))}",
                "INFO",
                debounce_key="watchlist_batch",
                debounce_sec=20,
            )
    except Exception as exc:
        log_execution(f"[WATCHLIST] monitor error: {exc}", "WARN")

    try:
        promoted = promote_to_queue()
        if promoted:
            log_execution(
                f"[QUEUE] Promoted {promoted} mature watchlist candidate(s)",
                "INFO",
                debounce_key="queue_promotions",
                debounce_sec=20,
            )
    except Exception as exc:
        log_execution(f"[QUEUE] promotion error: {exc}", "WARN")

    # Lifecycle cleanup lives here, never in dashboard read routes.
    try:
        cleanup_watchlist(float(os.getenv("WATCHLIST_EXPIRE_AFTER_SEC", "600")))
    except Exception as exc:
        log_execution(f"[WATCHLIST] cleanup error: {exc}", "WARN")

    MEMORY["queue_status"] = queue.get_status() if USE_EXECUTION_QUEUE else {}

    # Continuously evaluate global allocation over the current queue snapshot
    # (read-only publication; the actual gate still only fires on READY).
    try:
        if USE_EXECUTION_QUEUE:
            queue_snapshot = [c.to_dict() for c in queue._candidates.values()]
            report = ALLOCATOR.allocate(queue_snapshot, limit=PORTFOLIO.max_positions)
            MEMORY["portfolio_allocation"] = report.to_dict()
    except Exception as exc:
        log_execution(f"[PORTFOLIO] allocation evaluation failed: {exc}", "WARN")

def _execute_ready_queue_candidate():
    """Execute only a READY queue candidate through PortfolioManager.

    This preserves the intended pipeline:
    Discovery -> Watchlist -> Institutional Queue -> READY -> Portfolio execution.
    The scanner itself never places an order.
    """
    if not USE_EXECUTION_QUEUE:
        return False

    exec_pipe = MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
    def _exec_gate(outcome):
        exec_pipe[outcome] = exec_pipe.get(outcome, 0) + 1
        exec_pipe["last_outcome"] = outcome
        exec_pipe["ts"] = time.time()

    # Safety barrier: emergency kill switch (daily drawdown / loss limit) must
    # actually gate new entries, not merely exist as dead code.
    try:
        if emergency_kill_switch_active():
            log_execution(
                "[SAFETY] Kill switch active — new entries blocked",
                "WARN",
                debounce_key="kill_switch_active",
                debounce_sec=300,
            )
            _exec_gate("kill_switch")
            return False
    except Exception as exc:
        log_execution(f"[SAFETY] kill-switch check failed: {exc}", "WARN")

    slots = PORTFOLIO.max_positions - PORTFOLIO.count()
    if slots <= 0:
        _exec_gate("no_slots")
        return False

    try:
        best = queue.get_best_candidate()
        # Unified with zone_score threshold: READY and EXECUTION both at 75.
        # A genuinely READY candidate keeps the strict ready floor. A candidate
        # offered through the confirmed-trigger fallback (Waiting) is NOT
        # hard-blocked by this floor: the authoritative final decision is the
        # Entry Quality / quality assessment inside PORTFOLIO.open_candidate /
        # execute_entry.
        is_ready = best is not None and best.state == ExecutionState.READY
        if best is None:
            _exec_gate("no_ready_candidate")
            return False
        if is_ready and best.priority_score < float(os.getenv("QUEUE_MIN_READY_SCORE", "75")):
            _exec_gate("ready_score_below_min")
            return False

        # News is contextual evidence/catalyst and execution-risk input; it must
        # not independently qualify or create an entry. Technical and
        # institutional confirmation remain the primary decision authority.
        news_risk = 0.0
        watch = MEMORY.get("watchlist", {}).get(best.symbol, {})
        if isinstance(watch, dict):
            news_risk = float(watch.get("news_risk", 0) or 0)
        if news_risk >= float(os.getenv("NEWS_RISK_BLOCK", "80")):
            _exec_gate("news_risk_block")
            E.record_gate_event(best.symbol, "EXECUTION", "NEWS_RISK_BLOCK",
                                f"news_risk={news_risk:.0f}", best.side)
            return False

        candidate = {
            "symbol": best.symbol,
            "side": best.side,
            "price": best.price,
            "sl": best.stop_loss,
            "tp1": best.take_profit_1,
            "tp2": best.take_profit_2,
            "score": best.priority_score,
            "atr": best.atr,
            "scenario": best.opportunity_type.value,
            "asset_class": watch.get("asset_class", "CRYPTO") if isinstance(watch, dict) else "CRYPTO",
            "news": watch.get("news", {}) if isinstance(watch, dict) else {},
        }

        # Global portfolio-allocator gate: rejects when class/direction caps
        # would be violated, and records the reason for dashboard explain.
        # EXECUTED candidates are already counted as open positions via
        # PORTFOLIO.contexts; forwarding them into the allocator snapshot would
        # double-count capacity and wrongly cap new entries of the same class.
        queue_snapshot = [c.to_dict() for c in queue._candidates.values()
                          if c.state != ExecutionState.EXECUTED]
        alloc_report = ALLOCATOR.allocate(queue_snapshot + [candidate], limit=PORTFOLIO.max_positions)
        for decision in alloc_report.decisions:
            if decision.symbol == candidate["symbol"]:
                if not decision.allowed:
                    MEMORY["portfolio_allocation"] = alloc_report.to_dict()
                    _exec_gate("allocator_reject")
                    E.record_gate_event(candidate["symbol"], "RISK", "ALLOCATOR_REJECT",
                                        decision.reason, candidate["side"])
                    log_execution(
                        f"[PORTFOLIO] {candidate['symbol']} rejected: {decision.reason}",
                        "INFO",
                        debounce_key=f"alloc_reject_{candidate['symbol']}",
                        debounce_sec=60,
                    )
                    return False
                break

        MEMORY["portfolio_allocation"] = alloc_report.to_dict()
        # Lifecycle observability: allocator stage committed for this candidate.
        try:
            _lc = MEMORY.setdefault("opportunity_lifecycle", {}).setdefault(best.symbol, {})
            _lc["allocator_time"] = time.time()
            _lc["state"] = best.state.value
            _lc["primary_blocker"] = best.ready_blocker
        except Exception:
            pass
        if PORTFOLIO.open_candidate(candidate):
            with queue._lock:
                if best.symbol in queue._candidates:
                    queue._candidates[best.symbol].state = ExecutionState.EXECUTED
                    queue.total_executed += 1
            # Lifecycle observability: execution + opened timestamps.
            try:
                _lc = MEMORY.setdefault("opportunity_lifecycle", {}).setdefault(best.symbol, {})
                _lc["execution_time"] = time.time()
                _lc["opened_time"] = time.time()
                _lc["state"] = "EXECUTED"
                _lc["primary_blocker"] = "NONE"
            except Exception:
                pass
            _exec_gate("executed")
            E.record_gate_event(best.symbol, "EXECUTION", "EXECUTED",
                                f"priority={best.priority_score:.1f}", best.side)
            log_execution(
                f"[QUEUE] READY -> EXECUTED {best.symbol} {best.side} "
                f"priority={best.priority_score:.1f}",
                "SUCCESS",
            )
            return True
        _exec_gate("open_candidate_failed")
    except Exception as exc:
        log_execution(f"[QUEUE] ready execution error: {exc}", "ERROR")
    return False


def execute_news_slot():
    """Independent, news-driven slot (OPT-IN, env NEWS_SLOT_ENABLED=True).

    Completely separate from the technical OB/Zone ranking:
      * qualifies ONLY via strong news (direction from the news bias),
      * may open on ANY asset class but never consumes that class's cap
        (the candidate is classified as NEWS),
      * keeps total open positions <= MAX_OPEN_POSITIONS (default 6),
      * reserves exactly 1 NEWS position (slot cap), and
      * still routes through the unified safety path (kill switch,
        extreme news-risk block, sizing / SL / TP / position management) via
        PORTFOLIO.open_candidate -> engine.execute_entry, which remains the
        authority before any real order.
    """
    if str(os.getenv("NEWS_SLOT_ENABLED", "False")).lower() not in ("1", "true", "yes"):
        return False
    try:
        import portfolio.news_slot as news_slot
    except Exception as exc:
        log_execution(f"[NEWS_SLOT] init error: {exc}", "WARN")
        return False

    exec_pipe = MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
    def _gate(outcome):
        exec_pipe[outcome] = exec_pipe.get(outcome, 0) + 1
        exec_pipe["last_outcome"] = outcome

    try:
        if emergency_kill_switch_active():
            _gate("news_kill_switch")
            return False
    except Exception:
        pass

    # Global slot cap: total open positions must stay <= MAX_OPEN_POSITIONS.
    if PORTFOLIO.max_positions - PORTFOLIO.count() <= 0:
        _gate("news_no_slots")
        return False

    # NEWS slot is capped at 1 open news position (independent of class caps).
    if news_slot.count_open_news(PORTFOLIO) >= 1:
        _gate("news_slot_full")
        return False

    watchlist = MEMORY.get("watchlist", {}) or {}
    cand = news_slot.scan_for_news_candidate(watchlist)
    if cand is None:
        _gate("news_no_candidate")
        return False

    try:
        if PORTFOLIO.open_candidate(cand):
            _gate("news_executed")
            # One news event -> exactly one trade: fingerprint the headline so a
            # later scan never re-opens the same event (even after it closed).
            try:
                news_slot.mark_news_event_traded(cand)
            except Exception:
                pass
            E.record_gate_event(cand["symbol"], "EXECUTION", "NEWS_SLOT_EXECUTED",
                                f"side={cand['side']}", None)
            log_execution(
                f"[NEWS_SLOT] Opened independent news trade {cand['symbol']} {cand['side']}",
                "SUCCESS",
            )
            return True
        _gate("news_open_candidate_failed")
    except Exception as exc:
        log_execution(f"[NEWS_SLOT] execution error: {exc}", "WARN")
        _gate("news_error")
    return False


def portfolio_loop(dashboard_module=None):
    last_discovery = 0.0
    last_watch_service = 0.0
    last_snapshot = 0.0
    last_queue_eval = 0.0
    last_reconciliation = 0.0
    discovery_interval = float(os.getenv("GLOBAL_SCAN_INTERVAL_SEC", "900"))
    watch_interval = float(os.getenv("WATCHLIST_SERVICE_INTERVAL_SEC", "20"))
    snapshot_interval = float(os.getenv("SNAPSHOT_INTERVAL", "60"))
    reconciliation_interval = float(os.getenv("RECONCILIATION_INTERVAL", "30"))

    try:
        ex.load_markets()
        log_execution("Markets loaded", "INFO")
    except Exception as exc:
        log_execution(f"Failed to load markets: {exc}", "ERROR")

    tg_start(get_balance_safe(), "LIVE" if MODE_LIVE else "PAPER")

    while not E.shutdown_requested():
        try:
            now = time.time()

            # 1. Manage existing positions via TradeCouncil (no activate/deactivate)
            if PORTFOLIO.count():
                PORTFOLIO.manage_all()

            # 2. Reconcile pending intents (idempotency safety net)
            if now - last_reconciliation >= reconciliation_interval:
                try:
                    def exchange_fetcher(symbol):
                        return sync_position_with_exchange(symbol)
                    reconciled = COORDINATOR.reconcile_pending(exchange_fetcher)
                    if reconciled:
                        for trade in reconciled:
                            PORTFOLIO._trades[trade.trade_id] = trade
                        PORTFOLIO._sync_legacy_contexts()
                        log_execution(
                            f"[COORDINATOR] Reconciled {len(reconciled)} pending trade(s)",
                            "INFO",
                        )
                    last_reconciliation = now
                except Exception as exc:
                    log_execution(f"[COORDINATOR] reconciliation error: {exc}", "WARN")

            # 3. Discovery, watchlist, queue work
            if now - last_discovery >= discovery_interval:
                _run_discovery()
                last_discovery = now

            if now - last_watch_service >= watch_interval:
                _service_watchlist_and_queue()
                last_watch_service = now

            # 4. Queue re-evaluation
            if USE_EXECUTION_QUEUE and now - last_queue_eval >= float(os.getenv("QUEUE_RE_EVAL_INTERVAL", "5")):
                try:
                    queue.re_evaluate_all(lambda sym: get_ohlcv_safe(sym, 100))
                    MEMORY["queue_last_evaluation"] = time.time()
                    last_queue_eval = now
                except Exception as exc:
                    log_execution(f"[QUEUE] fast re-evaluation error: {exc}", "WARN")

            # 5. Execute best ready candidate via coordinator
            _execute_ready_queue_candidate()
            execute_news_slot()
            if USE_EXECUTION_QUEUE:
                queue.cleanup()
                MEMORY["queue_status"] = queue.get_status()

            # 6. Institutional flow panel
            if now - MEMORY.get("last_flow_update", 0) >= 60:
                try:
                    update_institutional_flow_scanner()
                except Exception as exc:
                    log_execution(f"[SCANNER] flow updater: {exc}", "WARN")
                MEMORY["last_flow_update"] = now

            # 7. Dashboard
            if dashboard_module:
                _publish_portfolio_dashboard(dashboard_module)

            # 8. Snapshot
            if time.time() - last_snapshot >= snapshot_interval:
                try:
                    if dashboard_module and hasattr(dashboard_module, "print_snapshot"):
                        dashboard_module.print_snapshot()
                except Exception as exc:
                    log_execution(f"[SNAPSHOT] {exc}", "WARN")
                last_snapshot = time.time()

            try:
                if dashboard_module and hasattr(dashboard_module, "hourly_cleanup"):
                    dashboard_module.hourly_cleanup()
            except Exception:
                pass

            E._RUNTIME_SHUTDOWN.wait(max(1, BASE_SLEEP))

        except Exception:
            if E.shutdown_requested():
                break
            log_execution(f"Portfolio loop error: {traceback.format_exc()}", "ERROR")
            E._RUNTIME_SHUTDOWN.wait(max(2, BASE_SLEEP))


def safe_main_loop(dashboard_module=None):
    while not E.shutdown_requested():
        try:
            portfolio_loop(dashboard_module)
        except Exception:
            tb = traceback.format_exc()
            print(f"CRITICAL EXCEPTION: {tb}")
            try:
                log_execution(f"CRITICAL EXCEPTION: {tb}", "ERROR")
            except Exception:
                pass
            E._RUNTIME_SHUTDOWN.wait(5)


def keep_alive():
    interval = int(os.environ.get("KEEPALIVE_INTERVAL", "50"))
    url = os.environ.get("KEEPALIVE_URL", "").strip()
    while not E.shutdown_requested():
        try:
            if url:
                requests.get(url, timeout=5)
        except Exception as exc:
            try:
                log_execution(f"[KEEPALIVE] {exc}", "WARN")
            except Exception:
                pass
        E._RUNTIME_SHUTDOWN.wait(max(10, interval))


RUNTIME_EXPORTS = [
    "sync_position_with_exchange",
    "get_realized_pnl",
    "validate_position_state",
    "sync_all_states",
    "safe_main_loop",
    "keep_alive",
]
