"""灰尘回收的安全性测试。

2026-11-04 实盘事故的回归用例：
BNB 同时有 0.007 灰尘和 0.009 持仓时，灰尘回收按全部余额下单，
把持仓一起卖掉，而账本里只留了一条 dust_recovered（没有 pnl），
那笔仓位的盈亏永久丢失。12 分钟后 reconcile 报"账户已空"。

修复要点：
  1. 卖出量不得超过记录的灰尘数量；
  2. 有持仓时整个跳过灰尘回收。
"""

from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("at", ROOT / "tools" / "auto_trader.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)

STEP = Decimal("0.001")


class TestDustSellQty(unittest.TestCase):
    def test_capped_by_recorded_dust(self) -> None:
        # 账户 0.0169 = 0.007 灰尘 + 0.009 持仓；只能卖 0.007
        self.assertEqual(at.dust_sell_qty(Decimal("0.0169"), "0.007", STEP), Decimal("0.007"))

    def test_capped_even_when_balance_much_larger(self) -> None:
        self.assertEqual(at.dust_sell_qty(Decimal("1.5"), "0.007", STEP), Decimal("0.007"))

    def test_uses_balance_when_no_record(self) -> None:
        self.assertEqual(at.dust_sell_qty(Decimal("0.0169"), None, STEP), Decimal("0.016"))

    def test_never_exceeds_balance(self) -> None:
        # 记录的灰尘比账户还多（数据过时）时，以账户为准
        self.assertEqual(at.dust_sell_qty(Decimal("0.003"), "0.007", STEP), Decimal("0.003"))

    def test_bad_record_falls_back_to_balance(self) -> None:
        self.assertEqual(at.dust_sell_qty(Decimal("0.0169"), "abc", STEP), Decimal("0.016"))

    def test_quantized_down(self) -> None:
        # 0.0079 向下量化到步长 0.001 -> 0.007
        self.assertEqual(at.dust_sell_qty(Decimal("0.0079"), None, STEP), Decimal("0.007"))


class TestDustSkippedWhenPositionOpen(unittest.TestCase):
    def test_helper_exists(self) -> None:
        self.assertTrue(callable(at.dust_sell_qty))

    def test_source_guards_position(self) -> None:
        """源码必须含有"有持仓就跳过"的守卫，防止被后续改动删掉。"""
        src = (ROOT / "tools" / "auto_trader.py").read_text(encoding="utf-8")
        self.assertIn("dust_skip_position", src)
        self.assertIn("if sym in st.get(\"positions\", {}):", src)


if __name__ == "__main__":
    unittest.main()
