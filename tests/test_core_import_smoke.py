import importlib
import sys
import types
import unittest


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        pass
    def route(self, *args, **kwargs):
        return lambda fn: fn
    def add_url_rule(self, *args, **kwargs):
        return None


class CoreImportSmokeTest(unittest.TestCase):
    def test_core_imports_without_exchange_network(self):
        saved_ccxt = sys.modules.get("ccxt")
        saved_flask = sys.modules.get("flask")
        fake_ccxt = types.ModuleType("ccxt")

        class FakeBingX:
            def __init__(self, *args, **kwargs):
                self.markets = {
                    "AAPL/USDT:USDT": {},
                    "GOLD(XAU)/USDT:USDT": {},
                }

        fake_ccxt.bingx = FakeBingX
        fake_flask = types.ModuleType("flask")
        fake_flask.Flask = _FakeFlask
        fake_flask.jsonify = lambda *a, **k: None
        fake_flask.request = types.SimpleNamespace()
        sys.modules["ccxt"] = fake_ccxt
        sys.modules["flask"] = fake_flask
        try:
            sys.modules.pop("core.engine", None)
            module = importlib.import_module("core.engine")
            self.assertTrue(hasattr(module, "resolve_exchange_symbol"))
            self.assertTrue(hasattr(module, "execute_entry"))
            self.assertEqual(module.resolve_exchange_symbol("AAPL/USDT"), "AAPL/USDT:USDT")
            self.assertEqual(module.resolve_exchange_symbol("GOLD(XAU)/USDT"), "GOLD(XAU)/USDT:USDT")
        finally:
            sys.modules.pop("core.engine", None)
            if saved_ccxt is not None:
                sys.modules["ccxt"] = saved_ccxt
            else:
                sys.modules.pop("ccxt", None)
            if saved_flask is not None:
                sys.modules["flask"] = saved_flask
            else:
                sys.modules.pop("flask", None)


if __name__ == "__main__":
    unittest.main()
