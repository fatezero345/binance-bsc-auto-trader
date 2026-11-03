"""离场规则的验收测试。纯函数，不联网。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategy.exit_rules import Position, exit_decision  # noqa: E402

T0 = 1_800_000_000


def pos() -> Position:
    return Position(symbol="CAKEUSDT", qty="2.72", entry_price=1.84,
                    stop_price=1.748, tp_price=1.9504, opened_ts=T0)


class TestExitDecision(unittest.TestCase):
    def test_hold_when_between_bounds(self):
        self.assertIsNone(exit_decision(pos(), 1.85, T0 + 60, 4 * 3600))

    def test_stop_at_or_below(self):
        self.assertEqual(exit_decision(pos(), 1.748, T0 + 60, 4 * 3600), "stop")
        self.assertEqual(exit_decision(pos(), 1.70, T0 + 60, 4 * 3600), "stop")

    def test_take_profit_at_or_above(self):
        self.assertEqual(exit_decision(pos(), 1.9504, T0 + 60, 4 * 3600), "take_profit")
        self.assertEqual(exit_decision(pos(), 2.10, T0 + 60, 4 * 3600), "take_profit")

    def test_max_hold(self):
        self.assertEqual(exit_decision(pos(), 1.85, T0 + 4 * 3600, 4 * 3600), "max_hold")

    def test_stop_wins_when_both_bounds_breached_in_one_tick(self):
        """同一 tick 既破止损又破止盈（跳空/脏数据）：按最坏情况处理。"""
        weird = Position(symbol="X", qty="1", entry_price=1.0,
                         stop_price=2.0, tp_price=0.5, opened_ts=T0)
        self.assertEqual(exit_decision(weird, 1.0, T0, 3600), "stop")

    def test_roundtrip_dict(self):
        p = pos()
        self.assertEqual(Position.from_dict(p.as_dict()), p)

    def test_zero_max_hold_disables_time_exit(self):
        self.assertIsNone(exit_decision(pos(), 1.85, T0 + 10**7, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
