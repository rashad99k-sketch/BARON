"""DEV-01 surgical conformance guard — BARON payload vs official BingX swap contract.

DEV-01 verdict was CONFIRMED (documented 2026-09-09 against the official
BingX Docs-v3 bundle app.0faf11fb00445c19dd82.js): every close and every
native SL sent ``reduceOnly=true`` together with ``positionSide=LONG/SHORT``,
which BingX Hedge Mode forbids ("do not send this parameter in Hedge Mode").

After the APPROVED surgical contract fix (2026-09-09) the execution layer no
longer passes ``reduceOnly`` on any hedge close/SL order.  This file is now the
regression guard for the FIXED contract:

  * It drives the REAL production close/SL functions in their LIVE branch and
    captures the EXACT arguments passed to ``ex.create_order``.
  * It translates those captured args through the REAL ccxt BingX adapter into
    the final HTTP request body BingX would receive, and checks it against the
    OFFICIAL contract.
  * Any capture that re-introduces ``reduceOnly`` (or drops ``positionSide``/
    ``stopPrice``/quantity) turns RED immediately.

BEFORE (DEV-01, BLOCKED)                                     AFTER (FIXED):
  LONG  partial/full close   MARKET SELL LONG reduceOnly=true   MARKET SELL LONG (no reduceOnly)
  SHORT partial/full close   MARKET BUY  SHORT reduceOnly=true  MARKET BUY  SHORT (no reduceOnly)
  LONG  native SL            STOP_MARKET SELL LONG reduceOnly   STOP_MARKET SELL LONG (no reduceOnly)
  SHORT native SL            STOP_MARKET BUY  SHORT reduceOnly  STOP_MARKET BUY  SHORT (no reduceOnly)

Official BingX Docs-v3 (bundle fetched 2026-09-09, POST
/openApi/swap/v2/trade/order + conditional-order notes + order schema):

  * reduceOnly  "true, false; Default value is false for single position mode;
                 This parameter is not accepted for both long and short
                 positions mode"
  * reduceOnly  "true, false; defaults to false in One-way Mode; do not send
                 this parameter in Hedge Mode"
  * notes       "Do not send reduceOnly in Hedge Mode. positionId is not
                 required in regular Hedge Mode; it is mandatory only when
                 closing a position in Separate Isolated mode"
  * closePosition "true, false; all position squaring after triggering, only
                 support STOP_MARKET and TAKE_PROFIT_MARKET; not used with
                 quantity; comes with only position squaring effect, not used
                 with reduceOnly"
  * positionSide "Position direction, required for single position as BOTH, for
                 both long and short positions only LONG or SHORT can be
                 chosen, defaults to LONG if empty"
  * response reduceOnly "This field is not used in Hedge Mode."

The "BingX contract gate" below models the official texts VERBATIM: reduceOnly
+ LONG/SHORT is always rejected (the external rule that now must never be
triggered by BARON); hedge closes without reduceOnly are accepted.
"""
import copy
import os
import unittest

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")

import core.engine as E  # noqa: E402  real production engine

_ENGINE_PAPER_MODE = E.PAPER_MODE          # baseline "True"
_ENGINE_EX = E.ex                          # real ccxt bingx instance
_MEMORY_BASE = copy.deepcopy(E.MEMORY)

_SWAP_SYM = "BTC/USDT:USDT"

# --- verbatim official contract texts ----------------------------------------
OFFICIAL_REDUCE_ONLY_HEDGE = (
    "do not send this parameter in Hedge Mode"
)
OFFICIAL_REDUCE_ONLY_NOT_ACCEPTED = (
    "This parameter is not accepted for both long and short positions mode"
)
OFFICIAL_DO_NOT_SEND_HEDGE = "Do not send reduceOnly in Hedge Mode."
OFFICIAL_POSITION_SIDE_RULE = (
    "for both long and short positions only LONG or SHORT can be chosen"
)


class _BingXContractGate:
    """Deterministic model of the OFFICIAL swap Place-Order contract.

    Models the docs verbatim:
      - positionSide LONG/SHORT  => hedge session.  reduceOnly must NOT be sent.
      - positionSide BOTH (or missing) => one-way session. reduceOnly allowed.
    Returns (accepted: bool, reason: str).
    """

    def check(self, request):
        pos = request.get("positionSide")
        reduce_only = "reduceOnly" in request
        if pos in ("LONG", "SHORT"):
            if reduce_only:
                return False, (
                    f"{OFFICIAL_DO_NOT_SEND_HEDGE} "
                    f"({OFFICIAL_REDUCE_ONLY_NOT_ACCEPTED}) reduceOnly=true sent "
                    f"with positionSide={pos}"
                )
            return True, f"hedge close accepted (positionSide={pos})"
        other = pos if pos is not None else "(empty)"
        if reduce_only:
            return True, f"one-way reduce accepted (positionSide={other})"
        return True, f"one-way accepted (positionSide={other})"


GATE = _BingXContractGate()


class _FakeVenue:
    """Replaces E.ex during LIVE-path capture: records create_order calls verbatim."""

    def __init__(self):
        self.calls = []
        # ccxt-compatible precision surface used by the audited code paths.
        self.markets = {_SWAP_SYM: {"id": "BTC-USDT", "symbol": _SWAP_SYM}}

    def amount_to_precision(self, symbol, amount):
        return float(amount)

    def price_to_precision(self, symbol, price):
        return float(price)

    def create_order(self, *args, **kwargs):
        self.calls.append({"args": tuple(args), "kwargs": dict(kwargs)})
        return {"id": "captured", "average": 60000.0, "price": 60000.0}

    def last(self):
        return self.calls[-1]

    def last_params(self):
        call = self.last()
        params = call["kwargs"].get("params")
        if params is None and len(call["args"]) > 4:
            params = call["args"][4]
        return dict(params or {})


class _CaptureHarness(unittest.TestCase):
    """Base: runs the REAL close functions in their LIVE branch and captures."""

    __test__ = False

    def setUp(self):
        self._reset_engine()
        self.venue = _FakeVenue()
        self._patched = []
        self._set(E, "PAPER_MODE", False)
        self._set(E, "ex", self.venue)
        self._set(E, "log_execution", lambda *a, **k: None)
        self._set(E, "normalize_symbol", lambda s: _SWAP_SYM)
        self._set(E, "resolve_exchange_symbol", lambda s: _SWAP_SYM)
        self._set(E, "verify_order_filled",
                  lambda symbol, order_id, side, qty, timeout=10: (True, qty))
        self._set(E, "fetch_position", lambda symbol: {"contracts": 0.0})
        self._set(E, "finalize_trade_with_reality", lambda *a, **k: None)
        self._set(E, "_exchange_sync",
                  type("_Sync", (), {"reconcile": staticmethod(lambda *a, **k: None)})())

    def tearDown(self):
        for obj, attr, old in reversed(self._patched):
            try:
                setattr(obj, attr, old)
            except Exception:
                pass
        self._patched.clear()
        self._reset_engine()

    def _set(self, obj, attr, value):
        old = getattr(obj, attr)
        self._patched.append((obj, attr, old))
        setattr(obj, attr, value)

    def _reset_engine(self):
        E._closing_in_progress = False
        E._reconciliation_pending = False
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.DASHBOARD_STATE.clear()
        E.paper.update({"balance": 10000.0, "position": None, "committed_margin": 0.0})
        E.PERF.update({"trades": 0, "wins": 0, "losses": 0, "total_pnl_usdt": 0.0,
                       "total_pnl_pct": 0.0, "last_trade": {}})
        E.MEMORY.clear()
        E.MEMORY.update(copy.deepcopy(_MEMORY_BASE))

    def _state(self, side, qty=1.0, entry=60000.0, mark=61200.0):
        state = {
            "open": True,
            "side": side,
            "entry": entry,
            "qty": qty,
            "qty_initial": qty,
            "remaining_qty": qty,
            "margin": 1000.0,
            "current_symbol": "BTC-USDT",
            "mark_price": mark,
            "synthetic_sl": entry * (0.98 if side == "BUY" else 1.02),
            "native_sl_state": "NONE",
            "realized_pnl_usdt": 0.0,
            "realized_pnl_pct": 0.0,
            "realized_legs": 0,
            "partial_realized": [],
        }
        return state

    def _capture_partial(self, side, ratio=0.5):
        self._reset_engine()
        qty = self._state(side)["remaining_qty"]
        E.STATE.update(self._state(side))
        # realistic post-close exchange qty: remaining after the partial leg
        E.fetch_position = lambda symbol: {"contracts": qty * (1 - ratio)}
        result = E.close_partial(ratio)
        return result, E.STATE, self.venue.last(), self.venue.last_params()

    def _capture_full(self, side):
        self._reset_engine()
        E.STATE.update(self._state(side))
        result = E.close_position_full()
        return result, E.STATE, self.venue.last(), self.venue.last_params()

    def _capture_native_sl(self, side):
        self._reset_engine()
        E.STATE.update(self._state(side))
        E.STATE["synthetic_sl"] = E.STATE["entry"] * (0.98 if side == "BUY" else 1.02)
        result = E.place_native_sl("BTC-USDT")
        return result, E.STATE, self.venue.last(), self.venue.last_params()


# ---------------------------------------------------------------------------
# 1) LIVE payload capture: close LONG / SHORT partial & full.
# ---------------------------------------------------------------------------
class ClosePartialLongPayloadTest(_CaptureHarness):
    __test__ = True

    def test_partial_close_long_sends_sell_market_hedge_payload(self):
        ok, st, call, params = self._capture_partial("BUY", 0.5)
        self.assertTrue(ok, "LIVE close_partial returned False")
        self.assertEqual(call["args"][1:3], ("market", "sell"),
                         "close LONG must post a SELL market order")
        self.assertAlmostEqual(float(call["args"][3]), 0.5, places=6,
                               msg="quantity must be remaining*ratio (0.5)")
        self.assertNotIn("reduceOnly", params,
                         "hedge close must NOT carry reduceOnly (DEV-01)")
        self.assertEqual(params.get("positionSide"), "LONG")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"BingX hedge contract must accept: {reason}")
        self.assertAlmostEqual(float(st["remaining_qty"]), 0.5, places=6,
                               msg="remaining after partial must be half")

    def test_partial_close_short_sends_buy_market_hedge_payload(self):
        ok, st, call, params = self._capture_partial("SELL", 0.5)
        self.assertTrue(ok)
        self.assertEqual(call["args"][1:3], ("market", "buy"),
                         "close SHORT must post a BUY market order")
        self.assertEqual(params.get("positionSide"), "SHORT")
        self.assertNotIn("reduceOnly", params,
                         "hedge close must NOT carry reduceOnly (DEV-01)")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"BingX hedge contract must accept: {reason}")
        self.assertAlmostEqual(float(st["remaining_qty"]), 0.5, places=6)


class CloseFullPayloadTest(_CaptureHarness):
    __test__ = True

    def test_full_close_long_sells_market_hedge_payload(self):
        ok, st, call, params = self._capture_full("BUY")
        self.assertTrue(ok, "LIVE close_position_full returned False")
        self.assertEqual(call["args"][1:3], ("market", "sell"))
        self.assertAlmostEqual(float(call["args"][3]), 1.0, places=6)
        self.assertEqual(params.get("positionSide"), "LONG")
        self.assertNotIn("reduceOnly", params,
                         "hedge close must NOT carry reduceOnly (DEV-01)")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"BingX hedge contract must accept: {reason}")
        self.assertFalse(bool(st["open"]), "position must be closed after full close")

    def test_full_close_short_buys_market_hedge_payload(self):
        ok, st, call, params = self._capture_full("SELL")
        self.assertTrue(ok)
        self.assertEqual(call["args"][1:3], ("market", "buy"))
        self.assertEqual(params.get("positionSide"), "SHORT")
        self.assertNotIn("reduceOnly", params,
                         "hedge close must NOT carry reduceOnly (DEV-01)")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"BingX hedge contract must accept: {reason}")
        self.assertFalse(bool(st["open"]))

    def test_emergency_close_fallback_uses_identical_hedge_payload(self):
        # emergency fallback path (engine.py:3706) reuses the same params shape
        E._closing_in_progress = False
        E._reconciliation_pending = False
        E.STATE.update(self._state("BUY"))
        E.verify_order_filled = lambda *a, **k: (False, 0.0)   # force fail -> emergency
        E.fetch_position = lambda symbol: {"contracts": 0.0}
        ok = E.close_position_full()
        self.assertTrue(ok, "emergency fallback should complete the close")
        params = self.venue.last_params()
        self.assertNotIn("reduceOnly", params,
                         "emergency close must NOT carry reduceOnly (DEV-01)")
        self.assertEqual(params.get("positionSide"), "LONG")


# ---------------------------------------------------------------------------
# 2) LIVE payload capture: native STOP_MARKET SL LONG / SHORT.
# ---------------------------------------------------------------------------
class NativeSlPayloadTest(_CaptureHarness):
    __test__ = True

    def test_native_sl_long_is_stop_market_sell_hedge_payload(self):
        ok, st, call, params = self._capture_native_sl("BUY")
        self.assertEqual(ok, "captured", "native SL must return order id")
        self.assertEqual(call["args"][1:3], ("STOP_MARKET", "sell"))
        self.assertIn("stopPrice", params)
        self.assertEqual(params.get("positionSide"), "LONG")
        self.assertNotIn("reduceOnly", params,
                         "hedge SL must NOT carry reduceOnly (DEV-01)")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"BingX hedge contract must accept: {reason}")
        self.assertEqual(st["native_sl_state"], "ACTIVE")

    def test_native_sl_short_is_stop_market_buy_hedge_payload(self):
        ok, st, call, params = self._capture_native_sl("SELL")
        self.assertEqual(ok, "captured")
        self.assertEqual(call["args"][1:3], ("STOP_MARKET", "buy"))
        self.assertIn("stopPrice", params)
        self.assertEqual(params.get("positionSide"), "SHORT")
        self.assertNotIn("reduceOnly", params,
                         "hedge SL must NOT carry reduceOnly (DEV-01)")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"BingX hedge contract must accept: {reason}")
        self.assertEqual(st["native_sl_state"], "ACTIVE")


# ---------------------------------------------------------------------------
# 3) Final ccxt HTTP request bodies (what BingX would actually receive).
# ---------------------------------------------------------------------------
class HttpRequestBodyTest(_CaptureHarness):
    """Translate captured LIVE args through the REAL ccxt BingX adapter."""

    __test__ = True

    @classmethod
    def setUpClass(cls):
        import ccxt
        cls.ex = ccxt.bingx({"enableRateLimit": True, "timeout": 20000})
        try:
            cls.ex.load_markets()
        except Exception as exc:  # pragma: no cover - network fallback
            raise unittest.SkipTest(f"cannot load public markets: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.ex = None

    def _body(self, order_type, side, amount, params):
        return self.ex.create_order_request(_SWAP_SYM, order_type, side, amount, None, params)

    def _assert_hedge_close_compliant(self, body, expect_ps):
        self.assertEqual(body.get("positionSide"), expect_ps)
        self.assertNotIn("reduceOnly", body,
                         f"final HTTP body must NOT carry reduceOnly (DEV-01): {body}")
        accepted, reason = GATE.check(body)
        self.assertTrue(accepted, f"BingX contract must accept: {reason}")

    def test_close_long_partial_http_body(self):
        ok, st, call, params = self._capture_partial("BUY", 0.5)
        body = self._body("market", call["args"][2], call["args"][3], params)
        self.assertEqual(body["type"], "MARKET")
        self.assertEqual(body["side"], "SELL")
        self.assertEqual(body["quantity"], 0.5)
        self._assert_hedge_close_compliant(body, "LONG")

    def test_close_short_full_http_body(self):
        ok, st, call, params = self._capture_full("SELL")
        body = self._body("market", call["args"][2], call["args"][3], params)
        self.assertEqual(body["side"], "BUY")
        self.assertEqual(body["quantity"], 1.0)
        self._assert_hedge_close_compliant(body, "SHORT")

    def test_native_sl_long_http_body(self):
        ok, st, call, params = self._capture_native_sl("BUY")
        body = self._body("STOP_MARKET", call["args"][2], call["args"][3], params)
        self.assertEqual(body["type"], "STOP_MARKET")
        self.assertEqual(body["side"], "SELL")
        self.assertTrue(body.get("stopPrice") is not None)
        self.assertEqual(body["quantity"], 1.0)
        self._assert_hedge_close_compliant(body, "LONG")

    def test_native_sl_short_http_body(self):
        ok, st, call, params = self._capture_native_sl("SELL")
        body = self._body("STOP_MARKET", call["args"][2], call["args"][3], params)
        self.assertEqual(body["type"], "STOP_MARKET")
        self.assertEqual(body["side"], "BUY")
        self.assertTrue(body.get("stopPrice") is not None)
        self.assertEqual(body["quantity"], 1.0)
        self._assert_hedge_close_compliant(body, "SHORT")

    def test_open_long_http_body_is_compliant(self):
        body = self._body("market", "buy", 0.0001,
                          {"leverage": 10, "positionSide": "LONG"})
        self.assertEqual(body["type"], "MARKET")
        self.assertEqual(body["side"], "BUY")
        self.assertEqual(body["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", body, "opens must never carry reduceOnly")
        accepted, reason = GATE.check(body)
        self.assertTrue(accepted, f"open LONG must be accepted: {reason}")

    def test_open_short_http_body_is_compliant(self):
        body = self._body("market", "sell", 0.0001,
                          {"leverage": 10, "positionSide": "SHORT"})
        self.assertEqual(body["side"], "SELL")
        self.assertEqual(body["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", body)
        accepted, reason = GATE.check(body)
        self.assertTrue(accepted, f"open SHORT must be accepted: {reason}")


# ---------------------------------------------------------------------------
# 4) Exit-scenario payload matrix: the 12 authorized scenarios, LIVE-captured.
#    Asserts side + positionSide + quantity + reduceOnly ABSENT + remaining +
#    no over-close + no reverse position + ledger reconciliation.
# ---------------------------------------------------------------------------
class ExitScenarioPayloadMatrixTest(_CaptureHarness):
    __test__ = True

    def _assert_close(self, label, call, params, expect_type, expect_side,
                      qty, remaining_after):
        self.assertEqual(call["args"][1], expect_type, f"{label}: order type")
        self.assertEqual(call["args"][2], expect_side, f"{label}: order side")
        self.assertAlmostEqual(float(call["args"][3]), qty, places=6,
                               msg=f"{label}: quantity")
        self.assertGreaterEqual(qty, 0.0, f"{label}: quantity must not be negative")
        self.assertLessEqual(float(call["args"][3]),
                             float(E.STATE["qty_initial"]),
                             f"{label}: over-close forbidden (qty > initial)")
        self.assertIn("positionSide", params, f"{label}: positionSide missing")
        self.assertNotIn("reduceOnly", params,
                         f"{label}: reduceOnly MUST be absent (DEV-01)")
        accepted, reason = GATE.check(params)
        self.assertTrue(accepted, f"{label}: BingX contract rejects: {reason}")
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), remaining_after,
                               places=6, msg=f"{label}: remaining")
        self.assertGreaterEqual(float(E.STATE["remaining_qty"]), 0.0,
                                f"{label}: remaining must never go negative")

    # ---- TP1 (partial) / Profit Lock (ratcheted partial) — LONG ----
    def test_tp1_and_profit_lock_partial_long(self):
        self._reset_engine()
        E.STATE.update(self._state("BUY"))
        E.fetch_position = lambda symbol: {"contracts": 0.5}
        ok = E.close_partial(0.5)               # TP1 / Profit Lock LONG partial
        self.assertTrue(ok, "TP1/ProfitLock LONG partial must close")
        call, params = self.venue.last(), self.venue.last_params()
        self._assert_close("TP1/ProfitLock LONG partial", call, params,
                           "market", "sell", 0.5, 0.5)
        self.assertEqual(params["positionSide"], "LONG")
        self.assertEqual(E.STATE["side"], "BUY",
                         "side must never reverse on a partial close")

    # ---- TP2 / Trailing / Strict Close / SL-hit — LONG full ----
    def test_tp2_trailing_strict_full_long(self):
        self._reset_engine()
        E.STATE.update(self._state("BUY"))
        # close_position_full clears the ledger via finalize_trade_with_reality
        E.finalize_trade_with_reality = lambda symbol: E.STATE.__setitem__("remaining_qty", 0.0)
        ok = E.close_position_full()            # TP2 / Trailing / Strict / SL LONG full
        self.assertTrue(ok)
        call, params = self.venue.last(), self.venue.last_params()
        self._assert_close("TP2/Trailing/Strict LONG full", call, params,
                           "market", "sell", 1.0, 0.0)
        self.assertEqual(params["positionSide"], "LONG")
        self.assertFalse(bool(E.STATE["open"]),
                         "full close must clear the position (no reverse)")

    # ---- SHORT partial & full ----
    def test_short_partial_and_full(self):
        self._reset_engine()
        E.STATE.update(self._state("SELL", entry=3000.0, mark=2940.0))
        E.fetch_position = lambda symbol: {"contracts": 0.5}
        ok = E.close_partial(0.5)               # SHORT partial (TP1/ProfitLock)
        self.assertTrue(ok)
        call, params = self.venue.last(), self.venue.last_params()
        self._assert_close("SHORT partial", call, params, "market", "buy", 0.5, 0.5)
        self.assertEqual(params["positionSide"], "SHORT")
        self.assertEqual(E.STATE["side"], "SELL", "side must never reverse")
        self._reset_engine()
        E.STATE.update(self._state("SELL", entry=3000.0, mark=2940.0))
        E.fetch_position = lambda symbol: {"contracts": 0.0}
        E.finalize_trade_with_reality = lambda symbol: E.STATE.__setitem__("remaining_qty", 0.0)
        ok = E.close_position_full()            # SHORT full (TP2/Trailing/Strict)
        self.assertTrue(ok)
        call, params = self.venue.last(), self.venue.last_params()
        self._assert_close("SHORT full", call, params, "market", "buy", 1.0, 0.0)
        self.assertEqual(params["positionSide"], "SHORT")
        self.assertFalse(bool(E.STATE["open"]))

    # ---- LONG / SHORT native protective SL ----
    def test_long_native_sl(self):
        self._reset_engine()
        E.STATE.update(self._state("BUY"))
        ok = E.place_native_sl("BTC-USDT")
        self.assertEqual(ok, "captured", "LONG native SL must be placed")
        call, params = self.venue.last(), self.venue.last_params()
        self._assert_close("LONG native SL", call, params,
                           "STOP_MARKET", "sell", 1.0, 1.0)
        self.assertEqual(params["positionSide"], "LONG")
        self.assertIn("stopPrice", params)
        self.assertEqual(E.STATE["native_sl_state"], "ACTIVE")

    def test_short_native_sl(self):
        self._reset_engine()
        E.STATE.update(self._state("SELL", entry=3000.0))
        E.STATE["synthetic_sl"] = 3060.0
        ok = E.place_native_sl("BTC-USDT")
        self.assertEqual(ok, "captured", "SHORT native SL must be placed")
        call, params = self.venue.last(), self.venue.last_params()
        self._assert_close("SHORT native SL", call, params,
                           "STOP_MARKET", "buy", 1.0, 1.0)
        self.assertEqual(params["positionSide"], "SHORT")
        self.assertAlmostEqual(float(params["stopPrice"]), 3060.0, places=3)
        self.assertEqual(E.STATE["native_sl_state"], "ACTIVE")

    # ---- Emergency Close fallback ----
    def test_emergency_close_long(self):
        self._reset_engine()
        E.STATE.update(self._state("BUY"))
        E.verify_order_filled = lambda *a, **k: (False, 0.0)  # force emergency path
        E.fetch_position = lambda symbol: {"contracts": 0.0}
        ok = E.close_position_full()
        self.assertTrue(ok, "emergency close must complete")
        call, params = self.venue.last(), self.venue.last_params()
        self.assertEqual(call["args"][1], "market")
        self.assertEqual(call["args"][2], "sell")
        self.assertEqual(params["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", params,
                         "emergency close must NOT carry reduceOnly (DEV-01)")
        self.assertFalse(bool(E.STATE["open"]))


# ---------------------------------------------------------------------------
# 5) PAPER ledger semantics — remaining / no-over-close / no-reverse /
#    reconciliation through the REAL engine in PAPER mode.
# ---------------------------------------------------------------------------
class PaperCloseSemanticsTest(unittest.TestCase):
    __test__ = True

    def setUp(self):
        self._orig_paper = E.PAPER_MODE
        E.PAPER_MODE = True
        self._orig_log = E.log_execution
        E.log_execution = lambda *a, **k: None
        self._orig_ticker = E.get_ticker_safe
        E.get_ticker_safe = lambda symbol, retries=0, **k: 61200.0
        self._orig_finalize = E.finalize_trade_with_reality
        E._closing_in_progress = False
        E._reconciliation_pending = False
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.DASHBOARD_STATE.clear()
        E.DASHBOARD_STATE["logs"] = []
        E.paper.update({"balance": 10000.0, "position": {}, "committed_margin": 1000.0})
        E.MEMORY.clear()
        E.MEMORY.update(copy.deepcopy(_MEMORY_BASE))

    def tearDown(self):
        E.PAPER_MODE = self._orig_paper
        E.log_execution = self._orig_log
        E.get_ticker_safe = self._orig_ticker
        E.finalize_trade_with_reality = self._orig_finalize
        # Test-only state isolation: seed()-style tests leave E.STATE open/closed
        # artifacts with 'current_symbol' / 'remaining_qty' set. If they leak,
        # later test modules that share the module-global STATE (e.g.
        # test_paper_audit_runtime) observe a stale 'open' flag. Reset the full
        # shared state after every test so nothing leaks across files.
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.DASHBOARD_STATE.clear()
        E.DASHBOARD_STATE["logs"] = []
        E.paper.update({"balance": 10000.0, "position": None, "committed_margin": 0.0})
        E._closing_in_progress = False
        E._reconciliation_pending = False

    def _seed(self, side, qty=1.0, entry=60000.0):
        E.STATE.update({
            "open": True, "side": side, "entry": entry, "qty": qty,
            "qty_initial": qty, "remaining_qty": qty, "margin": 1000.0,
            "current_symbol": "BTC-USDT", "mark_price": 61200.0,
            "synthetic_sl": entry * (0.98 if side == "BUY" else 1.02),
            "native_sl_state": "NONE",
            "realized_pnl_usdt": 0.0, "realized_pnl_pct": 0.0, "realized_legs": 0,
            "partial_realized": [],
        })
        E.paper["position"] = {"qty": qty, "remaining_qty": qty, "side": side,
                               "entry": entry, "symbol": "BTC-USDT"}

    def test_paper_partial_long_halves_remaining_twice(self):
        self._seed("BUY")
        E.close_partial(0.5)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.5, places=6)
        E.close_partial(0.5)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.25, places=6)
        self.assertAlmostEqual(float(E.paper["position"]["remaining_qty"]), 0.25,
                               places=6, msg="paper venue must track post-close size")

    def test_paper_partial_short_halves_remaining_twice(self):
        self._seed("SELL", entry=3000.0)
        E.STATE["entry"] = 3000.0
        E.paper["position"]["entry"] = 3000.0
        E.get_ticker_safe = lambda symbol, retries=0, **k: 2940.0
        E.close_partial(0.5)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.5, places=6)
        E.close_partial(0.5)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.25, places=6)

    def test_paper_full_close_clears_position(self):
        self._seed("BUY")
        E.finalize_trade_with_reality = lambda *a, **k: E.STATE.__setitem__("remaining_qty", 0.0)
        ok = E.close_position_full()
        self.assertTrue(ok)
        self.assertIsNone(E.paper["position"], "paper position must clear on full close")
        self.assertFalse(bool(E.DASHBOARD_STATE.get("live_trade_mode")),
                         "live_trade_mode must be off after paper full close")
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.0, places=6,
                               msg="finalize must zero the remaining qty after full close")

    def test_paper_never_over_closes_nor_reverses(self):
        # no over-close: repeated partials never exceed the initial size and
        # remaining never drops below 0; no reverse: side is never flipped.
        self._seed("BUY")
        E.close_partial(0.5)
        E.close_partial(0.5)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.25, places=6)
        self.assertGreaterEqual(float(E.STATE["remaining_qty"]), 0.0)
        self.assertEqual(E.STATE["side"], "BUY")
        E.finalize_trade_with_reality = lambda *a, **k: E.STATE.__setitem__("remaining_qty", 0.0)
        ok = E.close_position_full()
        self.assertTrue(ok)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.0, places=6)
        self.assertAlmostEqual(float(E.STATE["qty_initial"]), 1.0, places=6,
                               msg="over-close forbidden: closed more than initial")

    def test_paper_native_sl_is_idempotent_and_books_active_state(self):
        self._seed("BUY")
        order_1 = E.place_native_sl("BTC-USDT")
        state_1 = (E.STATE["native_sl_state"], E.STATE["native_sl_order_id"])
        order_2 = E.place_native_sl("BTC-USDT")
        state_2 = (E.STATE["native_sl_state"], E.STATE["native_sl_order_id"])
        self.assertEqual(state_1, ("ACTIVE", order_1))
        self.assertEqual(order_2, order_1, "idempotent native SL must not re-place")
        self.assertEqual(state_2, state_1)

    def test_paper_native_sl_short_books_short_sl_price(self):
        self._seed("SELL", entry=3000.0)
        E.STATE["entry"] = 3000.0
        E.paper["position"]["entry"] = 3000.0
        E.place_native_sl("BTC-USDT")
        self.assertEqual(E.STATE["native_sl_state"], "ACTIVE")
        self.assertAlmostEqual(float(E.STATE["native_sl_price"]), 3060.0, places=3)

    def test_scenario_labels_share_the_same_close_builders(self):
        # TP1 -> close_partial(0.5); TP2 / trailing / strict / SL -> full close;
        # both funnel into the single payload shapes audited in tests above.
        self._seed("BUY")
        E.close_partial(0.5)          # TP1 scenario
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.5, places=6)
        E.finalize_trade_with_reality = lambda *a, **k: E.STATE.__setitem__("remaining_qty", 0.0)
        E.close_reason = "TAKE_PROFIT_TP2"
        E.close_position_full()       # TP2 / strict close scenario
        self.assertIsNone(E.paper["position"])
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.0, places=6)


# ---------------------------------------------------------------------------
# 6) Contract-gate regression guard (models the official docs).
# ---------------------------------------------------------------------------
class ContractGateTest(unittest.TestCase):
    __test__ = True

    def test_hedge_rejects_reduceOnly_with_positionSide(self):
        for side in ("LONG", "SHORT"):
            accepted, reason = GATE.check({"positionSide": side, "reduceOnly": True})
            self.assertFalse(accepted, f"must reject: {reason}")
            self.assertIn(OFFICIAL_DO_NOT_SEND_HEDGE.strip(), reason)

    def test_hedge_accepts_close_without_reduceOnly(self):
        for side in ("LONG", "SHORT"):
            accepted, reason = GATE.check({"positionSide": side, "type": "MARKET"})
            self.assertTrue(accepted, reason)

    def test_hedge_stop_market_accepts_close_position_without_reduceOnly(self):
        accepted, reason = GATE.check({"positionSide": "LONG", "type": "STOP_MARKET",
                                       "stopPrice": 50000, "closePosition": True})
        self.assertTrue(accepted, reason)

    def test_one_way_requires_BOTH_positionSide(self):
        accepted, reason = GATE.check({"positionSide": "LONG", "reduceOnly": True})
        self.assertFalse(accepted, "one-way + LONG already rejected by hedge rule")
        accepted, reason = GATE.check({"positionSide": "BOTH", "reduceOnly": True})
        self.assertTrue(accepted, "one-way BOTH + reduceOnly must be allowed")


if __name__ == "__main__":
    unittest.main()