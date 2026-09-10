import importlib
import importlib.util
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
        orig_engine = sys.modules.get("core.engine")
        try:
            if orig_engine is not None:
                # Re-execute the engine under a private name so the shared
                # core.engine identity (bound by portfolio/manager and the
                # live-brain harnesses) is never evicted mid-suite.
                spec = importlib.util.spec_from_file_location(
                    "_import_smoke_fresh", orig_engine.__file__)
                module = importlib.util.module_from_spec(spec)
                sys.modules["_import_smoke_fresh"] = module
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop("_import_smoke_fresh", None)
            else:
                module = importlib.import_module("core.engine")
            self.assertTrue(hasattr(module, "resolve_exchange_symbol"))
            self.assertTrue(hasattr(module, "execute_entry"))
            self.assertEqual(module.resolve_exchange_symbol("AAPL/USDT"), "AAPL/USDT:USDT")
            self.assertEqual(module.resolve_exchange_symbol("GOLD(XAU)/USDT"), "GOLD(XAU)/USDT:USDT")
        finally:
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
