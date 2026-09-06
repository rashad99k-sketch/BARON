"""Flask dashboard and HTTP API.

The UI/route implementation is preserved separately from the trading engine.
"""
import os
import sys as _sys
import time
import traceback
import hmac
from flask import Flask, jsonify, request
import core.engine as E
import scanner.scanner as S
globals().update({k:v for k,v in vars(E).items() if not k.startswith('__')})
globals().update({k:v for k,v in vars(S).items() if not k.startswith('__')})


def _sync_engine_state():
    """Bind dashboard reads to the currently-registered core.engine module.

    Embedding runtimes and tests may re-import core.engine; shared mutable
    state (MEMORY/CACHE/queue) must always be read from the live module, never
    from a stale import-time snapshot.
    """
    eng = _sys.modules.get("core.engine")
    if eng is None or eng is E:
        return
    for name in ("MEMORY", "CACHE", "STATE", "DASHBOARD_STATE", "queue"):
        if hasattr(eng, name):
            globals()[name] = getattr(eng, name)
    for name in ("cache_get", "cache_set"):
        if hasattr(eng, name):
            globals()[name] = getattr(eng, name)

# ========== FLASK DASHBOARD ==========
app = Flask(__name__)


def _before_request_sync():
    _sync_engine_state()


try:
    app.before_request(_before_request_sync)
except AttributeError:
    pass  # dependency-boundary stub without the Flask API surface

# Manual trading controls are local-only unless an explicit control token is
# configured. This prevents a Render/public dashboard from exposing /trade or
# /close without authentication.
DASHBOARD_CONTROL_TOKEN = os.getenv("DASHBOARD_CONTROL_TOKEN", "").strip()

def _control_authorized():
    remote = str(getattr(request, "remote_addr", "") or "")
    if not DASHBOARD_CONTROL_TOKEN and remote in {"127.0.0.1", "::1", "localhost"}:
        return True
    supplied = request.headers.get("X-Dashboard-Token", "")
    if not supplied:
        body = request.get_json(silent=True) or {}
        supplied = str(body.get("control_token", ""))
    return bool(DASHBOARD_CONTROL_TOKEN) and hmac.compare_digest(supplied, DASHBOARD_CONTROL_TOKEN)

_update_position_dashboard_impl = E.update_position_dashboard

_RESERVED_UNDEFINED = frozenset(("undefined", "N/A", "n/a"))


def _normalize_payload(value, depth=0):
    """P1 normalization: deep-clean any payload destined for JSON so no literal
    'undefined'/'N/A' sentinel strings ever reach the browser. Numeric scalars
    are coerced to float/int and NaN/Inf removed by safe_json downstream.
    genuinely unknown values become None."""
    if depth > 40:
        return None
    if isinstance(value, dict):
        return {k: _normalize_payload(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_payload(v, depth + 1) for v in value]
    if isinstance(value, str):
        if value in _RESERVED_UNDEFINED:
            return None
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            if value != value or value in (float("inf"), float("-inf")):
                return None
        except Exception:
            pass
        return value
    return value

def update_position_dashboard(symbol, side, entry, qty, pnl=0.0):
    """Project the engine-owned canonical position state for the dashboard."""
    _update_position_dashboard_impl(symbol, side, entry, qty, pnl)

def clear_position_dashboard():
    E.clear_position_dashboard()

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
    
    # Pipeline Funnel Panel (forensic telemetry): MARKET UNIVERSE -> DISCOVERY
    # -> RADAR -> WATCHLIST -> QUEUE -> READY -> VALIDATION/RISK -> EXECUTION.
    pipeline_panel_html = """
    <div class="section smart-layer" id="pipeline-panel">
        <div class="title">🛰️ PIPELINE ACCOUNTING — Market Visibility Funnel</div>
        <div class="grid" style="grid-template-columns: repeat(8,1fr); margin-bottom:8px;">
            <div class="card">Universe<div id="p-universe">0</div></div>
            <div class="card">Radar<div id="p-radar">0</div></div>
            <div class="card">Watchlist<div id="p-watchlist">0</div></div>
            <div class="card">Promoted<div id="p-promoted">0</div></div>
            <div class="card">In Queue<div id="p-queue">0</div></div>
            <div class="card">Re-evaluated<div id="p-reeval">0</div></div>
            <div class="card">Ready<div id="p-ready" class="green">0</div></div>
            <div class="card">Executed<div id="p-executed" class="green">0</div></div>
        </div>
        <div id="p-detail" style="font-size:11px; color:#9ca3af; white-space:pre-wrap;"></div>
    </div>
    """

    # Dynamic Institutional Zone Analysis — intentionally separate from the
    # execution queue. The count is opportunity-driven (0..N), not a fixed slot count.
    institutional_zone_panel_html = """
    <div class="section smart-layer" id="institutional-zone-panel">
        <div class="title">🧠 INSTITUTIONAL ZONE ANALYSIS — Dynamic Candidates</div>
        <div id="iza-summary" class="grid" style="grid-template-columns: repeat(5,1fr); margin-bottom:10px;">
            <div class="card">Active<div id="iza-total">0</div></div>
            <div class="card">A-Grade Ready<div id="iza-ready" class="green">0</div></div>
            <div class="card">Medium Trigger<div id="iza-medium" class="orange">0</div></div>
            <div class="card">Long / Short<div id="iza-direction">0 / 0</div></div>
            <div class="card">Top Evidence<div id="iza-best">-</div></div>
        </div>
        <div id="iza-table" style="max-height:360px; overflow-y:auto; font-size:12px;">
            <table style="width:100%; border-collapse:collapse; background:#0f1724; border-radius:8px; overflow:hidden;">
                <thead><tr style="background:#1a2332; color:#9ca3af; text-align:center;">
                    <th>Symbol</th><th>Side</th><th>Strength</th><th>Inst Score</th><th>Zone</th><th>Phase</th><th>Alignment</th><th>Evidence</th><th>Stage</th>
                </tr></thead>
                <tbody id="iza-body"></tbody>
            </table>
        </div>
    </div>
    """

    # Execution Queue Panel (NEW)
    queue_panel_html = """
    <div class="section smart-layer" id="queue-panel" style="display: none;">
        <div class="title">🎯 EXECUTION QUEUE — A-GRADE / READY ONLY</div>
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
<html><head><title>RF Liquidity Pro v28 Fixed Live Supervisor</title>
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
<div class="header">🔥 RF Liquidity Pro — v28 Fixed Live Supervisor</div>
{decision_panel_html}
{scanner_v2_section}
{pipeline_panel_html}
{institutional_zone_panel_html}
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
<div class="section smart-layer"><div class="title">💼 PORTFOLIO — MULTI POSITION</div>
<div class="card">
  <div id="portfolioSummary">0 / 0 positions</div><div id="portfolioClasses" style="margin-top:6px;color:#9ca3af">No exposure</div>
  <div id="portfolioPositions" style="margin-top:10px;"></div>
</div>
</div>
<div class="section smart-layer"><div class="title">📰 NEWS / EVENT RISK</div>
<div id="newsPanel" class="card">No news assessment yet</div>
</div>
<div class="section smart-layer"><div class="title">🛰️ DEEP INSTITUTIONAL RADAR</div>
<div id="deepRadar" class="card">No radar data</div>
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
<div class="card">Universe<div id="universeSize">0</div></div>
<div class="card">Scanned<div id="scanned">0</div></div>
<div class="card">Watchlist<div id="watchlistCount">0</div></div>
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
    const news = d.news || [];
    const sentimentStyle = (h) => {{
        const c = h.sentiment_color === "GREEN" ? "#00ffa6" : (h.sentiment_color === "RED" ? "#ff4d4d" : "#9ca3af");
        return `color:${{c}};font-weight:700`;
    }};
    document.getElementById("newsPanel").innerHTML = news.length
        ? news.map(n => {{
            const headlines = (n.headlines || []).map(h =>
                `<div style="padding:7px 0;border-bottom:1px solid #1f2937;">
                    <span style="${{sentimentStyle(h)}}">${{h.sentiment || "NEUTRAL"}}</span>
                    <span style="color:#9ca3af">${{h.impact_strength || "LOW"}} · conf=${{Math.round(Number(h.sentiment_confidence || 0)*100)}}%</span>
                    <div>${{h.title || ""}}</div>
                    <small style="color:#6b7280">${{h.source || h.provider || "Unknown"}} · ${{h.event_type || "MARKET"}}</small>
                </div>`
            ).join("");
            const biasColor = n.bias === "BULLISH" ? "#00ffa6" : (n.bias === "BEARISH" ? "#ff4d4d" : "#9ca3af");
            return `<div style="padding:9px 0;">
                <div><b>${{n.symbol || "MARKET"}}</b> · <span style="color:${{biasColor}};font-weight:700">${{n.bias || "NEUTRAL"}}</span> · risk=${{Number(n.risk || 0).toFixed(0)}}</div>
                ${{headlines || "No headline evidence"}}
            </div>`;
          }}).join("")
        : "No news assessment yet";

    const portfolio = d.portfolio || {{open_positions: 0, max_positions: 0, capacity: 0}};
    document.getElementById("portfolioSummary").innerText =
        `${{portfolio.open_positions}} / ${{portfolio.max_positions}} positions | ${{portfolio.capacity}} slots available`;
    const assetClasses = portfolio.asset_classes || {{}};
    const alloc = d.allocation || null;
    let allocText = Object.entries(assetClasses).map(([k,v]) => `${{k}}: ${{v}}`).join(" | ") || "No exposure";
    if (alloc) {{
        allocText += ` | Concentration:<b>${{alloc.concentration||"-"}}</b>`;
        allocText += ` | Rotation:<b>${{alloc.rotation_regime||"-"}}</b>`;
        if (alloc.unused_slots > 0) allocText += ` | <span style="color:#e67e22" title="${{alloc.slot_reason}}">Unused slots: ${{alloc.unused_slots}}</span>`;
        const reject = (alloc.decisions||[]).find(x => !x.allowed);
        if (reject) allocText += ` | <span style="color:#e67e22" title="${{reject.reason}}">Rejected example: ${{reject.symbol}} (${{reject.reason}})</span>`;
    }}
    document.getElementById("portfolioClasses").innerHTML = allocText;
    const sessionLabel = (s) => {{
        if (!s || !s.state || s.state === "UNKNOWN") return "Session: N/A";
        const pair = s.pair ? ` · ${{s.pair}}` : "";
        const active = (s.indicator_sessions || []).join("/") || (s.active_market_centres || []).join("/") || "QUIET";
        const de = s.indicator_windows_germany || {{}};
        const ny = s.indicator_windows || {{}};
        return `Session: ${{s.state}}${{pair}} · now=${{active}} · DE NY-AM=${{de.NY_AM || ny.NY_AM || "-"}}`;
    }};
    const positions = d.positions || [];
    document.getElementById("portfolioPositions").innerHTML = positions.length
        ? positions.map(p => {{
            const cls = (p.roe_pct || 0) >= 0 ? "green" : "red";
            return `<div style="padding:8px 0;border-bottom:1px solid #1f2937;">
                <b>${{p.symbol}}</b> | ${{p.side}} |
                Entry ${{Number(p.entry || 0).toFixed(4)}} |
                ROE <span class="${{cls}}">${{Number(p.roe_pct || 0).toFixed(2)}}%</span> |
                SL ${{Number(p.sl || 0).toFixed(4)}} |
                TP1 ${{Number(p.tp1 || 0).toFixed(4)}} |
                TP2 ${{Number(p.tp2 || 0).toFixed(4)}} |
                <b>${{p.trade_style || "SCALP"}}</b> | ${{p.entry_timing || "WAIT_RETEST"}} |
                ${{p.zone_behaviour || "NEUTRAL"}}
                <div style="font-size:11px;color:#9ca3af">Board: ${{(p.trade_board||{{}}).stage || "ENTRY"}} → ${{(p.trade_board||{{}}).verdict || "MONITOR"}}</div>
                <div style="font-size:11px;color:#9ca3af">${{sessionLabel(p.market_session)}}</div>
            </div>`;
        }}).join("")
        : "No active positions";

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
            <div><b>Style:</b> ${{d.position.trade_style || "SCALP"}} | <b>Timing:</b> ${{d.position.entry_timing || "WAIT_RETEST"}} | <b>Phase:</b> ${{d.position.market_phase || "UNKNOWN"}}</div>
            <div><b>Zone:</b> ${{d.position.zone_behaviour || "NEUTRAL"}} | <b>Board:</b> ${{(d.position.trade_board||{{}}).stage || "ENTRY"}} → ${{(d.position.trade_board||{{}}).verdict || "MONITOR"}}</div>
            <div><b>Market Session:</b> ${{sessionLabel(d.position.market_session)}}</div>
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
        const sc = Number(o.score ?? o.radar_score ?? 0);
        top5Html += `<div><b>${{o.symbol}}</b> | Score: ${{sc.toFixed(2)}} | ADX: ${{o.adx || 0}} | RSI: ${{o.rsi ?? "-"}}</div><hr>`;
    }});
    document.getElementById("top5").innerHTML = top5Html || "No opportunities";
    const radar = d.deep_radar || [];
    document.getElementById("deepRadar").innerHTML = radar.length ? radar.slice(0,20).map(r =>
        `<div><b>${{r.symbol}}</b> | ${{r.asset_class}} | Radar ${{Number(r.radar_score||0).toFixed(2)}} | ADX ${{r.adx||0}} | ATR ${{r.atr_pct||0}}% | RF ${{r.rf_signal||"-"}}</div>`
    ).join("") : "No radar data";
    document.getElementById("scanned").innerText = d.scanned_count || 0;
    document.getElementById("universeSize").innerText = d.universe_size || 0;
    document.getElementById("watchlistCount").innerText = d.watchlist_active || 0;
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
            let lastUpdate = w.last_update ? new Date(w.last_update * 1000).toLocaleTimeString() : "-";
            let extraInfo = "";
            if (w.smart_money_bias_detailed) extraInfo += ` | Bias: ${{w.smart_money_bias_detailed}}`;
            else if (w.smart_money_bias) extraInfo += ` | Bias: ${{w.smart_money_bias}}`;
            if (w.distribution_risk !== undefined) extraInfo += ` | DistRisk: ${{w.distribution_risk}}`;
            if (w.momentum_expansion) extraInfo += ` | 🚀`;
            if (w.momentum_decay) extraInfo += ` | 📉`;
            if (w.news_state) {{
                const nc = {{NEWS_SUPPORT:"#2ecc71",NEWS_CONFLICT:"#e74c3c",NEWS_RISK:"#e74c3c",NEWS_NEUTRAL:"#95a5a6",NEWS_UNAVAILABLE:"#f1c40f"}}[w.news_state] || "#95a5a6";
                extraInfo += ` | <span style="color:${{nc}}">${{w.news_state}}</span>`;
            }}
            if (w.data_quality && w.data_quality !== "OK") extraInfo += ` | <span style="color:#f1c40f">DQ:${{w.data_quality}}</span>`;
            if (w.zone_status && w.zone_status !== "OK") extraInfo += ` | <span style="color:#e74c3c">${{w.zone_status}}</span>`;
            if (w.zone) extraInfo += ` | Zone:${{w.zone.type}} @${{w.zone.price}} str=${{w.zone.strength}}`;
            if (w.analysis_age !== undefined) extraInfo += ` | Age:${{w.analysis_age}}s`;
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
    // === Pipeline funnel (forensic telemetry) ===
    if (d.pipeline) {{
        const pl = d.pipeline;
        const uni = pl.universe || {{}};
        const rad = pl.radar || {{}};
        const wl = pl.watchlist || {{}};
        const promo = pl.promotion || {{}};
        const q = d.queue || {{}};
        const gs = q.gate_stats || {{}};
        const exe = pl.execution || {{}};
        document.getElementById("p-universe").innerText = uni.selected || 0;
        document.getElementById("p-radar").innerText = rad.scanned || 0;
        document.getElementById("p-watchlist").innerText = wl.active || wl.seeded || 0;
        document.getElementById("p-promoted").innerText = q.promotions || 0;
        document.getElementById("p-queue").innerText = q.total || 0;
        document.getElementById("p-reeval").innerText = q.gate_stats ? (q.gate_stats.ready || 0) + (q.gate_stats.extended || 0) + (q.gate_stats.zone_stale || 0) + (q.gate_stats.zone_invalidated || 0) + (q.gate_stats.score_too_low || 0) + (q.gate_stats.ob_broken || 0) + (q.gate_stats.insufficient_data || 0) : 0;
        document.getElementById("p-ready").innerText = q.ready || 0;
        document.getElementById("p-executed").innerText = (q.gate_stats && q.gate_stats.ready !== undefined) ? (exe.executed || 0) : 0;
        let det = "";
        if (uni.loaded !== undefined) det += `UNIVERSE loaded=${{uni.loaded}} eligible=${{uni.eligible}} filtered=${{uni.filtered}} selected=${{uni.selected}} ranked_by_activity=${{uni.activity_ranked}}\n`;
        if (uni.rejected_by_reason) det += `  rejects: ${{JSON.stringify(uni.rejected_by_reason)}}\n`;
        if (uni.by_class) det += `  classes: ${{JSON.stringify(uni.by_class)}}\n`;
        if (rad.attempted !== undefined) det += `RADAR attempted=${{rad.attempted}} scanned=${{rad.scanned}} no_data=${{rad.no_ohlcv_data}} errors=${{rad.analysis_errors}}\n`;
        if (wl.seeded !== undefined) det += `WATCHLIST seeded=${{wl.seeded}} active=${{wl.active}} analyzed_total=${{wl.analyzed_total || 0}}\n`;
        if (promo.checked !== undefined) det += `PROMOTION checked=${{promo.checked}} eligible=${{promo.eligible}} promoted=${{promo.promoted}} window_missed=${{promo.skipped_entry_window}} rejects=${{JSON.stringify(promo.rejected_by_reason || {{}})}}\n`;
        det += `QUEUE gates: ${{JSON.stringify(gs)}}\n`;
        if (exe.last_outcome) det += `EXECUTION last=${{exe.last_outcome}} counts=${{JSON.stringify(exe)}}\n`;
        document.getElementById("p-detail").innerText = det || "waiting for first cycle";
    }}
    // === Dynamic Institutional Zone Analysis (separate from Execution Queue) ===
    const iza = d.institutional_zone_analysis || [];
    const izaReady = iza.filter(x => x.a_grade_ready).length;
    const izaMedium = iza.filter(x => x.strength === "MEDIUM").length;
    const izaLong = iza.filter(x => String(x.hypothesis || "").endsWith("LONG")).length;
    const izaShort = iza.filter(x => String(x.hypothesis || "").endsWith("SHORT")).length;
    document.getElementById("iza-total").innerText = iza.length;
    document.getElementById("iza-ready").innerText = izaReady;
    document.getElementById("iza-medium").innerText = izaMedium;
    document.getElementById("iza-direction").innerText = `${{izaLong}} / ${{izaShort}}`;
    document.getElementById("iza-best").innerText = iza.length ? (iza[0].precursor_evidence || []).slice(0,2).join(" + ") : "-";
    const izaBody = document.getElementById("iza-body");
    izaBody.innerHTML = "";
    iza.slice(0, 50).forEach(x => {{
        const tr = document.createElement("tr");
        tr.style.borderBottom = "1px solid #2c3e50";
        const stage = x.a_grade_ready ? "A-GRADE READY" : "INSTITUTIONAL WATCH";
        tr.innerHTML = `
          <td><b>${{x.symbol || "-"}}</b></td>
          <td>${{x.side || "-"}}</td>
          <td>${{x.strength || "-"}}</td>
          <td>${{Number(x.institutional_score || 0).toFixed(1)}}</td>
          <td>${{x.zone || "-"}}</td>
          <td>${{x.phase || "-"}}</td>
          <td>${{x.indicator_alignment || "-"}}</td>
          <td style="font-size:10px">${{(x.precursor_evidence || []).join(" + ") || "-"}}</td>
          <td style="font-weight:bold">${{stage}}</td>`;
        izaBody.appendChild(tr);
    }});

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
            let blocker = (c.gate_status && c.gate_status.blocker && c.gate_status.blocker !== "NONE")
                ? `<br><small style="color:#e67e22">⛔ ${{c.gate_status.blocker}} (${{c.gate_status.confirmations || 0}}/2 conf)</small>` : "";
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
                <td style="color:${{stateColor}}; font-weight:bold;">${{c.state}}${{blocker}}</td>
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
        return jsonify(safe_json(_normalize_payload(cached))), 200
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
        pos = DASHBOARD_STATE.get("position")
        if STATE["open"] and STATE.get("current_symbol") and pos is None:
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
                "trade_type": STATE.get("trade_type"),
                "entry_type": STATE.get("entry_type"),
                "classification": STATE.get("classification"),
                "location": STATE.get("location"),
                "zone": STATE.get("zone_info"),
                "score": STATE.get("trade_score", 0),
                "narrative_classification": STATE.get("narrative_classification"),
                "narrative_confidence": STATE.get("narrative_confidence", 0.0),
                "confidence_level": STATE.get("confidence_level"),
                "current_confidence": STATE.get("current_confidence", 50.0),
                "market_regime": STATE.get("market_regime", "UNKNOWN"),
                "continuation_pressure": STATE.get("continuation_pressure", 50),
                "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
                "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
                "delay_tp1": STATE.get("delay_tp1", False)
            }
        health = MEMORY["health"].copy()
        health["errors"] = len(DASHBOARD_STATE["errors"])
        top5 = MEMORY.get("top_candidates", [])[:5] if "top_candidates" in MEMORY else []
        # Read-only route: watchlist lifecycle cleanup runs exclusively in the
        # runtime worker (core.runtime._service_watchlist_and_queue).
        watchlist_data = MEMORY.get("watchlist", {})
        # NO-ENTRY / BLOCKER feed: legacy narrative skips merged with the
        # structured pipeline gate feed (promotion/queue/zone/risk blockers).
        legacy_feed = list(MEMORY.get("no_entry_feed", []))
        gate_feed = [
            {
                "time": g.get("time"),
                "symbol": g.get("symbol"),
                "side": g.get("side") or "-",
                "reason": f"[{g.get('stage')}] {g.get('blocker')}: {g.get('detail', '')}",
                "score": 0,
            }
            for g in MEMORY.get("gate_feed", [])
        ]
        no_entry_feed = sorted(
            legacy_feed + gate_feed, key=lambda x: x.get("time") or 0
        )[-10:]
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
        target_symbol = STATE.get("current_symbol")
        if not target_symbol:
            radar = MEMORY.get("radar_top5", [])
            if radar:
                target_symbol = radar[0].get("symbol")
            else:
                target_symbol = DEFAULT_SYMBOL

        if target_symbol:
            intent_data = MEMORY.get(f"intent_{target_symbol}", {})
            if not intent_data:
                store_intent_for_symbol(target_symbol)
                intent_data = MEMORY.get(f"intent_{target_symbol}", {})
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
            "positions": DASHBOARD_STATE.get("positions", []),
            "portfolio": DASHBOARD_STATE.get("portfolio", {"open_positions": 0, "max_positions": 0, "capacity": 0}),
            "news": [
                {"symbol": c.get("symbol"), "bias": c.get("news", {}).get("bias", "NEUTRAL"),
                 "risk": c.get("news", {}).get("risk", 0),
                 "available": c.get("news", {}).get("available", False),
                 "provider": c.get("news", {}).get("provider", "NONE"),
                 "headlines": c.get("news", {}).get("headlines", [])[:3]}
                for c in MEMORY.get("deep_scanner", [])[:8]
                if c.get("news")
            ],
            "logs": DASHBOARD_STATE["logs"][-30:],
            "errors": DASHBOARD_STATE["errors"][-10:],
            "top5": top5,
            "candidates": MEMORY.get("top_candidates", []),
            "deep_radar": MEMORY.get("deep_radar", [])[:50],
            "deep_scanner_last_scan": MEMORY.get("deep_scanner_last_scan", 0),
            "scanned_count": int(MEMORY.get("scanned_count", MEMORY.get("deep_discovery_count", 0))),
             "universe_size": int(MEMORY.get("deep_universe_size", 0)),
             "watchlist_active": int(MEMORY.get("watchlist_active", len(MEMORY.get("watchlist", {})))),
             "watchlist_deep_analyzed": int(MEMORY.get("watchlist_deep_analyzed", 0)),
             "watchlist_cycle_id": int(MEMORY.get("watchlist_cycle_id", 0)),
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
            "institutional_zone_analysis": list((MEMORY.get("institutional_zone_analysis") or {}).values()),
            "institutional_zone_count": int(MEMORY.get("institutional_zone_count", 0)),
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
            "allocation": MEMORY.get("portfolio_allocation", None),
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
                'promotions': int(MEMORY.get('watchlist_queue_promotions', 0)),
                'gate_stats': queue_status.get('gate_stats', {}),
                'candidates': queue_status['candidates'][:15]
            }
        else:
            payload['queue'] = {'enabled': False, 'promotions': 0}
        # Permanent pipeline telemetry: MARKET UNIVERSE -> DISCOVERY -> RADAR ->
        # WATCHLIST -> QUEUE -> READY -> VALIDATION/RISK -> EXECUTION counters.
        payload['pipeline'] = MEMORY.get('pipeline', {})
        
        safe_payload = safe_json(_normalize_payload(payload))
        cache_set("dashboard", safe_payload)
        return jsonify(safe_payload), 200
    except Exception as e:
        log_execution(f"/data error: {traceback.format_exc()}", "ERROR")
        return jsonify({"status": "ERROR", "error": str(e),
                        "data_quality": "UNAVAILABLE",
                        "health": {"dashboard": "DEGRADED"}}), 503

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

def _dashboard_payload_cached():
    cached = cache_get("dashboard", 5)
    if cached is not None:
        return safe_json(cached)
    return None

@app.route("/status")
def status_endpoint():
    payload = _dashboard_payload_cached()
    if payload is None:
        return jsonify({"status": "DEGRADED", "reason": "dashboard_cache_not_warmed"}), 200
    return jsonify({"status": payload.get("health", {}).get("status", "UNKNOWN"),
                    "mode": payload.get("mode"),
                    "health": payload.get("health", {}),
                    "last_scan": payload.get("last_scan", 0),
                    "deep_scanner_last_scan": payload.get("deep_scanner_last_scan", 0)}), 200

@app.route("/scanner")
def scanner_endpoint():
    return jsonify({"status": "OK", "universe_size": int(MEMORY.get("deep_universe_size", 0)),
                    "scanned_count": int(MEMORY.get("scanned_count", 0)),
                    "candidates": safe_json(MEMORY.get("top_candidates", []))}), 200

@app.route("/watchlist")
def watchlist_endpoint():
    # Read-only route: never mutates MEMORY["watchlist"].
    return jsonify({"status": "OK", "count": len(MEMORY.get("watchlist", {})),
                    "items": safe_json(MEMORY.get("watchlist", {}))}), 200

@app.route("/execution")
def execution_endpoint():
    if not USE_EXECUTION_QUEUE:
        return jsonify({"enabled": False, "status": "DISABLED", "candidates": []}), 200
    return jsonify(safe_json(queue.get_status())), 200

@app.route("/positions")
def positions_endpoint():
    positions = _normalize_payload(DASHBOARD_STATE.get("positions", []))
    return jsonify({"status": "OK", "positions": safe_json(positions),
                    "open_positions": len(positions)}), 200

@app.route("/portfolio")
def portfolio_endpoint():
    return jsonify(safe_json(_normalize_payload(DASHBOARD_STATE.get("portfolio", {"open_positions": 0, "max_positions": 6, "capacity": 6})))), 200

@app.route("/news")
def news_endpoint():
    payload = _dashboard_payload_cached() or {}
    return jsonify({"status": "OK", "items": payload.get("news", []),
                    "count": len(payload.get("news", []))}), 200

@app.route("/radar")
def radar_endpoint():
    radar = MEMORY.get("deep_radar", MEMORY.get("radar_top5", []))
    return jsonify({"status": "OK", "count": len(radar), "items": safe_json(radar[:50])}), 200

@app.route("/metrics")
def metrics_endpoint():
    return jsonify({"status": "OK", "stats": safe_json(DASHBOARD_STATE.get("stats", {})),
                    "scanner": {"universe": MEMORY.get("deep_universe_size", 0), "scanned": MEMORY.get("scanned_count", 0)},
                    "watchlist": {"active": MEMORY.get("watchlist_active", len(MEMORY.get("watchlist", {})))},
                    "queue": safe_json(queue.get_status()) if USE_EXECUTION_QUEUE else {"enabled": False}}), 200

@app.route("/trade", methods=["POST"])
def manual_trade():
    if not _control_authorized():
        return jsonify({"error": "Manual control authentication required"}), 403
    data = request.json or {}
    side = data.get("side")
    if side not in ["BUY", "SELL"]:
        return jsonify({"error": "Invalid side"}), 400

    # Manual dashboard trades now enter the same portfolio layer as scanner trades.
    from core.runtime import PORTFOLIO
    if PORTFOLIO.count() >= PORTFOLIO.max_positions:
        return jsonify({"error": "Portfolio capacity reached"}), 409

    symbol = data.get("symbol") or DEFAULT_SYMBOL
    price = get_ticker_safe(symbol)
    if not price or price <= 0:
        return jsonify({"error": "No price"}), 400
    df = get_ohlcv_safe(symbol, 100)
    if df is None:
        return jsonify({"error": "No data"}), 500
    atr = compute_atr(df).iloc[-1]
    sl = price - atr * 1.6 if side == "BUY" else price + atr * 1.6
    tp1 = price * 1.006 if side == "BUY" else price * 0.994
    tp2 = price * 1.02 if side == "BUY" else price * 0.98
    candidate = {
        "symbol": symbol, "side": side, "price": price,
        "sl": sl, "tp1": tp1, "tp2": tp2, "atr": atr,
        "score": 80, "scenario": "MANUAL", "news": {"bias": "NEUTRAL"},
    }
    ok = PORTFOLIO.open_candidate(candidate)
    return jsonify({"message": "Done" if ok else "Failed"}), 200 if ok else 500

@app.route("/close", methods=["POST"])
def manual_close():
    if not _control_authorized():
        return jsonify({"error": "Manual control authentication required"}), 403
    data = request.json or {}
    symbol = data.get("symbol")
    from core.runtime import PORTFOLIO
    targets = [symbol] if symbol else PORTFOLIO.symbols()
    if not targets:
        return jsonify({"error": "No position"}), 400
    closed = sum(1 for sym in targets if PORTFOLIO.close_symbol(sym))
    return jsonify({"message": f"Closed {closed} position(s)"}), 200

@app.route("/health")
def health():
    health_state = MEMORY.get("health", {}) or {}
    queue_state = "HEALTHY" if USE_EXECUTION_QUEUE else "DISABLED"
    return jsonify({
        "ok": True,
        "overall_status": health_state.get("status", "RUNNING"),
        "scanner_status": "HEALTHY" if MEMORY.get("last_scan") else "STARTING",
        "watchlist_status": "HEALTHY" if MEMORY.get("watchlist") is not None else "STARTING",
        "radar_status": "HEALTHY" if MEMORY.get("deep_radar") else "DEGRADED",
        "news_status": "ENABLED" if os.getenv("NEWS_ENABLED", "true").lower() in {"1", "true", "yes", "on"} else "DISABLED",
        "market_data_status": health_state.get("api", "UNKNOWN"),
        "execution_status": "LIVE" if MODE_LIVE else "PAPER",
        "dashboard_status": "HEALTHY",
        "queue_status": queue_state,
        "errors": int(health_state.get("errors", 0) or 0),
    }), 200

app.add_url_rule('/narrative-debug', 'narrative_debug', narrative_debug)

def keep_alive():
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            requests.get(f"http://localhost:{os.environ.get('PORT', 8000)}/health", timeout=5)
        except Exception as exc:
            log_execution(f"[HEALTH] keep_alive probe failed: {exc}", "WARN", debounce_key="keep_alive_probe", debounce_sec=60)

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

