import re
import unittest

import core.engine as engine


class _MockExchange:
    """Records the exact clientOrderId passed to create_order."""

    def __init__(self):
        self.created = []
        self.fetch_order = lambda *a, **k: {"status": "closed", "filled": 1.0}

    def create_order(self, sym, order_type, side, amount, params=None):
        params = params or {}
        self.created.append(
            {"sym": sym, "type": order_type, "side": side, "amount": amount, "params": params}
        )
        return {"id": "mock", "status": "closed"}


class BingXClientOrderIdLengthTest(unittest.TestCase):
    BINGX_SAFE_CHARS = re.compile(r"^[A-Za-z0-9_]+$")

    def setUp(self):
        # Never hit the network; stub the module-level market registry so
        # normalize_symbol()/resolve_exchange_symbol() work deterministically.
        engine.ex.markets = {
            "BTC/USDT:USDT": {},
            "BTC/USDT": {},
            "BTC:-USDT": {},
            "1000000BABYDOGE/USDT:USDT": {},
            "1000000BABYDOGE/USDT": {},
            "1000000BABYDOGE:-USDT": {},
        }

    def _assert_safe(self, cid):
        length = len(cid)
        self.assertLessEqual(length, 40, f"clientOrderId too long ({length}): {cid}")
        self.assertRegex(cid, self.BINGX_SAFE_CHARS,
                         f"clientOrderId has non-BingX-safe chars: {cid}")
        return cid

    def test_helper_normal_and_long_symbols(self):
        om = engine.OrderManager(object())
        cases = [
            ("BTC/USDT:USDT", "BUY"),
            ("BTC/USDT:USDT", "SELL"),
            ("1000000BABYDOGE/USDT:USDT", "BUY"),
            ("1000000BABYDOGE/USDT:USDT", "SELL"),
            ("1000000BABYDOGE/USDT", "BUY"),
        ]
        for symbol, side in cases:
            cid = om._generate_client_id(symbol, side)
            self._assert_safe(cid)

    def test_consecutive_generated_ids_are_unique(self):
        om = engine.OrderManager(object())
        ids = {om._generate_client_id("BTC/USDT:USDT", "BUY") for _ in range(50)}
        self.assertEqual(len(ids), 50, "generated IDs collided")

    def test_submit_order_passes_short_client_order_id_to_create_order(self):
        om = engine.OrderManager(_MockExchange())
        cases = [
            ("BTC/USDT:USDT", "buy", 0.01),
            ("BTC/USDT:USDT", "sell", 0.01),
            ("1000000BABYDOGE/USDT:USDT", "buy", 0.01),
            ("1000000BABYDOGE/USDT:USDT", "sell", 0.01),
        ]
        for symbol, side, amount in cases:
            om._seen_ids.clear()
            order, cid = om.submit_order(symbol, side, amount, leverage=10)
            self._assert_safe(cid)
            self.assertGreaterEqual(len(om._pending_orders), 1)

    def test_exact_client_order_id_passed_to_create_order_is_under_limit(self):
        mock = _MockExchange()
        om = engine.OrderManager(mock)
        for symbol, side, amount in [
            ("BTC/USDT:USDT", "buy", 0.01),
            ("1000000BABYDOGE/USDT:USDT", "sell", 0.01),
        ]:
            om._seen_ids.clear()
            om.submit_order(symbol, side, amount, leverage=10)
        self.assertEqual(len(mock.created), 2)
        for call in mock.created:
            cid = call["params"]["clientOrderId"]
            self._assert_safe(cid)
            self.assertEqual(call["type"], "market")
            self.assertIn("leverage", call["params"])


if __name__ == "__main__":
    unittest.main()
