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
            for name in ("dashboard.app", "scanner.scanner", "core.engine"):
                sys.modules.pop(name, None)
            module = importlib.import_module("dashboard.app")
            self.assertTrue(hasattr(module, "app"))
            self.assertIsInstance(module.app, _FakeFlask)
        finally:
            for name, mod in saved.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod


if __name__ == "__main__":
    unittest.main()
