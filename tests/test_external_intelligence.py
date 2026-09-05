import unittest
from unittest.mock import patch

from external_intelligence.fusion import IntelligenceFusion
from external_intelligence.service import ExternalIntelligenceService

class ExternalIntelligenceTest(unittest.TestCase):
    def test_ticker_normalization(self):
        self.assertEqual(ExternalIntelligenceService.ticker_from_symbol('NVDA/USDT:USDT'), 'NVDA')
        self.assertEqual(ExternalIntelligenceService.ticker_from_symbol('BRK.B/USDT:USDT'), 'BRK.B')

    def test_fusion_requires_multiple_evidence_sources(self):
        f = IntelligenceFusion(70)
        weak = f.fuse('NVDA', {'relative_volume': 4.0, 'change': 8.0}, {'buy_count': 0}, {'filings': []})
        self.assertEqual(weak.direction, 'NEUTRAL')
        strong = f.fuse('NVDA', {'relative_volume': 4.0, 'change': 8.0}, {'buy_count': 2, 'sell_count': 0}, {'filings': [{'form':'8-K'}]})
        self.assertEqual(strong.direction, 'BUY')
        self.assertGreaterEqual(strong.score, 70)

    def test_fusion_does_not_treat_sec_filing_as_bullish_by_itself(self):
        f = IntelligenceFusion(70)
        snap = f.fuse('ABC', {}, {}, {'filings': [{'form':'8-K'}, {'form':'10-Q'}]})
        self.assertNotEqual(snap.direction, 'BUY')

    def test_service_never_returns_trade_action(self):
        service = ExternalIntelligenceService()
        with patch.object(service.finviz, 'quote', return_value={'relative_volume': 3.0, 'change': 8.0, 'available': True}), \
             patch.object(service.insider, 'latest', return_value={'buy_count': 2, 'sell_count': 0, 'available': True}), \
             patch.object(service.sec, 'recent_filings', return_value={'filings': [{'form':'8-K'}], 'available': True}):
            result = service.scan(['NVDA/USDT:USDT'], force=True)
        item = result['NVDA']
        self.assertEqual(item['direction'], 'BUY')
        self.assertNotIn('execute', str(item).lower())

if __name__ == '__main__':
    unittest.main()
