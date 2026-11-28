"""手动下注金额与限额的纯逻辑检查；不连接数据库、不调用资金接口。"""

import unittest
from decimal import Decimal

from services.api.app.orders import manual_limits_valid, order_is_manual


class ManualLimitTests(unittest.TestCase):
    def test_stake_within_daily_and_exposure_is_valid(self):
        self.assertTrue(manual_limits_valid(Decimal('10'), Decimal('100'), Decimal('50')))

    def test_stake_equal_to_both_caps_is_valid(self):
        self.assertTrue(manual_limits_valid('10', '10', '10'))

    def test_rejects_zero_or_negative_stake(self):
        for stake in ('0', '-1'):
            with self.subTest(stake=stake):
                self.assertFalse(manual_limits_valid(stake, '100', '100'))

    def test_rejects_stake_above_either_limit(self):
        self.assertFalse(manual_limits_valid('11', '10', '100'))
        self.assertFalse(manual_limits_valid('11', '100', '10'))

    def test_rejects_non_numeric_or_non_finite(self):
        for stake in ('abc', 'NaN', 'Infinity', None):
            with self.subTest(stake=stake):
                self.assertFalse(manual_limits_valid(stake, '100', '100'))


class ManualFlagTests(unittest.TestCase):
    """复现 asyncpg Record 语义：`x in record` 遍历的是值，不能用来判断列是否存在。"""

    class RecordLike:
        def __init__(self, row):
            self._row = row

        def __getitem__(self, key):
            if key not in self._row:
                raise KeyError(key)
            return self._row[key]

        def __iter__(self):
            return iter(self._row.values())

    def test_manual_flag_read_from_column(self):
        self.assertTrue(order_is_manual(self.RecordLike({'manual': True})))
        self.assertFalse(order_is_manual(self.RecordLike({'manual': False})))

    def test_missing_column_is_not_manual(self):
        self.assertFalse(order_is_manual(self.RecordLike({'stake': 5})))
        self.assertFalse(order_is_manual({'stake': 5}))


if __name__ == '__main__':
    unittest.main()
