"""Price refresh, dated estimates, and repricing without rescanning session logs."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dsh_control_app.pricing import BUNDLED_TERMS, TZ, load_prices, parse_pricing, refresh_prices
from dsh_control_app.usage import Ledger, fee, period


PRICE_PAGE = """
<table>
<tr><td colspan="3">模型</td><td>deepseek-flash<sup>(1)</sup></td><td>deepseek-v4-pro</td></tr>
<tr><td rowspan="6">价格<sup>(2)</sup></td><td rowspan="2">百万 tokens 输入<br>（缓存命中）</td>
<td>空闲时段</td><td>0.02元</td><td>0.15元</td></tr>
<tr><td>高峰时段</td><td>0.04元</td><td>0.30元</td></tr>
<tr><td rowspan="2">百万tokens输入<br>（缓存未命中）</td><td>空闲时段</td><td>1元</td><td>4.5元</td></tr>
<tr><td>高峰时段</td><td>2元</td><td>9.0元</td></tr>
<tr><td rowspan="2">百万tokens输出</td><td>空闲时段</td><td>4元</td><td>13.5元</td></tr>
<tr><td>高峰时段</td><td>8元</td><td>27.0元</td></tr>
</table>
<p>(1) 模型名请使用 <code>deepseek-flash</code>。旧模型名
<code>deepseek-v4-flash</code>、<code>deepseek-v4-flash-vision-exp</code> 仍可调用。</p>
<p>北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；
其余时段，包括周末及中国法定节假日全天均为空闲时段。</p>
"""
SAMPLE = {'inputTokens': 100, 'outputTokens': 200, 'cacheReadTokens': 300}


def ms(at):
    return int(at.timestamp() * 1000)


class PricingTests(unittest.TestCase):
    def test_price_parser_uses_both_tiers_and_rejects_unknown_units(self):
        self.assertEqual(parse_pricing(PRICE_PAGE), BUNDLED_TERMS)
        parsed = parse_pricing(PRICE_PAGE.replace('0.04元', '0.06元').replace('14:00 - 18:00', '14:30 - 18:00'))
        self.assertEqual(parsed['rates']['deepseek-flash'][1][0], .06)
        self.assertEqual(parsed['hours'][1][0], 870)
        for malformed in (PRICE_PAGE.replace('0.04元', '0.04美元'),
                          PRICE_PAGE.replace('百万tokens输出', '千tokens输出'),
                          PRICE_PAGE.replace('不含中国法定节假日', '含中国法定节假日')):
            with self.assertRaises(ValueError):
                parse_pricing(malformed)

    def test_recheck_renews_estimates_but_failed_refresh_keeps_cache(self):
        with TemporaryDirectory() as tmp:
            checked = datetime(2026, 12, 20, 12, tzinfo=TZ)
            holiday = datetime(2027, 1, 1, 10, tzinfo=TZ)
            ledger = Ledger(Path(tmp) / 'usage.sqlite3', 'fixture')
            with ledger.connect() as db:
                db.execute("""INSERT INTO usage
                    (scope, session, seq, time, tokens, model, provider, input_tokens,
                     cache_read_tokens, output_tokens, price_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ('fixture', 's', 1, ms(holiday), 600, 'deepseek-flash', 'deepseek', 100, 300, 200,
                     ledger.prices.revision))
            with (patch('dsh_control_app.pricing.datetime', wraps=datetime) as clock,
                  patch('dsh_control_app.pricing.official_terms', return_value=BUNDLED_TERMS)):
                clock.now.return_value = checked
                refresh_prices(tmp)
            ledger.prices = load_prices(tmp)
            ledger.reprice()
            row = ledger.summary(holiday)['today']
            self.assertAlmostEqual(row['cost'], .000906)
            self.assertEqual(row['tokens'], 600)
            self.assertEqual(row['unpriced'], 0)
            self.assertEqual(period(holiday, ledger.prices)[0], '空闲')
            self.assertEqual(period(datetime(2027, 1, 4, 10, tzinfo=TZ), ledger.prices)[0], '高峰')
            self.assertIn('过期', period(datetime(2027, 1, 19, tzinfo=TZ), ledger.prices)[0])
            with patch('dsh_control_app.pricing.official_terms', side_effect=ValueError('unrecognized')):
                with self.assertRaises(ValueError):
                    refresh_prices(tmp)
            self.assertEqual(load_prices(tmp).revision, ledger.prices.revision)

    def test_changed_terms_apply_from_observation_with_uncertain_gap_unpriced(self):
        checked = datetime(2026, 11, 30, 8, tzinfo=TZ)
        changed = parse_pricing(PRICE_PAGE.replace('8元', '12元'))
        with TemporaryDirectory() as tmp:
            with (patch('dsh_control_app.pricing.datetime', wraps=datetime) as clock,
                  patch('dsh_control_app.pricing.official_terms', return_value=changed)):
                clock.now.return_value = checked
                refresh_prices(tmp)
            prices = load_prices(tmp)
            self.assertAlmostEqual(fee('deepseek-flash', ms(datetime(2026, 11, 20, 10, tzinfo=TZ)),
                                       SAMPLE, prices=prices), .001812)
            self.assertIsNone(fee('deepseek-flash', ms(datetime(2026, 11, 27, 10, tzinfo=TZ)),
                                  SAMPLE, prices=prices))
            self.assertAlmostEqual(fee('deepseek-flash', ms(checked.replace(hour=10)), SAMPLE,
                                       prices=prices), .002612)
