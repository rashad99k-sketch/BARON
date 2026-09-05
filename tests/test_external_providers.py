import unittest
from unittest.mock import Mock

from external_intelligence.providers import FinvizProvider, OpenInsiderProvider

class ExternalProviderParsingTest(unittest.TestCase):
    def test_finviz_quote_parses_common_snapshot_fields(self):
        html = '<td>Relative Volume</td><td>3.45</td><td>Change</td><td>8.2%</td><td>Price</td><td>12.30</td>'
        r = Mock(text=html)
        c = Mock(); c.get.return_value = r
        data = FinvizProvider(c).quote('NVDA')
        self.assertTrue(data['available'])
        self.assertAlmostEqual(data.get('relative_volume'), 3.45)
        self.assertAlmostEqual(data.get('change'), 8.2)
        self.assertAlmostEqual(data.get('price'), 12.30)

    def test_openinsider_filters_rows_for_requested_ticker(self):
        html = '''<table><tr><th>Ticker</th><th>Trade Type</th></tr>
        <tr><td>NVDA</td><td>Purchase</td></tr>
        <tr><td>AAPL</td><td>Purchase</td></tr>
        <tr><td>NVDA</td><td>Sale</td></tr></table>'''
        r = Mock(text=html)
        c = Mock(); c.get.return_value = r
        data = OpenInsiderProvider(c).latest('NVDA')
        self.assertTrue(data['available'])
        self.assertEqual(data['buy_count'], 1)
        self.assertEqual(data['sell_count'], 1)

if __name__ == '__main__':
    unittest.main()
