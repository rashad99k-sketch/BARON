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
    def run(self, *args, **kwargs):
        return None


class DashboardImportSmokeTest(unittest.TestCase):
    def test_dashboard_imports_with_dependency_boundary_stubs(self):
        saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine", "scanner.scanner", "dashboard.app")}
        fake_ccxt = types.ModuleType("ccxt")
        fake_ccxt.bingx = lambda *a, **k: types.SimpleNamespace(markets={})
        fake_flask = types.ModuleType("flask")
        fake_flask.Flask = _FakeFlask
        fake_flask.jsonify = lambda payload=None, *a, **k: payload
        fake_flask.request = types.SimpleNamespace(args={}, json={})
        sys.modules["ccxt"] = fake_ccxt
        sys.modules["flask"] = fake_flask
        try:
            # MUST NOT evict core.engine: re-importing it under a fresh
            # identity mid-suite permanently orphans every module that already
            # holds `import core.engine` (portfolio.manager and the live-brain
            # harnesses), which then manages a STATE nobody mirrors. Instead we
            # fresh-execute dashboard.app under a PRIVATE module name and assert
            # the structural contract it must satisfy to be loadable at all.
            module_path = importlib.util.find_spec("dashboard.app").origin
            aspec = importlib.util.spec_from_file_location("_dash_smoke_app", module_path)
            module = importlib.util.module_from_spec(aspec)
            sys.modules["_dash_smoke_app"] = module
            try:
                aspec.loader.exec_module(module)
            finally:
                sys.modules.pop("_dash_smoke_app", None)
            self.assertTrue(hasattr(module, "app"))
            self.assertTrue(callable(module.app.route))
            self.assertTrue(callable(module.app.add_url_rule))
            # The engine-bound copies prove the core.engine wiring survived the
            # dependency stubs (this is what the smoke test actually guards).
            self.assertTrue(hasattr(module, "resolve_exchange_symbol"))
            self.assertEqual(
                module.resolve_exchange_symbol("AAPL/USDT"), "AAPL/USDT:USDT")
        finally:
            for name, mod in saved.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod


if __name__ == "__main__":
    unittest.main()