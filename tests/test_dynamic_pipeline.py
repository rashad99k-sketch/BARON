import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DynamicPipelineStaticTest(unittest.TestCase):
    def test_runtime_uses_watchlist_queue_pipeline(self):
        runtime = (ROOT / "core" / "runtime.py").read_text(encoding="utf-8")
        self.assertIn("DEEP_SCANNER.scan(force=True)", runtime)
        self.assertIn("DEEP_SCANNER.monitor_watchlist()", runtime)
        self.assertIn("promote_to_queue()", runtime)
        self.assertIn("queue.get_best_candidate()", runtime)
        self.assertIn("PORTFOLIO.open_candidate(candidate)", runtime)
        # The old direct deep-candidate portfolio bypass must not remain.
        self.assertNotIn("candidates = DEEP_SCANNER.scan()\n    if not candidates:", runtime)

    def test_deep_scanner_has_dynamic_watchlist(self):
        source = (ROOT / "scanner" / "deep_scanner.py").read_text(encoding="utf-8")
        self.assertIn("DEEP_WATCHLIST_SIZE", source)
        self.assertIn("WATCHLIST_DEEP_BATCH_SIZE", source)
        self.assertIn("WATCHLIST_DEEP_INTERVAL_SEC", source)
        self.assertIn("monitor_watchlist", source)
        self.assertIn("for side in (\"BUY\", \"SELL\")", source)

    def test_deep_radar_requests_enough_bars_for_core_validator(self):
        source = (ROOT / "scanner" / "deep_scanner.py").read_text(encoding="utf-8")
        self.assertIn('E.get_ohlcv_safe(sym, 120)', source)
        self.assertNotIn('E.get_ohlcv_safe(sym, 80)', source)

    def test_cleanup_is_defensive(self):
        source = (ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('v.get("last_update", v.get("updated_at", 0))', source)


if __name__ == "__main__":
    unittest.main()
