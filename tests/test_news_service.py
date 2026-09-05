import unittest
from unittest.mock import patch

from news.service import NewsService


class FakeResponse:
    content = b"""<?xml version='1.0'?><rss><channel>
    <item><title>Gold rallies as Fed rate decision approaches</title><link>x</link></item>
    <item><title>Company beats estimates and raises guidance</title><link>y</link></item>
    </channel></rss>"""

    def raise_for_status(self):
        return None


class NewsServiceTest(unittest.TestCase):
    def test_rss_scoring(self):
        service = NewsService()
        service.feeds = ["https://example.test/rss?q={query}"]

        with patch("news.service.requests.get", return_value=FakeResponse()):
            result = service.assess("XAU/USDT", "GOLD")

        self.assertTrue(result.available)
        self.assertGreater(result.risk, 0)
        self.assertEqual(result.bias, "BULLISH")


if __name__ == "__main__":
    unittest.main()

class FakeYahooResponse:
    def raise_for_status(self):
        return None
    def json(self):
        return {"news": [
            {"title": "BTC rallies on strong inflows", "link": "https://example.test/btc", "publisher": "Yahoo", "providerPublishTime": 1890000000, "summary": "Institutional inflows increased."}
        ]}


class NewsProviderRegressionTest(unittest.TestCase):
    def test_yahoo_provider_is_preferred(self):
        service = NewsService()
        service.feeds = []
        with patch("news.service.requests.get", return_value=FakeYahooResponse()):
            result = service.assess("BTC/USDT:USDT", "CRYPTO")
        self.assertTrue(result.available)
        self.assertEqual(result.provider, "YAHOO")
        self.assertGreaterEqual(len(result.headlines), 1)

    def test_rss_is_used_when_yahoo_fails(self):
        service = NewsService()
        service.feeds = ["https://example.test/rss?q={query}"]
        with patch("news.service.requests.get", side_effect=[RuntimeError("yahoo unavailable"), FakeResponse(), FakeResponse()]):
            result = service.assess("XAU/USDT", "GOLD")
        self.assertTrue(result.available)
        self.assertEqual(result.provider, "RSS")
