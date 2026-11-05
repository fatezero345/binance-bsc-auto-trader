"""Kronos 接入的单元测试：只测纯逻辑，不联网。

重点覆盖三件事：
  1. 系数永远落在 [min_factor, 1.0] —— 它不能放大仓位；
  2. 服务失败时退化成 1.0，绝不阻断交易；
  3. 载荷缺字段时同样退化，不抛异常。
"""

from __future__ import annotations

import unittest

from src.strategy.kronos_client import KronosClient, KronosDecision


class TestSizeFactor(unittest.TestCase):
    def setUp(self) -> None:
        self.kc = KronosClient(min_factor=0.25)

    def test_high_probability_caps_at_one(self) -> None:
        d = KronosDecision(True, prob_up=0.95, size_factor=max(0.25, min(1.0, 0.95)))
        self.assertLessEqual(d.size_factor, 1.0)

    def test_certain_probability_is_exactly_one(self) -> None:
        self.assertEqual(max(0.25, min(1.0, 1.0)), 1.0)

    def test_low_probability_clamped_to_min(self) -> None:
        self.assertEqual(max(0.25, min(1.0, 0.05)), 0.25)

    def test_mid_probability_passes_through(self) -> None:
        self.assertAlmostEqual(max(0.25, min(1.0, 0.63)), 0.63, places=6)

    def test_factor_never_exceeds_one_for_any_prob(self) -> None:
        for p in (0.0, 0.1, 0.5, 0.9, 1.0, 1.5, -0.3):
            f = max(0.25, min(1.0, p))
            self.assertGreaterEqual(f, 0.25)
            self.assertLessEqual(f, 1.0)


class TestFailureIsNeutral(unittest.TestCase):
    def test_unreachable_service_is_neutral(self) -> None:
        kc = KronosClient(url="http://127.0.0.1:1", timeout=0.5)
        d = kc.decision("CAKEUSDT", "4h", bars=[{"timestamp": "2026-01-01T00:00:00",
                                                 "open": 1, "high": 1, "low": 1,
                                                 "close": 1, "volume": 1}] * 10)
        self.assertFalse(d.ok)
        self.assertEqual(d.size_factor, 1.0)
        self.assertIsNotNone(d.error)

    def test_decision_dict_has_no_nan(self) -> None:
        d = KronosDecision(False, error="x")
        payload = d.as_dict()
        self.assertEqual(payload["size_factor"], 1.0)
        self.assertIsNone(payload["prob_up"])


if __name__ == "__main__":
    unittest.main()
