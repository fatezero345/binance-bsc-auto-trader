"""平仓账目正确性测试。

2026-11-05 两个实盘问题：

1. 一笔 2.48 CAKE 的持仓超时平仓时名义只有 4.94 < 5.00（交易所下限），
   被当成"卖不掉的灰尘"写掉并移出持仓表 —— 一笔约 5 USDT 的真实仓位
   从账上消失。修复：保留仓位 + 30 分钟冷却，不提交订单也就没有 -1013。

2. pnl 用**全部余额**算：CAKE 持仓 2.49、灰尘 2.48 时卖 4.97，
   盈亏会按 4.97 计，虚增近一倍。修复：只按持仓量算，超出部分记 dust_cleared。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("at", ROOT / "tools" / "auto_trader.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)


class TestExitAccountingSource(unittest.TestCase):
    def setUp(self) -> None:
        self.src = (ROOT / "tools" / "auto_trader.py").read_text(encoding="utf-8")

    def test_position_below_min_is_not_written_off(self) -> None:
        """持仓低于下限时必须走 position_below_min_notional，而不是 dust_unsellable。"""
        self.assertIn("position_below_min_notional", self.src)
        # 分支里必须先判断 pos is not None
        i = self.src.index("if sellable < snap.filters.min_notional:")
        seg = self.src[i:i + 900]
        self.assertIn("if pos is not None:", seg)

    def test_exit_cooldown_used_in_tick_loop(self) -> None:
        i = self.src.index("def _tick_locked")
        seg = self.src[i:i + 1200]
        self.assertIn("exit_cooldown", seg)

    def test_pnl_uses_position_qty_only(self) -> None:
        self.assertIn("pnl_qty = min(", self.src)
        self.assertIn("dust_cleared", self.src)
        # 不能再出现直接用 qty 算 pnl 的老写法
        self.assertNotIn("(float(snap.price) - pos.entry_price) * float(qty)", self.src)

    def test_retry_constant_sane(self) -> None:
        self.assertGreaterEqual(at.EXIT_RETRY_S, 300)
        self.assertLessEqual(at.EXIT_RETRY_S, 7200)


class TestPnlArithmetic(unittest.TestCase):
    def test_pnl_excludes_dust(self) -> None:
        # 模拟：entry=2.012, exit=2.10, 持仓 2.49，但余额 4.97
        entry, exit_px = 2.012, 2.10
        pos_qty = 2.49
        sold = 4.97
        pnl_qty = min(sold, pos_qty)
        self.assertEqual(pnl_qty, 2.49)
        pnl = (exit_px - entry) * pnl_qty
        self.assertLess(pnl, (exit_px - entry) * sold)
        self.assertAlmostEqual(pnl, 0.088 * 2.49, places=6)


if __name__ == "__main__":
    unittest.main()
