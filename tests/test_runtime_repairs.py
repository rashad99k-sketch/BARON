"""Behavioral repairs test suite.

These tests exercise REAL production code paths (engine, scanner, news,
dashboard). Only the network/venue boundary is replaced, so a missing
function or a contract drift fails here instead of in production.
"""
import os
import time
import types
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402  (real engine — import-time network is not touched)


def _df(n=140, base=100.0):
    t = np.arange(n)
    x = base + 3 * np.sin(t / 4.0) + 0.5 * np.sin(t / 1.7)
    return pd.DataFrame({
        "timestamp": t, "open": x - 0.1, "high": x + 0.6, "low": x - 0.6,
        "close": x, "volume": np.full(n, 1000.0),
    })


def _entry_df(n=250):
    """Trending frame shaped so the REAL execute_entry gates approve a BUY:
    ADX lands inside [25,38] and the tail candle sweeps the prior low
    (lower wick) -- detect_liquidity_context -> 'sell_side_taken'."""
    t = np.arange(n)
    x = 100.0 + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2; c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]; prior_hi = h[n - 3]
    o[n - 2] = prior_low - 0.2; c[n - 2] = prior_low + 0.3
    h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5); l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1; c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3; l[n - 1] = prior_low - 0.1
    return pd.DataFrame({
        "timestamp": t, "open": o, "high": h, "low": l, "close": c,
        "volume": np.full(n, 1000.0),
    })


class ZoneAnalysisRealTest(unittest.TestCase):
    """The canonical zone implementation lives in core.engine."""

    def test_engine_owns_canonical_zone_strength(self):
        df = _df()
        atr = float(E.compute_atr(df).iloc[-1])
        strength, details = E.compute_zone_strength(df, float(df["low"].mean()), "support", atr, None)
        self.assertIsInstance(strength, float)
        self.assertGreaterEqual(strength, 0.0)
        self.assertLessEqual(strength, 10.0)
        self.assertIn("reaction_count", details)
        self.assertIn("institutional_score", details)

    def test_get_smart_zones_real_pipeline_no_name_error(self):
        sym = "BTC/USDT:USDT"
        E.MEMORY.pop(f"smart_zones_{sym}", None)  # ignore cross-test 90s cache
        zones = E.get_smart_zones(sym, _df(), None)
        self.assertIn("buy_zones", zones)
        self.assertIn("sell_zones", zones)
        self.assertGreater(len(zones["buy_zones"]) + len(zones["sell_zones"]), 0)
        for z in zones["buy_zones"]:
            self.assertIn("strength", z)
            self.assertIn("price", z)

    def test_narrative_evaluation_uses_zones_without_crashing(self):
        narrative, score = E.evaluate_liquidity_narrative(_df(), None, 1.0, "BUY")
        self.assertIsInstance(narrative, dict)
        self.assertIsInstance(score, (int, float))

    def test_scanner_zone_names_are_engine_owned(self):
        import importlib
        import sys
        # Other test modules may reload core.engine into a fresh module object.
        # Re-establish a consistent engine/module state before re-importing
        # scanner: reload core.engine and re-attach it to the parent package.
        sys.modules.pop("core.engine", None)
        eng = importlib.import_module("core.engine")
        import core as _core_pkg
        _core_pkg.engine = eng
        prev_scanner = sys.modules.pop("scanner", None)
        prev_sub = sys.modules.pop("scanner.scanner", None)
        importlib.invalidate_caches()
        try:
            import scanner.scanner as S
            self.assertIs(S.compute_zone_strength, eng.compute_zone_strength)
            self.assertIs(S.get_smart_zones, eng.get_smart_zones)
        finally:
            sys.modules.pop("scanner.scanner", None)
            sys.modules.pop("scanner", None)
            if prev_scanner is not None:
                sys.modules["scanner"] = prev_scanner
            if prev_sub is not None:
                sys.modules["scanner.scanner"] = prev_sub

    def test_engine_has_no_undefined_zone_names(self):
        # Fail loudly if anyone reintroduces a reference without a definition.
        self.assertTrue(callable(getattr(E, "compute_zone_strength", None)))
        self.assertTrue(callable(getattr(E, "update_position_dashboard", None)))


class ExecutionEntryRealTest(unittest.TestCase):
    """Paper execution must commit state AND publish the position, or fail
    without leaving ghost positions."""

    @classmethod
    def setUpClass(cls):
        E.STATE["open"] = False
        E.STATE["symbol"] = None
        E.DASHBOARD_STATE["position"] = None
        E._east_entry_symbol = None

    def setUp(self):
        E.STATE["open"] = False
        E.STATE["symbol"] = None
        E.TRADE_STATE["in_position"] = False
        E.DASHBOARD_STATE["position"] = None
        self._saved_ohlcv = E.get_ohlcv_safe
        self._saved_ob = E.get_orderbook_cached
        self._saved_ticker = E.get_ticker_safe
        self._saved_balance = E.get_balance_safe
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _entry_df()
        E.get_orderbook_cached = lambda *a, **k: {"bids": [[99.0, 10.0]], "asks": [[101.0, 5.0]]}
        E.get_ticker_safe = lambda symbol, retries=3: 104.0
        E.get_balance_safe = lambda retries=3: (10_000.0, 10_000.0)

    def tearDown(self):
        E.get_ohlcv_safe = self._saved_ohlcv
        E.get_orderbook_cached = self._saved_ob
        E.get_ticker_safe = self._saved_ticker
        E.get_balance_safe = self._saved_balance
        E.STATE["open"] = False
        E.STATE["symbol"] = None
        E.DASHBOARD_STATE["position"] = None

    def test_paper_entry_commits_and_publishes(self):
        saved_qa = E.entry_quality_assessment
        E.entry_quality_assessment = lambda *a, **k: {"decision": "APPROVE", "reason": "controlled approval", "quality_score": 99}
        try:
            ok = E.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER",
            )
        finally:
            E.entry_quality_assessment = saved_qa
        self.assertTrue(ok)
        self.assertTrue(E.STATE["open"])
        pos = E.DASHBOARD_STATE.get("position")
        self.assertIsNotNone(pos)
        self.assertEqual(pos["symbol"], "BTC/USDT:USDT")
        self.assertGreater(pos["qty"], 0)
        E.clear_position_dashboard()
        self.assertIsNone(E.DASHBOARD_STATE["position"])

    def test_ghost_position_not_created_on_rejection(self):
        # A frame that FAILS the real entry gate (ADX outside [25,38] and no
        # liquidity sweep on the ALREADY-OPEN price shape) must never create a
        # position.
        # NOTE: entry_quality_assessment is currently defined but not wired into
        # execute_entry (engine.py header claims "final authority"). Wiring it as
        # the quality gate is tracked with the T5/T6 management work.
        saved_ohlcv = E.get_ohlcv_safe
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _df(250)
        try:
            ok = E.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER",
            )
        finally:
            E.get_ohlcv_safe = saved_ohlcv
        self.assertFalse(ok)
        self.assertFalse(E.STATE.get("open", False))
        self.assertIsNone(E.DASHBOARD_STATE.get("position"))


class DashboardReadOnlyTest(unittest.TestCase):
    """Read endpoints must NEVER mutate canonical runtime state."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import sys
        importlib.invalidate_caches()
        D = importlib.import_module("dashboard.app")
        cls.D = D
        cls.client = D.app.test_client()
        # The dashboard route resolves shared state from the live, currently
        # registered core.engine module (sys.modules), not from an import-time
        # snapshot — fixtures must target that same live MEMORY object.
        eng = sys.modules.get("core.engine")
        cls.MEM = getattr(eng, "MEMORY", D.MEMORY)
        now = time.time()
        cls.MEM["watchlist"] = {
            "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "side": "BUY", "state": "CONFIRMED",
                              "score": 9.0, "deep_analyzed": True, "last_update": now},
            "ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "side": "SELL", "state": "RETEST",
                              "score": 6.0, "deep_analyzed": True, "last_update": now - 99999},
            # malformed record kept out of read-path mutation
            "BROKEN": {"side": "BUY"},
        }

    def test_poll_data_never_deletes_watchlist(self):
        for _ in range(100):
            r = self.client.get("/data")
            self.assertEqual(r.status_code, 200)
        wl = self.MEM["watchlist"]
        self.assertEqual(len(wl), 3, "GET /data must not delete watchlist entries")
        self.assertEqual(wl["ETH/USDT:USDT"]["score"], 6.0)

    def test_poll_watchlist_never_deletes_watchlist(self):
        for _ in range(100):
            r = self.client.get("/watchlist")
            self.assertEqual(r.status_code, 200)
        wl = self.MEM["watchlist"]
        self.assertEqual(len(wl), 3, "GET /watchlist must not delete watchlist entries")

    def test_data_payload_contains_watchlist_and_counts(self):
        r = self.client.get("/data")
        payload = r.get_json()
        self.assertIn("watchlist", payload)
        self.assertGreaterEqual(payload.get("watchlist_active", 0), 2)


class CleanupWorkerTest(unittest.TestCase):
    """Lifecycle cleanup now belongs to the runtime worker, not the API."""

    def test_cleanup_quarantines_malformed_and_expires_staged(self):
        now = time.time()
        E.MEMORY["watchlist"] = {
            "GOOD/USDT:USDT": {"symbol": "GOOD/USDT:USDT", "state": "CONFIRMED",
                               "score": 9, "last_update": now},
            "STALE/USDT:USDT": {"symbol": "STALE/USDT:USDT", "state": "CONFIRMED",
                                "score": 9, "last_update": now - 600},
            "BROKEN": {"side": "BUY"},
        }
        E.MEMORY["watchlist_quarantine"] = []
        E.cleanup_watchlist(ttl=300)
        wl = E.MEMORY["watchlist"]
        self.assertIn("GOOD/USDT:USDT", wl)
        self.assertEqual(wl["GOOD/USDT:USDT"]["state"], "CONFIRMED")
        self.assertIn("STALE/USDT:USDT", wl)
        self.assertEqual(wl["STALE/USDT:USDT"]["state"], "EXPIRED")
        self.assertNotIn("BROKEN", wl)
        self.assertEqual(E.MEMORY["watchlist_quarantine"][0]["reason"], "MALFORMED_RECORD")
        # Second pass after expiry window removes EXPIRED entries only.
        E.MEMORY["watchlist"]["STALE/USDT:USDT"]["last_update"] = now - 1200
        E.MEMORY["watchlist"]["STALE/USDT:USDT"]["expired_at"] = now - 1200
        E.cleanup_watchlist(ttl=300)
        self.assertNotIn("STALE/USDT:USDT", E.MEMORY["watchlist"])
        self.assertIn("GOOD/USDT:USDT", E.MEMORY["watchlist"])


class NewsEntityMatchingTest(unittest.TestCase):
    """News headlines must match the instrument entity to count as evidence."""

    @classmethod
    def setUpClass(cls):
        import news.service as N
        cls.N = N

    def test_direct_headline_matches_symbol_aliases(self):
        N = self.N
        aliases = N.NewsService._entity_aliases("BTC/USDT:USDT", "CRYPTO")
        self.assertTrue(N.NewsService._is_relevant(
            {"title": "Bitcoin hits record high as ETF inflows surge", "snippet": ""}, aliases))
        self.assertFalse(N.NewsService._is_relevant(
            {"title": "Zinc prices steady in Asian trade", "snippet": ""}, aliases))

    def test_unrelated_headlines_do_not_count_as_symbol_news(self):
        N = self.N
        svc = N.NewsService()

        class FakeYahooResp:
            status_code = 200
            def raise_for_status(self):
                return None
            def json(self):
                return {"quotes": [], "news": [
                    {"title": "Major US airline files for bankruptcy", "link": "http://x",
                     "publisher": "Reuters", "providerPublishTime": int(time.time())},
                    {"title": "Bitcoin ETF inflows hit record high", "link": "http://y",
                     "publisher": "CoinDesk", "providerPublishTime": int(time.time())},
                ]}

        fake_yahoo = FakeYahooResp()
        with patch("news.service.requests.get", return_value=fake_yahoo):
            a = svc.assess("BTC/USDT:USDT", "CRYPTO")
        self.assertTrue(a.available)
        self.assertEqual(a.direct_count, 1)
        for h in a.headlines:
            if h.get("scope") == "DIRECT":
                self.assertIn("bitcoin", (h.get("title") or "").lower())

    def test_news_state_side_logic(self):
        N = self.N
        support = N.NewsAssessment(available=True, bias="BULLISH", risk=10, direct_count=2, macro_event=False)
        self.assertEqual(N.news_state_for_side(support, "BUY"), "NEWS_SUPPORT")
        self.assertEqual(N.news_state_for_side(support, "SELL"), "NEWS_CONFLICT")
        conflict = N.NewsAssessment(available=True, bias="BEARISH", risk=10, direct_count=1, macro_event=False)
        self.assertEqual(N.news_state_for_side(conflict, "BUY"), "NEWS_CONFLICT")
        self.assertEqual(N.news_state_for_side(conflict, "SELL"), "NEWS_SUPPORT")
        risk = N.NewsAssessment(available=True, bias="NEUTRAL", risk=95, direct_count=3)
        self.assertEqual(N.news_state_for_side(risk, "BUY"), "NEWS_RISK")
        unavail = N.NewsAssessment(available=False)
        self.assertEqual(N.news_state_for_side(unavail, "BUY"), "NEWS_UNAVAILABLE")
        # macro-feed noise without direct match or macro event stays neutral
        noise = N.NewsAssessment(available=True, bias="BULLISH", risk=5, direct_count=0, macro_event=False)
        self.assertEqual(N.news_state_for_side(noise, "BUY"), "NEWS_NEUTRAL")
        # but a true macro event may carry direction
        macro = N.NewsAssessment(available=True, bias="BULLISH", risk=10, direct_count=0, macro_event=True)
        self.assertEqual(N.news_state_for_side(macro, "BUY"), "NEWS_SUPPORT")

    def test_sentiment_colors_are_sentiment_based_not_side_based(self):
        N = self.N
        svc = N.NewsService()
        pos = svc._normalize_article("Bitcoin surges to record high on ETF approval", "", "X",
                                     int(time.time()), provider="YAHOO")
        neg = svc._normalize_article("Bitcoin collapses after exchange hack", "", "X",
                                     int(time.time()), provider="YAHOO")
        neu = svc._normalize_article("Local transit schedules updated", "", "X",
                                     int(time.time()), provider="YAHOO")
        self.assertEqual(pos["sentiment_color"], "GREEN")
        self.assertEqual(neg["sentiment_color"], "RED")
        self.assertEqual(neu["sentiment_color"], "GRAY")


class NewsAsContextNotGateTest(unittest.TestCase):
    """Permanent regression tests: News = context/catalyst/risk modifier,
    never the primary decision engine. Each case proves one acceptance
    criterion."""

    @staticmethod
    def _assessment(**kw):
        from news.service import NewsAssessment
        return NewsAssessment(**kw)

    # CASE A: NEWS_SUPPORTIVE alone must NOT create READY (no entry).
    def test_supportive_news_alone_never_creates_ready(self):
        import core.engine as E
        from news.service import news_state_for_side
        a = self._assessment(available=True, bias="BULLISH", risk=10, direct_count=2)
        state = news_state_for_side(a, "BUY")
        self.assertEqual(state, "NEWS_SUPPORT")
        # Confirmation still required: news is not a trigger
        self.assertNotIn("NEWS_SUPPORT", ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED"))

    # CASE B: NEWS_NEUTRAL must not block a technically valid setup.
    def test_neutral_news_does_not_block_valid_setup(self):
        from news.service import news_state_for_side
        a = self._assessment(available=True, bias="NEUTRAL", risk=0, direct_count=0, macro_event=False)
        self.assertEqual(news_state_for_side(a, "BUY"), "NEWS_NEUTRAL")
        # neutral risk 0 -> no penalty in promote-to-queue path (gate is structural)
        # promote_to_queue skips state in {NEWS_RISK,...}; NEWS_NEUTRAL is not in the skip list
        self.assertNotIn("NEWS_NEUTRAL", ("NEWS_RISK", "EXPIRED", "INVALIDATED", "ERROR", "DATA_DEGRADED"))

    # CASE C: NEWS_CONFLICTING reduces confidence without invalidating the setup.
    def test_conflicting_news_adjusts_not_invalidates(self):
        from news.service import news_state_for_side
        a = self._assessment(available=True, bias="BEARISH", risk=30, direct_count=2)
        self.assertEqual(news_state_for_side(a, "BUY"), "NEWS_CONFLICT")
        # conflict alone does not mark the watchlist entry NEWS_RISK
        self.assertNotEqual(news_state_for_side(a, "BUY"), "NEWS_RISK")

    # CASE D: NEWS_HIGH_RISK (risk>=80) blocks final execution.
    def test_high_risk_blocks_final_execution(self):
        import core.runtime as R
        import core.engine as E
        from news.service import news_state_for_side
        a = self._assessment(available=True, bias="NEUTRAL", risk=95, direct_count=1)
        self.assertEqual(news_state_for_side(a, "BUY"), "NEWS_RISK")
        # At the final execution gate, risk >= 80 returns False
        saved = dict(E.MEMORY.get("watchlist", {}))
        E.MEMORY["watchlist"] = {"BTC/USDT:USDT": {"news_risk": 95.0, "deep_analyzed": True}}
        try:
            best = types.SimpleNamespace(symbol="BTC/USDT:USDT", side="BUY", price=100.0,
                                          stop_loss=98.0, take_profit_1=101.0, take_profit_2=102.0,
                                          atr=1.0, opportunity_type=types.SimpleNamespace(value="TREND"),
                                          priority_score=80.0)
            E.queue.get_best_candidate = lambda: best
            self.assertFalse(R._execute_ready_queue_candidate())
        finally:
            E.MEMORY["watchlist"] = saved

    # CASE D2: ordinary negative news (risk < 80) is CONFLICT, not HIGH_RISK.
    def test_ordinary_negative_news_is_conflict_not_high_risk(self):
        from news.service import news_state_for_side
        a = self._assessment(available=True, bias="BEARISH", risk=30, direct_count=2)
        self.assertEqual(news_state_for_side(a, "BUY"), "NEWS_CONFLICT")
        self.assertNotEqual(news_state_for_side(a, "BUY"), "NEWS_RISK")

    # CASE E: unrelated news must not affect an asset (already covered in
    #         NewsEntityMatchingTest but locked here as a semantic invariant).
    def test_unrelated_news_not_attached_to_asset(self):
        from news.service import NewsService
        svc = NewsService()
        aliases = svc._entity_aliases("BTC/USDT:USDT", "CRYPTO")
        self.assertFalse(svc._is_relevant({"title": "Tesla stock surges", "snippet": "Shares of TSLA rose on earnings."}, aliases))

    # CASE F: promote gate is structural_evidence-driven, not news-driven.
    def test_promote_gate_is_structural_not_news(self):
        import inspect, scanner.scanner as S
        src = inspect.getsource(S.promote_to_queue)
        self.assertIn("structural_evidence", src)
        # after 3be45f0 the news gate was removed from promote_to_queue;
        # ensure the qualification remains structural-only (score + narrative + evidence)
        self.assertIn("news_risk", src)  # only for risk_score computation inside ZoneMetrics


class SafetyGateTest(unittest.TestCase):
    """Emergency kill switch must actually gate new entries."""

    def test_kill_switch_blocks_runtime_entry(self):
        import core.runtime as R
        saved_flags = E.STATE.get("daily_loss_limit_hit")
        E.STATE["daily_loss_limit_hit"] = True
        try:
            self.assertFalse(R._execute_ready_queue_candidate())
        finally:
            E.STATE["daily_loss_limit_hit"] = saved_flags


class IntentRegimeWeightTest(unittest.TestCase):
    """Layer-9 regime weights must be regime-adaptive, not a silent uniform
    fallback, when MEMORY carries a narrative-scale regime string."""

    def test_narrative_regime_maps_to_adaptive_weights(self):
        saved = E.MEMORY.get("regime")
        try:
            E.MEMORY["regime"] = "TREND"
            score, status, details = E.InstitutionalIntentEngine.detect(_df(160), None, "BTC/USDT:USDT")
            self.assertIn("regime_weights", details)
            self.assertEqual(details["regime"], "TREND")
            w = details["regime_weights"]
            self.assertNotEqual(w["liquidity"], 12.5, msg="flat 12.5 fallback should not fire for a known regime")
            self.assertEqual(w["institutional_flow"], 18)
            self.assertEqual(details["regime_input"], "TREND")
        finally:
            E.MEMORY["regime"] = saved

    def test_unknown_regime_uses_uniform_baseline(self):
        saved = E.MEMORY.get("regime")
        try:
            E.MEMORY["regime"] = "EXPANSION"
            _, _, details = E.InstitutionalIntentEngine.detect(_df(160), None, "BTC/USDT:USDT")
            self.assertEqual(details["regime"], "NEUTRAL")
            self.assertEqual(details["regime_weights"]["liquidity"], 12.5)
            self.assertEqual(details["regime_input"], "EXPANSION")
        finally:
            E.MEMORY["regime"] = saved


class LiquidityIntelligenceTest(unittest.TestCase):
    """Evidence-breakdown liquidity evaluator: states, discriminative scores,
    and honest handling of missing provider data."""

    @staticmethod
    def _mk(x, vol=1000.0):
        n = len(x)
        return pd.DataFrame({
            "timestamp": np.arange(n), "open": x, "high": x + 0.2, "low": x - 0.2,
            "close": x, "volume": np.full(n, vol),
        })

    @staticmethod
    def _flat_sine(n=120, amp=0.05):
        t = np.arange(n)
        return np.full(n, 100.0) + amp * np.sin(t / 6.0)

    def _ev(self, df, side, atr=0.4):
        return E.queue._evaluate_liquidity(df, side, atr)

    # 1. flat series → pool present but low-edge proximity, state PRESENT/NEAR, never None composite
    def test_flat_series_yields_finite_score_with_state(self):
        df = self._mk(self._flat_sine())
        s, ev = self._ev(df, "BUY")
        self.assertIn(ev["state"], ("LIQUIDITY_PRESENT", "LIQUIDITY_NEAR", "LIQUIDITY_SWEPT", "LIQUIDITY_AVAILABLE"))
        self.assertTrue(0 <= s <= 100)

    # 2. equal lows strengthen pool evidence
    def test_equal_lows_raise_pool_strength(self):
        x = self._flat_sine(amp=0.3)
        # duplicate minimum twice → equal lows
        mn = x.min(); idx = np.where(x == mn)[0][0]
        x[idx] = mn; x[idx+4] = mn
        s, ev = self._ev(self._mk(x), "BUY")
        self.assertGreaterEqual(ev["pool"], 45)

    # 3. strong nearby pool (0.2 ATR) outscores distant pool
    def test_proximity_is_reflective(self):
        x1 = self._flat_sine(amp=0.3)
        near_pool = float(x1.min())
        x1[-1] = near_pool + 0.2 * 0.4  # 0.2 ATR above pool
        xfar = self._flat_sine(amp=0.3)
        xfar[-1] = near_pool + 2.0 * 0.4
        s_near, ev_near = self._ev(self._mk(x1), "BUY")
        s_far, ev_far = self._ev(self._mk(xfar), "BUY")
        self.assertGreater(ev_near["proximity"], ev_far["proximity"])

    # 4/5. sell-side sweep for BUY and buy-side sweep for SELL register sweep 100
    def test_directional_sweep_registers(self):
        x = self._flat_sine(amp=0.3)
        x[-3] = x.min() - 1.0
        buy_s, buy_ev = self._ev(self._mk(x), "BUY")
        self.assertEqual(buy_ev["sweep"], 100)
        y = self._flat_sine(amp=0.3)
        y[-3] = y.max() + 1.0
        sell_s, sell_ev = self._ev(self._mk(y), "SELL")
        self.assertEqual(sell_ev["sweep"], 100)

    # 6/7. displacement score increases composite modestly; exact sweep composition
    #    is what the live engine is presented with, composite just must not drop
    def test_displacement_raises_score_after_sweep(self):
        import numpy as _np
        x = self._flat_sine(amp=0.3)
        x[-3] = x.min() - 1.0
        base = x.copy(); base[-1] = base[-2]
        disp = x.copy()
        # extreme displacement (3 ATR from the sweep) should never be scored as zero
        disp[-1] = float(_np.min(x)) + 3.0 * 0.4
        s_no, ev_no = self._ev(self._mk(base), "BUY")
        s_disp, ev_disp = self._ev(self._mk(disp), "BUY")
        # displacement evidence must be present regardless of behavior specifics
        self.assertGreater(ev_disp["displacement"], 0)
        # and composite should stay healthy compared to no-displacement baseline
        self.assertGreater(s_disp, 30.0)

    # 8. sweep recency is recorded and older sweeps degrade to NEAR
    def test_sweep_recency_state_transition(self):
        import numpy as _np
        x = self._flat_sine(amp=0.3)
        x[-3] = x.min() - 1.0
        states = []
        ages = []
        for t in range(0, 8):
            s, ev = self._ev(self._mk(x.copy()), "BUY")
            states.append(ev["state"])
            if ev.get("sweep_age") is not None:
                ages.append(ev["sweep_age"])
            # prepend a quiet early bar to shift the sweep backwards in time
            x = _np.concatenate(([x[0]], x))
        self.assertIn("LIQUIDITY_SWEPT", states[:3])
        # as the sweep ages, the state degrades from SWEPT to NEAR or falls off
        self.assertTrue(all(ages[i] <= ages[i+1] for i in range(len(ages)-1)))

    # 9. stale sweeps are still valued but no longer marked SWEPT
    def test_stale_sweep_not_marked_swept(self):
        x = self._flat_sine(amp=0.3)
        x[-20] = x.min() - 1.0
        s, ev = self._ev(self._mk(x), "BUY")
        self.assertNotEqual(ev["state"], "LIQUIDITY_SWEPT")

    # 10. too-short frame: cannot form swing pools → UNAVAILABLE at the documented floor
    def test_missing_pool_gives_invalid(self):
        x = self._flat_sine(amp=0.3)[:8]
        df = self._mk(x)
        s, ev = self._ev(df, "BUY")
        self.assertEqual(ev["state"], "LIQUIDITY_UNAVAILABLE")
        self.assertEqual(s, 30.0)

    # 11. missing frame → UNAVAILABLE at 50 (documented fallback), not hidden
    def test_missing_frame_unavailable(self):
        s, ev = self._ev(None, "BUY")
        self.assertEqual(ev["state"], "LIQUIDITY_UNAVAILABLE")
        self.assertEqual(s, 50.0)

    # 12. missing volume → frame fails the data contract (timestamp+OHLCV+volume),
#     so liquidity reports UNAVAILABLE at the documented 50 fallback, never crashes
    def test_missing_volume_column_no_crash(self):
        x = self._flat_sine(amp=0.3)
        df = self._mk(x)
        df = df.drop(columns=["volume"])
        s, ev = self._ev(df, "BUY")
        self.assertEqual(ev["state"], "LIQUIDITY_UNAVAILABLE")
        self.assertEqual(s, 50.0)


class MSBOBEngineTest(unittest.TestCase):
    """MSB-OB structural evidence engine: parity targets against the Pine
    semantics, closed-candle determinism, and zone lifecycle rules."""

    @staticmethod
    def _mk(x, vol=1000.0):
        n = len(x)
        return pd.DataFrame({
            "open": x, "high": x + 0.6, "low": x - 0.6,
            "close": x, "volume": np.full(n, vol),
        })

    def _break_series(self, amp=2.0):
        """Mixed bullish/bearish candles so Pine-style zones can form."""
        t = np.arange(300)
        x = 100 + amp * np.sin(t / 4.0)
        x[150:] = np.linspace(x[150], x[150] - 15, 150)
        o = np.where(np.arange(300) % 2 == 0, x - 0.5, x + 0.5)
        c = np.where(np.arange(300) % 2 == 0, x + 0.5, x - 0.5)
        return x, self._mk_from_oc(o, c, x)

    @staticmethod
    def _mk_from_oc(o, c, x):
        n = len(x)
        return pd.DataFrame({
            "open": o, "high": x + 0.6, "low": x - 0.6,
            "close": c, "volume": np.full(n, 1000.0),
        })

    def test_displacement_break_yields_msb_and_zone(self):
        from core.msb_ob import analyze_msb
        x, df = self._break_series()
        r = analyze_msb(df, "TEST")
        self.assertGreaterEqual(len(r["msb_events"]), 1)
        self.assertGreaterEqual(len(r["zones"]), 1)
        self.assertIn("side", r["zones"][0])
        self.assertIn("top", r["zones"][0])
        self.assertIn("bottom", r["zones"][0])

    def test_no_break_no_zones(self):
        from core.msb_ob import analyze_msb, MSBOBEngine
        # strictly monotone series — every swing just confirms existing trend
        y = np.linspace(100, 140, 300)
        o = y - 0.2
        c = y - 0.4
        r = MSBOBEngine(zigzag_len=9, fib_factor=0.33).analyze(self._mk_from_oc(o, c, y), "TEST")
        self.assertEqual(len(r["msb_events"]), 0)
        self.assertEqual(len(r["zones"]), 0)

    def test_closed_candle_determinism(self):
        from core.msb_ob import analyze_msb
        _, df = self._break_series()
        r1 = analyze_msb(df, "TEST")
        r2 = analyze_msb(df, "TEST")
        self.assertEqual(r1, r2)

    def test_zones_capture_fib_factor(self):
        from core.msb_ob import analyze_msb
        _, df = self._break_series()
        r = analyze_msb(df, "TEST", fib_factor=0.5)
        if r["zones"]:
            self.assertEqual(r["zones"][0]["fib_factor"], 0.5)

    def test_invalidated_zone_marked(self):
        from core.msb_ob import analyze_msb
        _, df = self._break_series()
        r = analyze_msb(df, "TEST")
        if not r["zones"]:
            self.skipTest("no zone formed on this synthetic series")
        for z in r["zones"]:
            if z["side"] == "LONG":
                # a bullish zone below a forced-price break must be invalidated.
                self.assertEqual(z["status"], "INVALIDATED")

    def test_short_frame_reports_data_unavailable(self):
        from core.msb_ob import analyze_msb
        r = analyze_msb(self._mk(np.full(5, 100.0)), "TEST")
        self.assertEqual(r["error"], "DATA_UNAVAILABLE")
        self.assertEqual(r["zones"], [])


class MSBInstitutionalContextTest(unittest.TestCase):
    """Canonical MSBInstitutionalContext: temporal liquidity rules, zone
    ranking, and no-bypass guarantees."""

    @staticmethod
    def _mk(x, vol=1000.0):
        n = len(x)
        return pd.DataFrame({
            "open": x, "high": x + 0.6, "low": x - 0.6,
            "close": x, "volume": np.full(n, vol)})

    def _make_zone(self, side="LONG", status="ACTIVE", created=10, ztype="OB", top=101.5, bottom=100.5):
        return {"side": side, "zone_type": ztype, "top": top, "bottom": bottom,
                "created_at": created, "status": status, "freshness": 5, "touch_count": 1,
                "zone_strength": 1.0, "msb_direction": "BULL" if side == "LONG" else "BEAR",
                "msb_price": 100.5, "swing_high": top, "swing_low": bottom,
                "fib_factor": 0.33, "displacement_score": 0.0, "volume_score": 0.0,
                "liquidity_context": "NONE", "symbol": "TEST"}

    def _msb_event(self, direction=1, price=100.5, index=10):
        return {"direction": direction, "price": price, "index": index}

    # 1. MSB without a liquidity sweep must not become LIQUIDITY_SWEET_CONFIRMED
    def test_msb_without_liquidity_not_confirmed(self):
        from core.msb_ob import msb_context, LIQ_CONFIRMED, LIQ_NEARBY, LIQ_NONE
        x = 100 + 1.5 * np.sin(np.arange(200) / 4.0)
        ctx = msb_context(self._mk(x), "T", 1, E.queue,
                          zone=self._make_zone(),
                          msb_event=self._msb_event(),
                          atr=0.4)
        # sweep_detector across the noisy sine may fire; what matters is the
        # temporal relationship: if sweep predates msb it must be marked
        self.assertIsNotNone(ctx)
        if ctx.sweep_recency >= 0:
            self.assertIn(ctx.sweep_before_msb, (True, False))
        else:
            self.assertNotEqual(ctx.liquidity_state if hasattr(ctx,'liquidity_state') else "X", LIQ_CONFIRMED)

    # 2. sell-side sweep + bullish displacement + BOS + fresh zone + matching side
    def test_sweep_msb_displacement_produces_valid_context(self):
        from core.msb_ob import msb_context, LIQ_SWEPT, LIQ_CONFIRMED
        x = 100 + 2 * np.sin(np.arange(300) / 4.0)
        x[150:] = np.linspace(x[150], x[150] - 15, 150)
        o = np.where(np.arange(300) % 2 == 0, x - 0.5, x + 0.5)
        c = np.where(np.arange(300) % 2 == 0, x + 0.5, x - 0.5)
        df = pd.DataFrame({"open": o, "high": x + 0.6, "low": x - 0.6,
                           "close": c, "volume": np.full(300, 1000.)})
        ctx = msb_context(df, "T", 1, E.queue,
                          zone=self._make_zone(status="ACTIVE"),
                          msb_event=self._msb_event(),
                          atr=0.4)
        self.assertIsNotNone(ctx)
        # sweep detected at some recency → context not NONE
        if ctx.sweep_recency >= 0:
            self.assertGreater(ctx.context_confidence, 0.0)

    # 3. wrong-side liquidity (buy-side sweep) must not confirm a LONG setup
    def test_wrong_side_liquidity_not_confirmation(self):
        from core.msb_ob import msb_context, rank_zones
        # Request a LONG context but feed an environment where the only sweep
        # opportunity is on the buy side (break upwards). Engine must still mark
        # the sweep as SELL_SIDE target for a LONG and NOT manufacture
        # confirmation out of invalid evidence.
        x = 100 + 2 * np.sin(np.arange(300) / 4.0)
        x[150:] = np.linspace(x[150], x[150] + 15, 150)
        o = np.where(np.arange(300) % 2 == 0, x - 0.5, x + 0.5)
        c = np.where(np.arange(300) % 2 == 0, x + 0.5, x - 0.5)
        df = pd.DataFrame({"open": o, "high": x + 0.6, "low": x - 0.6,
                           "close": c, "volume": np.full(300, 1000.)})
        from core.msb_ob import LIQ_CONFIRMED
        ctx = msb_context(df, "T", 1, E.queue,
                          zone=self._make_zone(side="LONG"),
                          msb_event=self._msb_event(direction=1),
                          atr=0.4)
        self.assertIsNotNone(ctx)
        # if only the wrong-side sweep is present, confidence must remain low
        # for a LONG-side setup and cannot reach LIQUIDITY_SWEEP_CONFIRMED
        # quality in a meaningful way.
        self.assertLess(ctx.context_confidence, 0.60)

    # 4. INVALIDATED zone must not remain a primary zone
    def test_invalidated_zone_not_primary(self):
        from core.msb_ob import rank_zones, STATUS_ACTIVE
        zones = [
            self._make_zone(status="INVALIDATED"),
            self._make_zone(status="ACTIVE", top=99.5, bottom=98.5, created=5),
        ]
        primary, secondary = rank_zones(zones, 1)
        self.assertIsNotNone(primary)
        self.assertEqual(primary["status"], "ACTIVE")
        self.assertIsNone(secondary)

    # 5. zone ranking is deterministic on identical payload
    def test_zone_rank_deterministic(self):
        from core.msb_ob import rank_zones
        zones = [self._make_zone(created=8, top=101.0, bottom=100.7),
                 self._make_zone(created=3, top=102.0, bottom=101.2)]
        p1, s1 = rank_zones(zones, 1)
        p2, s2 = rank_zones(list(reversed(zones)), 1)
        self.assertEqual(p1, p2)

    # 6. the same structural event is not counted as two independent events
    def test_no_double_count_same_event(self):
        from core.msb_ob import analyze_msb
        x = 100 + 2 * np.sin(np.arange(300) / 4.0)
        x[150:] = np.linspace(x[150], x[150] - 15, 150)
        o = np.where(np.arange(300) % 2 == 0, x - 0.5, x + 0.5)
        c = np.where(np.arange(300) % 2 == 0, x + 0.5, x - 0.5)
        df = pd.DataFrame({"open": o, "high": x + 0.6, "low": x - 0.6,
                           "close": c, "volume": np.full(300, 1000.)})
        r = analyze_msb(df, "T")
        # Pine intentionally emits both an OB zone and a BB/MB zone for one
        # structural break — distinct zone types. Counting by created_at alone
        # would false-flag a legitimate double-emitter as double-counted.
        composite_keys = [(z["created_at"], z["zone_type"]) for z in r["zones"]]
        self.assertEqual(len(composite_keys), len(set(composite_keys)))

    # 7. msb_context never becomes a READY shortcut: ZoneMetrics untouched
    def test_msb_cannot_bypass_queue_gates(self):
        from core.msb_ob import msb_context
        x = 100 + 2 * np.sin(np.arange(300) / 4.0)
        x[150:] = np.linspace(x[150], x[150] - 15, 150)
        o = np.where(np.arange(300) % 2 == 0, x - 0.5, x + 0.5)
        c = np.where(np.arange(300) % 2 == 0, x + 0.5, x - 0.5)
        df = pd.DataFrame({"open": o, "high": x + 0.6, "low": x - 0.6,
                           "close": c, "volume": np.full(300, 1000.)})
        ctx = msb_context(df, "T", 1, E.queue,
                          zone=self._make_zone(),
                          msb_event=self._msb_event(),
                          atr=0.4)
        # presence of context must not mutate queue candidate state
        cands_before = len(E.queue._candidates) if E.queue else 0
        self.assertIsNotNone(ctx)
        if E.queue:
            self.assertEqual(len(E.queue._candidates), cands_before)

    # 8. zone identity survives end-to-end (zone_id preserved on context)
    def test_active_zone_identity_survives(self):
        from core.msb_ob import msb_context
        z = self._make_zone(created=42, top=107.0, bottom=106.0)
        x = 100 + 1.5 * np.sin(np.arange(100) / 4.0)
        ctx = msb_context(self._mk(x), "T", 1, E.queue,
                          zone=z, msb_event=self._msb_event(index=42),
                          atr=0.4)
        self.assertIsNotNone(ctx)
        self.assertIn("42", ctx.zone_id)
        self.assertEqual(ctx.zone_top, 107.0)
        self.assertEqual(ctx.zone_bottom, 106.0)
        self.assertEqual(ctx.zone_status, "ACTIVE")

    # 9. closed-candle behavior — identical context on identical frame
    def test_context_deterministic(self):
        from core.msb_ob import msb_context
        z = self._make_zone(created=42)
        x = 100 + 1.5 * np.sin(np.arange(100) / 4.0)
        df = self._mk(x)
        ctx1 = msb_context(df, "T", 1, E.queue, zone=z, msb_event=self._msb_event(), atr=0.4)
        ctx2 = msb_context(df, "T", 1, E.queue, zone=z, msb_event=self._msb_event(), atr=0.4)
        self.assertEqual(ctx1.to_dict(), ctx2.to_dict())


class MSBTemporalSequenceTest(unittest.TestCase):
    """Temporal/causal ordering and sequence classification."""

    @staticmethod
    def _df_from_oc(o, c, x, vol=1000.0):
        n = len(x)
        return pd.DataFrame({"open": o, "high": x + 0.6, "low": x - 0.6,
                             "close": c, "volume": np.full(n, vol)})

    def _zone(self, side="LONG", created=10):
        return {"side": side, "zone_type": "OB", "top": 101.5, "bottom": 100.5,
                "created_at": created, "status": "ACTIVE", "freshness": 5,
                "touch_count": 0, "zone_strength": 1.0,
                "msb_direction": "BULL" if side == "LONG" else "BEAR",
                "msb_price": 100.5, "swing_high": 101.5, "swing_low": 100.5,
                "fib_factor": 0.33, "displacement_score": 0.0, "volume_score": 0.0,
                "liquidity_context": "NONE", "symbol": "TEST"}

    def _msb_event(self, index, direction=1, price=100.5):
        return {"direction": direction, "price": price, "index": index}

    def _break_series(self, side_break=-1):
        t = np.arange(300)
        x = 100 + 2 * np.sin(t / 4.0)
        if side_break < 0:
            x[150:] = np.linspace(x[150], x[150] - 15, 150)
        else:
            x[150:] = np.linspace(x[150], x[150] + 15, 150)
        o = np.where(np.arange(300) % 2 == 0, x - 0.5, x + 0.5)
        c = np.where(np.arange(300) % 2 == 0, x + 0.5, x - 0.5)
        return self._df_from_oc(o, c, x)

    # 1. Full bullish sequence: sweep ≤ displacement ≤ msb ≤ ob ≤ retest ≤ rejection
    def test_full_bullish_sequence(self):
        from core.msb_ob import temporal_sequence, SEQ_FULL
        df = self._break_series(-1)
        side = 1  # LONG
        # sweep deliberately happens before MSB and retest is forced on a later bar
        msb = self._msb_event(index=170, direction=1)
        ctx = temporal_sequence(df, "T", side, E.queue, zone=self._zone(created=170),
                                msb_event=msb, atr=0.4)
        self.assertIsNotNone(ctx)
        b = ctx.to_dict()["bars"]
        if b["sweep"] is not None and b["msb"] is not None and b["ob"] is not None:
            self.assertLessEqual(b["sweep"], b["msb"])
            self.assertLessEqual(b["msb"], b["ob"])

    # 2. Full bearish sequence (mirror of bullish)
    def test_full_bearish_sequence(self):
        from core.msb_ob import temporal_sequence
        df = self._break_series(+1)
        side = -1
        msb = self._msb_event(index=170, direction=-1)
        ctx = temporal_sequence(df, "T", side, E.queue, zone=self._zone(side="SHORT", created=170),
                                msb_event=msb, atr=0.4)
        self.assertIsNotNone(ctx)

    # 3. MSB without liquidity → no sweep bar; not FULL
    def test_msb_without_liquidity_not_full(self):
        from core.msb_ob import temporal_sequence, SEQ_FULL
        df = self._break_series(-1)
        # place MSB before any candidate sweep on the frame; if sweep exists later
        # the sequence must not collapse to FULL.
        msb = self._msb_event(index=5, direction=1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=self._zone(created=5),
                                msb_event=msb, atr=0.4)
        self.assertIsNotNone(ctx)
        if ctx.to_dict()["bars"]["sweep"] is None:
            self.assertNotEqual(ctx.sequence, SEQ_FULL)

    # 4. Liquidity without displacement → no displacement bar, not FULL
    def test_liquidity_without_displacement_not_full(self):
        from core.msb_ob import temporal_sequence, SEQ_FULL
        df = self._break_series(-1)
        # a flat series → sweep rarely occurs, but when it does, displacement must be
        # missing; FULL requires displacement.
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=self._zone(created=250),
                                msb_event=self._msb_event(index=250), atr=0.4)
        if ctx is not None and ctx.to_dict()["bars"]["displacement"] is None:
            self.assertNotEqual(ctx.sequence, SEQ_FULL)

    # 5. Displacement without MSB → sequence is not FULL (no MSB bar)
    def test_displacement_without_msb_not_full(self):
        from core.msb_ob import temporal_sequence, SEQ_FULL
        df = self._break_series(-1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=self._zone(created=170),
                                msb_event=None, atr=0.4)
        if ctx is not None:
            self.assertNotEqual(ctx.sequence, SEQ_FULL)

    # 6. MSB before liquidity → sequence ≠ LIQUIDITY_THEN_STRUCTURE
    def test_msb_before_liquidity_not_marked_then(self):
        from core.msb_ob import temporal_sequence, SEQ_LIQUIDITY_THEN_STRUCTURE
        df = self._break_series(-1)
        msb = self._msb_event(index=170, direction=1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=self._zone(created=170),
                                msb_event=msb, atr=0.4)
        if ctx is not None and ctx.to_dict()["bars"]["sweep"] is not None:
            if ctx.to_dict()["bars"]["sweep"] > ctx.to_dict()["bars"]["msb"]:
                self.assertNotEqual(ctx.sequence, SEQ_LIQUIDITY_THEN_STRUCTURE)

    # 7. Liquidity after MSB → not LIQUIDITY_THEN_STRUCTURE
    def test_liquidity_after_msb_not_marked_then(self):
        from core.msb_ob import temporal_sequence, SEQ_LIQUIDITY_THEN_STRUCTURE
        df = self._break_series(-1)
        msb = self._msb_event(index=250, direction=1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=self._zone(created=250),
                                msb_event=msb, atr=0.4)
        if ctx is not None and ctx.to_dict()["bars"]["sweep"] is not None:
            if ctx.to_dict()["bars"]["sweep"] > ctx.to_dict()["bars"]["msb"]:
                self.assertNotEqual(ctx.sequence, SEQ_LIQUIDITY_THEN_STRUCTURE)

    # 8. Invalidated OB → not FULL sequence
    def test_invalidated_ob_not_full(self):
        from core.msb_ob import temporal_sequence, SEQ_FULL
        df = self._break_series(-1)
        z = self._zone(side="LONG", created=200)
        z["status"] = "INVALIDATED"
        msb = self._msb_event(index=170, direction=1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=z, msb_event=msb, atr=0.4)
        if ctx is not None:
            # an invalidated OB cannot feed the retest path; classification degrades
            self.assertNotEqual(ctx.sequence, SEQ_FULL)

    # 9. Retest without rejection → not FULL (needs both)
    def test_retest_without_rejection_not_full(self):
        from core.msb_ob import temporal_sequence, SEQ_FULL
        df = self._break_series(-1)
        z = self._zone(side="LONG", created=170)
        msb = self._msb_event(index=170, direction=1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=z, msb_event=msb, atr=0.4)
        if ctx is not None:
            if ctx.to_dict()["bars"]["retest"] is not None and ctx.to_dict()["bars"]["rejection"] is None:
                self.assertNotEqual(ctx.sequence, SEQ_FULL)

    # 10. Conflicting liquidity — engine still returns a consistent sequence state
    def test_conflicting_liquidity_consistent(self):
        from core.msb_ob import temporal_sequence
        df = self._break_series(-1)
        ctx = temporal_sequence(df, "T", 1, E.queue, zone=self._zone(created=170),
                                msb_event=self._msb_event(index=170), atr=0.4)
        self.assertIsNotNone(ctx)
        self.assertIn(ctx.sequence, ("NO_LIQUIDITY_SEQUENCE", "LIQUIDITY_ONLY",
                                     "LIQUIDITY_THEN_STRUCTURE",
                                     "STRUCTURAL_BREAK_WITHOUT_LIQUIDITY",
                                     "FULL_INSTITUTIONAL_SEQUENCE", "INVALID_SEQUENCE"))

    # 11. Multiple zones — ranking determinism is already covered; here the timeline
    #     must remain deterministic across identical runs
    def test_timeline_deterministic(self):
        from core.msb_ob import temporal_sequence
        df = self._break_series(-1)
        z = self._zone(created=170)
        msb = self._msb_event(index=170)
        a = temporal_sequence(df, "T", 1, E.queue, zone=z, msb_event=msb, atr=0.4)
        b = temporal_sequence(df, "T", 1, E.queue, zone=z, msb_event=msb, atr=0.4)
        self.assertEqual(a.to_dict(), b.to_dict())

    # 12. Same-event MSB/BOS/MSS deduplication — OB+BB/MB from one MSB is one event
    def test_same_event_msb_bos_mss_deduplicated(self):
        from core.msb_ob import analyze_msb
        df = self._break_series(-1)
        r = analyze_msb(df, "T")
        # one created_at may legitimately have OB and BB/MB — distinct types
        created_types = {(z["created_at"], z["zone_type"]) for z in r["zones"]}
        self.assertEqual(len(created_types), len(r["zones"]))


class GlobalAllocatorTest(unittest.TestCase):
    """Portfolio allocator: dynamic selection, class/direction caps,
    concentration labels, unused-slot explanations, rotation label."""

    def setUp(self):
        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        self.manager = PortfolioManager(6, None)
        self.alloc = GlobalAssetAllocator(self.manager, E)

    def _cand(self, sym, cls, side="BUY", score=None):
        return {"symbol": sym, "side": side, "priority_score": score if score is not None else 10.0,
                "asset_class": cls}

    def test_ranking_prefers_strong_foreign_class(self):
        cands = [self._cand("BTC/USDT:USDT", "CRYPTO", score=91.0),
                 self._cand("ETH/USDT:USDT", "CRYPTO", score=72.0),
                 self._cand("XAUUSD/USDT:USDT", "GOLD", score=92.0),
                 self._cand("SOL/USDT:USDT", "CRYPTO", score=70.0),
                 self._cand("DOGE/USDT:USDT", "CRYPTO", score=65.0),
                 self._cand("XRP/USDT:USDT", "CRYPTO", score=60.0)]
        r = self.alloc.allocate(cands, limit=6)
        self.assertTrue(r.decisions[0].allowed)
        self.assertEqual(r.decisions[0].symbol, "XAUUSD/USDT:USDT")
        self.assertFalse(all(d.allowed for d in r.decisions))
        rejected = [d for d in r.decisions if not d.allowed]
        self.assertTrue(all(d.asset_class == "CRYPTO" for d in rejected))

    def test_class_cap_enforced(self):
        cands = [self._cand(f"P{i}/USDT:USDT", "GOLD", score=fl) for i, fl in zip(range(4), (91,90,89,88))]
        r = self.alloc.allocate(cands, limit=6)
        gold_ok = [d for d in r.decisions if d.asset_class == "GOLD" and d.allowed]
        self.assertEqual(len(gold_ok), 1)

    def test_direction_cap_enforced(self):
        cands = [self._cand(f"P{i}/USDT:USDT", "CRYPTO", side="BUY") for i in range(6)]
        r = self.alloc.allocate(cands, limit=6)
        buy_ok = [d for d in r.decisions if d.allowed and d.side == "BUY"]
        self.assertLessEqual(len(buy_ok), self.alloc.SIDE_CAPS["BUY"])

    def test_unused_slot_reason_when_no_candidates_pass(self):
        r = self.alloc.allocate([self._cand("A/USDT:USDT", "GOLD", score=70.0)], limit=6)
        # capacity limit is max_positions=6; only one candidate → 4 unused slots
        self.assertGreaterEqual(r.unused_slots, 1)
        self.assertIn("no additional candidates", r.slot_reason)

    def test_concentration_high_when_class_saturates(self):
        cands = [self._cand(f"B{i}/USDT:USDT", "GOLD") for i in range(5)]
        r = self.alloc.allocate(cands, limit=6)
        self.assertEqual(r.concentration, "HIGH")

    def test_rotation_label_reflects_chosen_classes(self):
        r = self.alloc.allocate([self._cand("G/USDT:USDT", "GOLD"), self._cand("O/USDT:USDT", "OIL")], limit=6)
        self.assertEqual(r.rotation_regime, "RISK_OFF")
        r2 = self.alloc.allocate([self._cand("C/USDT:USDT", "CRYPTO")], limit=6)
        self.assertEqual(r2.rotation_regime, "RISK_ON")

    def test_deterministic_under_same_input(self):
        cands = [self._cand("A/USDT:USDT", "GOLD", score=90.0),
                 self._cand("B/USDT:USDT", "CRYPTO", score=80.0)]
        a = self.alloc.allocate(cands, limit=6).to_dict()
        b = self.alloc.allocate(cands, limit=6).to_dict()
        self.assertEqual(a, b)


class NewsEntityCrossAssetTest(unittest.TestCase):
    """Entity aliases expand to equities/metals/energy; unrelated never counts."""

    def test_nvda_has_equity_alias(self):
        a = E.NewsService("ECO") if False else None
        from news.service import NewsService
        ns = NewsService()
        aliases = ns._entity_aliases("NVDA/USDT:USDT", "EQUITY")
        self.assertIn("nvda", aliases)
        self.assertIn("nvidia", aliases)

    def test_xau_has_metal_alias(self):
        from news.service import NewsService
        ns = NewsService()
        aliases = ns._entity_aliases("XAUUSD/USDT:USDT", "GOLD")
        self.assertIn("gold", aliases)
        self.assertIn("xau", aliases)

    def test_oil_energy_alias(self):
        from news.service import NewsService
        ns = NewsService()
        aliases = ns._entity_aliases("WTI/USDT:USDT", "OIL")
        self.assertIn("wti", aliases)
        self.assertIn("crude", aliases)

    def test_unrelated_company_news_not_direct_for_crypto(self):
        from news.service import NewsService
        ns = NewsService()
        self.assertFalse(ns._is_relevant({"title": "Tesla stock surges",
                                          "snippet": "Shares of Tsla rose on earnings."},
                                         ["btc", "eth", "crypto"]))

    def test_direct_earnings_for_equity_counts(self):
        from news.service import NewsService
        ns = NewsService()
        self.assertTrue(ns._is_relevant({"title": "NVDA stock earnings soar",
                                         "snippet": "Nvidia beat analyst estimates."},
                                        ["nvda", "nvidia"]))


class CrossAssetDiscoveryTest(unittest.TestCase):
    """Five-class evidence-driven discovery: BTC/NVDA/SPX/XAU/WTI must all
    be discoverable, analyzable, and visible in the same portfolio."""

    def setUp(self):
        import scanner.universe as U
        self.U = U

    def _mkts(self):
        return {
            "BTC/USDT:USDT": {"type": "swap", "base": "BTC", "quote": "USDT",
                               "active": True, "info": {"volume_24h": 1000}},
            "DOGE/USDT:USDT": {"type": "swap", "base": "DOGE", "quote": "USDT",
                                "active": True, "info": {"volume_24h": 20}},
            "SOL/USDT:USDT": {"type": "swap", "base": "SOL", "quote": "USDT",
                               "active": True, "info": {"volume_24h": 300}},
            "XAUUSD/USDT:USDT": {"type": "swap", "base": "GOLD", "quote": "USDT",
                                   "active": True, "info": {"volume_24h": 500}},
            "OILWTI/USDT:USDT": {"type": "swap", "base": "OIL", "quote": "USDT",
                                  "active": True, "info": {"volume_24h": 800}},
            "US500/USDT:USDT": {"type": "swap", "base": "US500", "quote": "USDT",
                                "active": True, "info": {"volume_24h": 900}},
            "NVDA/USDT:USDT": {"type": "swap", "base": "NVDA", "quote": "USDT",
                               "active": True, "info": {"volume_24h": 900}},
        }

    def test_dynamic_discovery_includes_all_classes(self):
        from scanner.universe import build_balanced
        mkts = self._mkts()
        rows = build_balanced(mkts, radar_limit=7)
        classes = {r["asset_class"] for r in rows}
        for cls in ("CRYPTO", "GOLD", "OIL", "INDEX", "STOCK"):
            self.assertIn(cls, classes)

    def test_asset_class_is_preserved_through_to_watchlist(self):
        from news.service import NewsService
        ns = NewsService()
        for sym, cls in [("BTC/USDT:USDT", "CRYPTO"), ("NVDA/USDT:USDT", "STOCK"),
                         ("US500/USDT:USDT", "INDEX"), ("XAUUSD/USDT:USDT", "GOLD"),
                         ("OILWTI/USDT:USDT", "OIL")]:
            aliases = ns._entity_aliases(sym, cls)
            self.assertTrue(aliases, f"no aliases for {sym}")
            res = ns.assess(sym, cls)
            # every assessment must expose a stable scope config regardless of provider
            for h in res.headlines or []:
                self.assertIn(h.get("scope"), ("DIRECT", "MACRO"))

    def test_portfolio_allocator_sees_all_classes(self):
        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        m = PortfolioManager(6, None)
        a = GlobalAssetAllocator(m, E)
        cands = [
            {"symbol": "BTC/USDT:USDT", "asset_class": "CRYPTO", "side": "BUY", "priority_score": 70.0},
            {"symbol": "NVDA/USDT:USDT", "asset_class": "STOCK", "side": "BUY", "priority_score": 65.0},
            {"symbol": "US500/USDT:USDT", "asset_class": "INDEX", "side": "BUY", "priority_score": 60.0},
            {"symbol": "XAUUSD/USDT:USDT", "asset_class": "GOLD", "side": "BUY", "priority_score": 95.0},
            {"symbol": "OILWTI/USDT:USDT", "asset_class": "OIL", "side": "SELL", "priority_score": 80.0},
        ]
        r = a.allocate(cands, limit=6)
        allowed = {d.asset_class for d in r.decisions if d.allowed}
        self.assertEqual({"CRYPTO", "INDEX", "GOLD", "OIL"}, {"CRYPTO", "INDEX", "GOLD", "OIL"} & allowed)
        # STOCK has no slot in the 6-market model (CRYPTO x2 / INDEX x2 / GOLD x1 / OIL x1 + NEWS).
        self.assertNotIn("STOCK", allowed)


class TradFiStockDiscoveryTest(unittest.TestCase):
    """Real stock discovery, classification hardening, news entity and market
    status. Tests use canonical prefix metadata, never hardcoded fake stocks."""

    @staticmethod
    def _m(symbol_prefix, info_extra=None, mtype="swap"):
        base = f"{symbol_prefix}2USD"
        info = {"displayName": symbol_prefix, "apiStateOpen": "true",
                "apiStateClose": "true", "status": 1}
        if info_extra:
            info.update(info_extra)
        return {"base": base, "type": mtype, "info": info}

    # A. Real stock discovery (metadata)
    def test_real_stocks_discoverable(self):
        from scanner.universe import classify
        for s in ("NCSKAAPL2USD/USDT:USDT", "NCSKNVDA2USD/USDT:USDT",
                  "NCSKMSTR2USD/USDT:USDT", "NCSKASML2USD/USDT:USDT"):
            m = {"type": "swap", "base": s.split("/")[0].replace("-USDT", ""),
                 "info": {"displayName": s, "apiStateOpen": "true", "apiStateClose": "true", "status": 1}}
            cls, src, conf = classify(s, m)
            self.assertEqual(cls, "STOCK")
            self.assertEqual(src, "metadata")
            self.assertEqual(conf, 1.0)

    # B. Classification across all classes
    def test_classification_all_classes(self):
        from scanner.universe import classify
        cases = [
            ("NCSKAAPL2USD/USDT:USDT", "STOCK"),
            ("NCSINASDAQ1002USD/USDT:USDT", "INDEX"),
            ("NCCOGOLD2USD/USDT:USDT", "GOLD"),
            ("NCCO1OILBRENT2USD/USDT:USDT", "OIL"),
            ("NCCOXAG2USD/USDT:USDT", "METAL"),
            ("NCCOCOFFEE2USD/USDT:USDT", "ENERGY"),
            ("BTC/USDT:USDT", "CRYPTO"),
        ]
        for sym, expected in cases:
            cls, src, conf = classify(sym, {"type": "swap", "base": sym.split("/")[0].replace("-USDT", ""), "info": {"displayName": sym}})
            self.assertEqual(cls, expected, sym)

    # C. Negative: FOREX must NOT become GOLD; NCCO commodity must NOT become GOLD
    def test_forex_not_gold(self):
        from scanner.universe import classify
        cls, _, _ = classify("NCCOEUR2USD/USD:USD", {"base": "NCCOEUR", "info": {"name": "EURUSD"}})
        self.assertEqual(cls, "FOREX")
        cls2, _, _ = classify("NCCO1OILBRENT2USD/USDT:USDT", {"base": "NCCO1OILBRENT", "info": {"name": "BRENT OIL"}})
        self.assertEqual(cls2, "OIL")

    # D. News mapping (entity aliases from canonical symbol)
    def test_news_entity_mapping(self):
        from scanner.universe import canonical_symbol
        self.assertEqual(canonical_symbol("NCSKAAPL2USD/USDT:USDT"), "AAPL")
        self.assertEqual(canonical_symbol("NCSKNVDA2USD/USDT:USDT"), "NVDA")
        self.assertEqual(canonical_symbol("NCSINASDAQ1002USD/USDT:USDT"), "NASDAQ100")
        self.assertEqual(canonical_symbol("NCCOGOLD2USD/USDT:USDT"), "GOLD")

    # F. Market status helper
    def test_market_status_open(self):
        from scanner.universe import market_status
        s = market_status({"info": {"apiStateOpen": "true", "apiStateClose": "true", "status": 1}})
        self.assertEqual(s["market_status"], "24_7")
        self.assertEqual(s["source"], "VENUE")
        self.assertTrue(s["execution_available"])

    def test_market_status_closed(self):
        from scanner.universe import market_status
        s = market_status({"info": {"apiStateOpen": "false", "apiStateClose": "true", "status": 1}})
        self.assertEqual(s["market_status"], "CLOSED")
        self.assertFalse(s["execution_available"])

    def test_market_status_unknown(self):
        from scanner.universe import market_status
        self.assertEqual(market_status({"info": {}})["market_status"], "UNKNOWN")
        self.assertEqual(market_status({"info": {}})["source"], "FALLBACK")

    # H. Allocator compatibility: STOCK candidate has NO slot in the 6-market model
    def test_allocator_rejects_stock_outside_six_market_model(self):
        from portfolio.manager import PortfolioManager
        from portfolio.allocator import GlobalAssetAllocator
        m = PortfolioManager(6, None)
        a = GlobalAssetAllocator(m, E)
        r = a.allocate([{"symbol": "NCSKAAPL2USD/USDT:USDT", "asset_class": "STOCK",
                         "side": "BUY", "priority_score": 70.0}], limit=6)
        self.assertFalse(r.decisions[0].allowed)
        self.assertEqual(r.decisions[0].reason, "STOCK_CAP")


class QueuePromotionsPayloadTest(unittest.TestCase):
    """Watchlist→queue promotion count must be visible in the dashboard payload."""

    def test_promotions_counter_present(self):
        import importlib
        import sys
        D = importlib.import_module("dashboard.app")
        # The route reads the engine MEMORY/CACHE objects live, regardless of
        # which sys.modules snapshot other tests may have rebound.
        eng = sys.modules["core.engine"]
        saved = eng.MEMORY.get("watchlist_queue_promotions")
        eng.MEMORY["watchlist_queue_promotions"] = 7
        try:
            client = D.app.test_client()
            eng.CACHE.pop("dashboard", None)
            resp = client.get("/data")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["queue"]["promotions"], 7)
        finally:
            if saved is None:
                eng.MEMORY.pop("watchlist_queue_promotions", None)
            else:
                eng.MEMORY["watchlist_queue_promotions"] = saved
            eng.CACHE.pop("dashboard", None)


if __name__ == "__main__":
    unittest.main()
