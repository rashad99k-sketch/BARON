import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / 'dashboard' / 'app.py').read_text(encoding='utf-8')
NEWS = (ROOT / 'news' / 'service.py').read_text(encoding='utf-8')


class DashboardContractTest(unittest.TestCase):
    def test_required_api_routes_exist(self):
        for route in ('/', '/data', '/health', '/status', '/scanner', '/watchlist',
                      '/execution', '/positions', '/portfolio', '/news', '/radar', '/metrics'):
            self.assertIn(f'@app.route("{route}")', DASH, route)

    def test_news_panel_has_positive_and_negative_colors(self):
        self.assertIn('sentiment_color === "GREEN"', DASH)
        self.assertIn('sentiment_color === "RED"', DASH)
        self.assertIn('#00ffa6', DASH)
        self.assertIn('#ff4d4d', DASH)

    def test_dashboard_exposes_queue_and_watchlist(self):
        self.assertIn('d.queue', DASH)
        self.assertIn('d.watchlist', DASH)
        self.assertIn('watchlistCount', DASH)
        self.assertIn('q-ready', DASH)

    def test_news_contract_contains_sentiment_metadata(self):
        for field in ('sentiment', 'sentiment_color', 'impact_strength', 'sentiment_confidence'):
            self.assertIn(f'"{field}"', NEWS)

    def test_no_bare_except_pass_in_dashboard(self):
        self.assertNotRegex(DASH, r'except\s*:\s*\n\s*pass')


if __name__ == '__main__':
    unittest.main()
