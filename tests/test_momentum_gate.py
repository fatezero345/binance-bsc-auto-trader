"""入场闸的验收测试。用构造序列，不打网络。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategy.momentum_gate import closes_from_klines, entry_gate  # noqa: E402


def flat(n: int = 96, price: float = 1.84, noise: float = 0.001) -> list[float]:
    """轻微上下抖动的横盘序列（σ 很小但不为零）。"""
    out = []
    for i in range(n):
        out.append(price * (1 + (noise if i % 2 == 0 else -noise)))
    return out


class TestEntryGate(unittest.TestCase):
    def test_flat_market_allows(self):
        v = entry_gate(flat())
        self.assertTrue(v.allow, v.summary())

    def test_soft_drift_below_ma_within_one_sigma_allows(self):
        """2026-11-03 的真实情形：比 MA20 低 0.30%，而 σ=0.258% —— 约 1.18σ。

        这里构造一个略小于 1σ 的偏离，应当放行（说明规则不是「只要低于均线就拒」）。
        """
        series = flat()
        series[-1] = series[-1] * 0.9995
        v = entry_gate(series)
        self.assertTrue(v.allow, v.summary())

    def test_sharp_drop_below_one_sigma_skips(self):
        series = flat()
        series[-1] = series[-1] * 0.99          # 单根 -1%
        v = entry_gate(series)
        self.assertFalse(v.allow)
        self.assertIn("deviation", v.reason)

    def test_fast_collapse_skips_even_if_ma_is_close(self):
        """近 5 根连续下跌，跌幅超过 2σ —— 即使离 MA 还不远也要拒。"""
        series = flat(price=1.84, noise=0.0002)
        for i in range(5):
            series[-1 - i] = series[-1 - i] * (1 - 0.004)
        v = entry_gate(series)
        self.assertFalse(v.allow)

    def test_zero_volatility_fails_closed(self):
        v = entry_gate([1.84] * 96)
        self.assertFalse(v.allow)
        self.assertIn("zero volatility", v.reason)

    def test_too_few_points_raises(self):
        with self.assertRaises(ValueError):
            entry_gate([1.0] * 10)

    def test_threshold_is_derived_from_sigma_not_constant(self):
        """同一偏离幅度，在低波动标的上应被拒、在高波动标的上应放行。"""
        low_vol = flat(noise=0.0001)
        low_vol[-1] *= 0.997
        high_vol = flat(noise=0.02)
        high_vol[-1] *= 0.997
        self.assertFalse(entry_gate(low_vol).allow)
        self.assertTrue(entry_gate(high_vol).allow)

    def test_closes_from_klines(self):
        raw = [[0, "0", "0", "0", "1.5", "0"], [0, "0", "0", "0", "1.6", "0"]]
        self.assertEqual(closes_from_klines(raw), [1.5, 1.6])


if __name__ == "__main__":
    unittest.main(verbosity=2)
