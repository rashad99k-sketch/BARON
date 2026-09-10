"""P7: REAL runtime lifecycle end-to-end (READY -> allocator -> execution -> open).

Proves, through the REAL runtime executor (_execute_ready_queue_candidate, which
is the single production READY->EXECUTION path), with only the network/provider
boundary replaced:

  1. A genuinely READY candidate (the state the queue produces after confirmation
     persists on the candidate) is executed: pipeline accounting records
     READY -> EXECUTED, the candidate transitions to EXECUTED, and the lifecycle
     observability record gains allocator_time -> execution_time -> opened_time
     in monotonic order (Opportunity Survival Rate = 1.0).
  2. The global allocator still gates: a third CRYPTO READY candidate while two
     CRYPTO slots are already open is rejected at the allocator stage with the
     structured ALLOCATOR_REJECT gates, before any commit; no ghost position and
     no survival credit (protections not weakened).
  3. With no READY candidate, the executor returns without side effects
     (no_ready_candidate gate; no ghost paper position).

The queue -> READY transition (slow path, fast path, confirmations, ADX band,
READY score floor, ATOM gating) is covered by test_forensic_fixes.py and
test_pipeline_accounting.py; this test drives the surface those tests do not:
the actual runtime executor with PortfolioManager/allocator/execute_entry.
"""
import os
import sys
import types
import importlib
import time
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")

_ORIG_ENV = {k: os.environ.get(k) for k in ("USE_EXECUTION_QUEUE", "NEWS_ENABLED")}
os.environ.setdefault("USE_EXECUTION_QUEUE", "True")
os.environ.setdefault("NEWS_ENABLED", "False")


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        self.routes = {}

    def route(self, path, methods=None, **kwargs):
        return lambda fn: fn

    def add_url_rule(self, *args, **kwargs):
        return None


def _load_runtime():
    for name in list(sys.modules):
        if (name == "core.engine" or name == "core.runtime"
                or name.startswith("scanner.") or name.startswith("portfolio.")
                or name.startswith("news.") or name.startswith("strategy.")):
            sys.modules.pop(name, None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = {
                "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "type": "swap", "active": True},
                "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "type": "swap", "active": True},
                "SOL/USDT:USDT": {"base": "SOL", "quote": "USDT", "type": "swap", "active": True},
            }

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: a[0] if a else None
    fake_flask.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1", json=None)
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask

    import core.runtime as RT
    return RT


def _restore_modules(saved):
    """Restore the module table exactly as it was before this file's hermetic
    import, so a single-process full-suite run is not poisoned by the fake
    ccxt/flask boundary (DashboardReadOnlyTest needs the REAL flask.testing)."""
    sys.modules.clear()
    sys.modules.update(saved)
    for k, v in _ORIG_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _frame(n=250, side="BUY", base=100.0):
    """Synthetic frame the REAL execute_entry gates approve (ADX in class band,
    tail candle sweeps low-side liquidity for BUY -> sell_side_taken)."""
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    if str(side).upper() == "BUY":
        o[n - 2] = prior_low - 0.2
        c[n - 2] = prior_low + 0.3
        h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
        l[n - 2] = prior_low - 1.2
        o[n - 1] = prior_low + 0.1
        c[n - 1] = prior_low + 0.9
        h[n - 1] = prior_low + 1.3
        l[n - 1] = prior_low - 0.1
    else:
        o[n - 2] = prior_hi - 0.3
        c[n - 2] = prior_hi - 0.2
        l[n - 2] = max(prior_low + 0.1, prior_hi - 0.5)
        h[n - 2] = prior_hi + 1.2
        o[n - 1] = prior_hi - 0.1
        c[n - 1] = prior_hi - 0.9
        h[n - 1] = prior_hi + 0.1
        l[n - 1] = prior_hi - 1.3
    return pd.DataFrame({
        "timestamp": t, "open": o, "high": h, "low": l, "close": c,
        "volume": np.full(n, 1000.0),
    })


class PipelineLifecycleRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_modules = sys.modules.copy()
        cls.RT = _load_runtime()

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules)

    def setUp(self):
        RT = self.RT
        E = RT.E
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._prices = {"BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0,
                        "SOL/USDT:USDT": 150.0}
        self._frames = {s: _frame(base=p) for s, p in self._prices.items()}

        def provider(symbol, limit=120, htf=False):
            return self._frames.get(str(symbol))

        E.get_ohlcv_safe = provider
        E.get_ticker_safe = lambda symbol, retries=3: self._prices.get(str(symbol), 100.0)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[self._prices.get(str(a[0]), 100.0) - 1.0, 10.0]],
            "asks": [[self._prices.get(str(a[0]), 100.0) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]

        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.MEMORY.clear()
        E.MEMORY["pipeline"] = {"execution": {}}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        # Exercise the REAL kill-switch gate with a clean daily ledger.
        E.STATE["daily_loss_limit_hit"] = False
        E.STATE["last_trade_day"] = time.strftime("%Y-%m-%d")
        E.STATE["daily_peak_balance"] = E.paper["balance"]
        E.queue._candidates.clear()
        E.queue.total_executed = 0
        E.queue.total_rejected = 0

        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        RT.PORTFOLIO = PortfolioManager(6, E)
        RT.ALLOCATOR = GlobalAssetAllocator(RT.PORTFOLIO, E)
        RT.PORTFOLIO.bind(E)
        RT.PORTFOLIO.risk_guard._day = None
        RT.PORTFOLIO.risk_guard._consecutive_losses = 0
        RT.PORTFOLIO.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        RT = self.RT
        E = RT.E
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved

    def _govern_watch(self, symbol):
        RT = self.RT
        RT.E.MEMORY.setdefault("watchlist", {})[symbol] = {
            "symbol": symbol, "side": "BUY", "news_risk": 0,
            "asset_class": "CRYPTO",
        }

    def _ready_candidate(self, symbol, price):
        E = self.RT.E
        atr = price * 0.01
        cand = E.ExecutionCandidate(
            symbol=symbol, side="BUY", price=price,
            entry_price=price - atr * 0.5, stop_loss=price - atr * 1.6,
            take_profit_1=price + atr * 1.5, take_profit_2=price + atr * 2.5,
            atr=atr, df=self._frames[symbol], ob={},
        )
        cand.priority_score = 88.0
        cand.state = E.ExecutionState.READY
        cand.confirmation_count = 2
        cand.confirmation_state = "CONFIRMED_2"
        cand.confirmation_reason = "CONFIRMATION_COMPLETE"
        cand.ready_time = time.time()
        cand.ready_blocker = "NONE"
        cand.institutional_score = 85.0
        cand.pre_institutional_state = "CONFIRMED"
        cand.zone_low = price - atr * 0.6
        cand.zone_high = price + atr * 0.4
        cand.entry_distance_atr = 0.4
        cand.opportunity_type = E.OpportunityType.ACCUMULATION_ENTRY
        cand.asset_class = "CRYPTO"
        self._govern_watch(symbol)
        self.assertTrue(E.queue.add_candidate(cand), f"admit {symbol}")
        E.queue._record_opportunity_lifecycle(cand)
        return cand

    def test_ready_candidate_executed_with_lifecycle_timestamps(self):
        RT = self.RT
        E = RT.E
        cand = self._ready_candidate("BTC/USDT:USDT", 60000.0)

        executed = RT._execute_ready_queue_candidate()
        self.assertTrue(executed)

        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "executed")
        self.assertGreaterEqual(out.get("executed", 0), 1)

        c = E.queue._candidates.get("BTC/USDT:USDT")
        self.assertIsNotNone(c)
        self.assertEqual(c.state, E.ExecutionState.EXECUTED)
        self.assertEqual(RT.PORTFOLIO.count(), 1)

        rec = RT.MEMORY.get("opportunity_lifecycle", {}).get("BTC/USDT:USDT")
        self.assertIsNotNone(rec)
        for key in ("ready_time", "allocator_time", "execution_time", "opened_time"):
            self.assertGreater(rec.get(key, 0), 0, key)
        self.assertLessEqual(rec["ready_time"], rec["allocator_time"])
        self.assertLessEqual(rec["allocator_time"], rec["execution_time"])
        self.assertLessEqual(rec["execution_time"], rec["opened_time"])
        self.assertEqual(rec["state"], "EXECUTED")
        self.assertEqual(rec["primary_blocker"], "NONE")

        summary = E.queue.summarize_opportunity_lifecycle()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["survival_rate_ready_to_exec"], 1.0)

    def test_allocator_still_gates_third_crypto(self):
        RT = self.RT
        E = RT.E
        self._ready_candidate("BTC/USDT:USDT", 60000.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        # Margin deployment moves committed funds out of the paper balance; the
        # daily peak must track deployment (not a drawdown) for the REAL
        # kill-switch to stay clean across the two allowed CRYPTO slots.
        RT.E.STATE["daily_peak_balance"] = RT.E.paper["balance"]
        self._ready_candidate("ETH/USDT:USDT", 3000.0)
        self.assertTrue(RT._execute_ready_queue_candidate())
        RT.E.STATE["daily_peak_balance"] = RT.E.paper["balance"]
        self.assertEqual(RT.PORTFOLIO.count(), 2)

        sol = self._ready_candidate("SOL/USDT:USDT", 150.0)
        rejected = RT._execute_ready_queue_candidate()
        self.assertFalse(rejected)

        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "allocator_reject")
        self.assertGreaterEqual(out.get("allocator_reject", 0), 1)
        self.assertEqual(RT.PORTFOLIO.count(), 2, "no third slot")

        c = E.queue._candidates.get("SOL/USDT:USDT")
        self.assertIsNotNone(c)
        self.assertEqual(c.state, E.ExecutionState.READY, "rejected before commit")
        rec = RT.MEMORY.get("opportunity_lifecycle", {}).get("SOL/USDT:USDT", {})
        self.assertTrue(rec.get("ready_time", 0) > 0)
        self.assertFalse(rec.get("allocator_time"), "allocator gate blocked commit")
        self.assertFalse(rec.get("opened_time"))
        summary = E.queue.summarize_opportunity_lifecycle()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["allocator_accepted"], 2)
        self.assertEqual(summary["executed"], 2)
        self.assertEqual(summary["survival_rate_ready_to_exec"], round(2 / 3, 4))

    def test_no_ready_candidate_no_side_effects(self):
        RT = self.RT
        E = RT.E
        cand = self._ready_candidate("BTC/USDT:USDT", 60000.0)
        cand.state = E.ExecutionState.WAITING_TRIGGER
        cand.ready_time = 0.0

        executed = RT._execute_ready_queue_candidate()
        self.assertFalse(executed)
        out = RT.MEMORY.setdefault("pipeline", {}).setdefault("execution", {})
        self.assertEqual(out.get("last_outcome"), "no_ready_candidate")
        self.assertEqual(RT.PORTFOLIO.count(), 0, "no ghost paper position")
        self.assertEqual(len(RT.PORTFOLIO.snapshot()), 0)
        self.assertEqual(E.queue.total_executed, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)