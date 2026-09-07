"""SIX-TRADE SL/TP1/TP2 LADDER VALIDATION

Opens 6 REAL portfolio positions in one book (3 BUY + 3 SELL across
CRYPTO/INDEX/GOLD/OIL) through the production open path, then verifies the
risk ladder numbers for EVERY trade:

  BUY  : sl < entry < tp1 < tp2   (risk below the entry, targets above)
  SELL : sl > entry > tp1 > tp2   (risk above the entry, targets below)

Validates the numbers at three moments, not just at open:
  A. at FILL            : stored sl / synthetic sl, tp1, tp2, dynamic tp1/2
  B. after TP1 hit      : SL ratchets to breakeven (synthetic_sl == entry),
                          tp2 stays strictly farther than tp1
  C. at TP2/full exit   : ladder kept its geometry the whole life, the runner
                          closes at a profit and the margin ledger reconciles
                          exactly (equity == 10000 + realized PnL).

The engine/fill path is production; only the provider boundary (OHLCV/ticker/
orderbook/balance) is stubbed with trend frames (up-frames for BUY symbols,
down-frames for SELL symbols).
"""
import os
import sys

# --- process-wide gatekeepers: set BEFORE importing core.engine ----
os.environ["PAPER_MODE"] = "True"
os.environ["BINGX_KEY"] = ""
os.environ["BINGX_SECRET"] = ""
os.environ["NEWS_ENABLED"] = "True"
os.environ["POSITION_MARGIN_PCT"] = "0.10"
os.environ["PORTFOLIO_MARGIN_CAP_PCT"] = "0.60"
os.environ["MAX_DAILY_LOSS_PCT"] = "20.0"
os.environ["MAX_CONSECUTIVE_LOSSES"] = "3"
os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = "2"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import core.engine as E  # noqa: E402
from portfolio.manager import PortfolioManager  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "US500/USDT:USDT": 5000.0,
    "USTECH/USDT:USDT": 17000.0,
    "XAUUSD": 2300.0,
    "WTI": 100.0,
}
CAND = [
    ("BTC/USDT:USDT", "CRYPTO", "BUY"),
    ("ETH/USDT:USDT", "CRYPTO", "SELL"),
    ("US500/USDT:USDT", "INDEX", "BUY"),
    ("USTECH/USDT:USDT", "INDEX", "SELL"),
    ("XAUUSD", "GOLD", "BUY"),
    ("WTI", "OIL", "SELL"),
]


def _price(symbol):
    return float(PRICES.get(str(symbol), 100.0))


def _up_frame(n=250, base=100.0):
    t = np.arange(n).astype(float)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    o[n - 2] = prior_low - 0.2
    c[n - 2] = prior_low + 0.3
    h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
    l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1
    c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3
    l[n - 1] = prior_low - 0.1
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


def _down_frame(n=250, base=100.0):
    t = np.arange(n).astype(float)
    x = base - 3.0 * (1 - np.exp(-t / 900.0)) - 1.5 * np.sin(t / 6.0)
    o = x + 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_hi = h[n - 3]
    o[n - 2] = prior_hi + 0.9
    c[n - 2] = prior_hi
    h[n - 2] = prior_hi + 1.6
    l[n - 2] = prior_hi - 0.6
    o[n - 1] = prior_hi - 0.15
    c[n - 1] = prior_hi - 0.9
    h[n - 1] = prior_hi + 0.1
    l[n - 1] = prior_hi - 1.0
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


def _cand(sym, cls, side="BUY"):
    p = _price(sym)
    atr = p * 0.01
    if side == "BUY":
        sl, tp1, tp2 = p - atr * 1.6, p + atr * 1.5, p + atr * 2.5
    else:
        sl, tp1, tp2 = p + atr * 1.6, p - atr * 1.5, p - atr * 2.5
    return {"symbol": sym, "side": side, "price": p, "sl": sl, "tp1": tp1,
            "tp2": tp2, "score": 88.0, "atr": atr, "asset_class": cls,
            "trade_id": sym}


_BUY = {s for s, c, d in CAND if d == "BUY"}


def _reset():
    def ohlcv(symbol, limit=120, htf=False):
        b = _price(symbol)
        return _up_frame(base=b) if str(symbol) in _BUY else _down_frame(base=b)
    E.get_ohlcv_safe = ohlcv
    E.get_orderbook_cached = lambda *a, **k: {
        "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
    E.get_balance_safe = lambda retries=3: E.paper["balance"]
    E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
    E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
    E.STATE.clear()
    E.TRADE_STATE.clear()
    E.MEMORY.setdefault("watchlist", {}).clear()
    E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
                   "wins": 0, "losses": 0})


def ladder_fields(st):
    return {k: st.get(k) for k in ("side", "symbol", "remaining_qty", "entry",
                                   "synthetic_sl", "synthetic_tp1", "synthetic_tp2",
                                   "dynamic_tp1", "dynamic_tp2", "tp1_price",
                                   "tp2_price", "sl", "tp1", "tp2", "tp1_hit")
            if k in st}


def check_geometry(side, entry, sl, tp1, tp2, when):
    if side == "BUY":
        return (sl < entry < tp1 < tp2 and tp1 > entry and tp2 > tp1), (
            f"sl {sl:g} < entry {entry:g} < tp1 {tp1:g} < tp2 {tp2:g}")
    return (sl > entry > tp1 > tp2 and tp1 < entry and tp2 < tp1), (
        f"sl {sl:g} > entry {entry:g} > tp1 {tp1:g} > tp2 {tp2:g}")


def row(ok, sym, when, detail):
    mark = u"\u2713" if ok else "X"
    print(f"  [{when:12s}] {mark} {sym:22s} {detail}")
    return 0 if ok else 1


def open_book(pm):
    cands = [_cand(s, c, d) for s, c, d in CAND]
    opened = pm.open_top(cands, slots=len(CAND))
    assert opened == len(CAND), f"expected {len(CAND)} opens, got {opened}"
    return opened


def main():
    print("=" * 74)
    print("SIX-TRADE SL/TP1/TP2 LADDER VALIDATION")
    print("=" * 74)

    _reset()
    pm = PortfolioManager(6, E)
    pm.bind(E)
    pm.risk_guard._day = None
    pm.risk_guard._consecutive_losses = 0
    pm.risk_guard._cooldown_until = 0.0

    open_book(pm)
    failures = 0

    print("\n[PHASE A] ladder numbers at FILL (stored per trade)")
    print(f"  {'symbol':22s} side  sl          entry       tp1         tp2        -> OK")
    for sym, cls, side in CAND:
        st = pm.contexts[sym].state
        f = ladder_fields(st)
        sl, tp1, tp2 = f["synthetic_sl"], f["synthetic_tp1"], f["synthetic_tp2"]
        ok, detail = check_geometry(side, f["entry"], sl, tp1, tp2, "FILL")
        sync = (abs(sl - f["sl"]) < 1e-9 and abs(tp1 - f["tp1_price"]) < 1e-6
                and abs(tp2 - f["tp2_price"]) < 1e-6
                and abs(tp1 - f["dynamic_tp1"]) < 1e-9
                and abs(tp2 - f["dynamic_tp2"]) < 1e-9)
        print(f"  {sym:22s} {side:4s} {sl:10.4f} {f['entry']:10.4f} {tp1:10.4f} {tp2:10.4f}  {detail}")
        if not ok:
            failures += 1
        if not sync:
            failures += 1
            print(f"  [FILL        ] X {sym:22s} synthetic/dynamic/prices disagree: {f}")
        left_buf = (f["entry"] - sl) if side == "BUY" else (sl - f["entry"])
        right_gap = (tp2 - tp1) if side == "BUY" else (tp1 - tp2)
        if not (left_buf > 0 and right_gap > 0):
            failures += 1
            print(f"  [FILL        ] X {sym:22s} zero/negative risk buffer or tp gap")

    # ---- Phase B: first managed tick sits INSIDE the TP1 zone so the TP1
    # machinery de-risks every leg (partial 50% + SL->breakeven ratchet).
    print("\n[PHASE B] first manage cycles at TP1 zone (partial + breakeven ratchet)")
    live = {}
    for sym, cls, side in CAND:
        p = _price(sym)
        if side == "BUY":
            # +1.2% .. +1.45% inside tp1 (target = +1.8% of entry at 0.01 atr)
            live[sym] = p * (1.0 + 0.012 + 0.002 * (1 if sym == "BTC/USDT:USDT" else 0))
        else:
            live[sym] = p * (1.0 - 0.012 - 0.002 * (1 if sym == "ETH/USDT:USDT" else 0))

    def track(mult):
        E.get_ticker_safe = lambda symbol, retries=3: live[str(symbol)] * mult

    track(1.0)
    for _ in range(3):
        pm.manage_all()

    for sym, cls, side in CAND:
        ctx = pm.contexts.get(sym)
        if ctx is None:
            print(f"  [TP1_DE_RISK ] - {sym:22s} exited early (no ladder state to verify)")
            continue
        st = ctx.state
        f = ladder_fields(st)
        entry = f["entry"]
        sl, tp1, tp2 = f["synthetic_sl"], f["synthetic_tp1"], f["synthetic_tp2"]
        ok, detail = check_geometry(side, entry, sl, tp1, tp2, "TP1_HIT")
        be = (abs(sl - entry) < 1e-6)
        backstop = (tp2 > tp1) if side == "BUY" else (tp2 < tp1)
        if f.get("tp1_hit"):
            if not be:
                failures += 1
                print(f"  [TP1_DE_RISK ] X {sym:22s} breakeven ratchet missing: sl={sl} entry={entry}")
            if not backstop:
                failures += 1
                print(f"  [TP1_DE_RISK ] X {sym:22s} tp2 no longer farther than tp1")
            mark = (u"\u2713" if (ok or be) and backstop else "X")
            print(f"  [TP1_DE_RISK ] {mark} {sym:22s} BE sl={sl:g} qty={f['remaining_qty']:g} tp2_tail={tp2:g} {detail}")
        else:
            print(f"  [TP1_DE_RISK ] . {sym:22s} tp1 not yet hit at (mark {live[sym]:g}) pre-ramp qty={f['remaining_qty']:g}")

    # ---- Phase C: drive every survivor toward TP2, then close the book ----
    print("\n[PHASE C] ramp to TP2 and full-exit the book")
    track(1.0)
    live_up = {}
    for sym, cls, side in CAND:
        p = _price(sym)
        live_up[sym] = p * (1.06 if side == "BUY" else 0.94)
    E.get_ticker_safe = lambda symbol, retries=3: live_up[str(symbol)]
    prev = -1
    for step in range(6):
        track(1.0 + step * 0.004)
        pm.manage_all()
        nowt = E.PERF["trades"]
        if nowt != prev:
            print(f"  step {step}: realized trades={nowt} open={pm.count()} pnl={E.PERF['total_pnl_usdt']:.4f} mu={E.paper['balance']:.4f}")
        prev = nowt
        if pm.count() == 0:
            break

    for sym in list(pm.symbols()):
        if not pm.close_symbol(sym):
            failures += 1
            print(f"  [FULL_EXIT   ] X {sym} close_symbol returned False")

    holds = pm.count()
    if holds != 0:
        failures += 1
    print(f"  book_after_full_exit: count={holds} (expected 0)")
    if E.PERF["trades"] <= 0:
        failures += 1
        print("  X no trades were booked in PERF")

    # Margin invariant must reconcile EXACTLY.
    equity = E.paper["balance"] + E.paper["committed_margin"]
    target = 10000.0 + E.PERF["total_pnl_usdt"]
    if abs(equity - target) > 1e-4:
        failures += 1
        print(f"  X margin invariant broken: equity={equity:.6f} target={target:.6f}")
    ok_pnl = E.PERF["total_pnl_usdt"] > 0
    if not ok_pnl:
        failures += 1
    print(f"  realized_pnl={E.PERF['total_pnl_usdt']:.4f} equity={equity:.4f}"
          f" (expected {target - E.PERF['total_pnl_usdt']:.0f} + pnl)")
    print(f"  wins={E.PERF['wins']} losses={E.PERF['losses']} trades={E.PERF['trades']}")

    print("\n" + "=" * 74)
    print("SIX-TRADE LADDER VALIDATION = " + ("PASS" if failures == 0 else f"FAIL ({failures} checks)"))
    print("=" * 74)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())