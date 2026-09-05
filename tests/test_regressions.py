import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class RegressionTest(unittest.TestCase):
    def test_dashboard_portfolio_object_is_escaped_inside_fstring(self):
        text = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertIn('const portfolio = d.portfolio || {{open_positions: 0, max_positions: 0, capacity: 0}};', text)
        self.assertNotIn('const portfolio = d.portfolio || {open_positions:', text)

    def test_core_has_canonical_smart_zone_provider(self):
        text = (ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('def get_smart_zones(symbol, df, ob=None):', text)

    def test_six_position_margin_policy(self):
        text = (ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('PORTFOLIO_MARGIN_CAP_PCT', text)
        self.assertIn('POSITION_MARGIN_PCT', text)

    def test_all_python_files_parse(self):
        for path in ROOT.rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

if __name__ == "__main__":
    unittest.main()
