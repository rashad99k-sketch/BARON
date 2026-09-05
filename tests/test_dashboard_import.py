import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec("flask"), "Flask dependency is installed by run_windows.bat")
class DashboardImportTest(unittest.TestCase):
    def test_dashboard_exposes_flask_app(self):
        import importlib
        module = importlib.import_module("dashboard.app")
        self.assertTrue(hasattr(module, "app"))
        self.assertEqual(module.app.__class__.__name__, "Flask")


if __name__ == "__main__":
    unittest.main()
