import unittest
from pathlib import Path


class SecurityControlRegressionTest(unittest.TestCase):
    def test_manual_controls_are_authenticated(self):
        source = Path("dashboard/app.py").read_text(encoding="utf-8")
        trade = source[source.index("def manual_trade"):source.index("def manual_close")]
        close = source[source.index("def manual_close"):source.index("def health")]
        self.assertIn("_control_authorized()", trade)
        self.assertIn("_control_authorized()", close)
        self.assertIn("X-Dashboard-Token", source)

    def test_portfolio_risk_is_published(self):
        source = Path("core/runtime.py").read_text(encoding="utf-8")
        self.assertIn('"risk": PORTFOLIO.risk_snapshot()', source)


if __name__ == "__main__":
    unittest.main()
