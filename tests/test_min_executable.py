"""最小可成交金额与实际量化器的一致性测试。

2026-11-05 连续踩了两次同一个坑：
  min_executable_notional() 自己按"步长×价"算，与 plan_buy_quantity 的
  回退路径算法不同，算出的预算比实际需要少 0.0001，于是抬底之后仍被
  quantized_above_budget 拒掉。

本测试用多个真实风格的 filters 交叉验证：只要 min_executable_notional 给出
预算，plan_buy_quantity 用同一份预算必须能成功量化。
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
from src.risk import plan_buy_quantity  # noqa: E402


class F:
    def __init__(self, step, min_qty, min_notional):
        self.step_size = Decimal(step)
        self.min_qty = Decimal(min_qty)
        self.min_notional = Decimal(min_notional)


CASES = [
    # (价格, 步长, 最小量, 最小名义)  —— 覆盖 BTC/BNB/CAKE 与若干边界
    (Decimal("2.0100"), "0.01", "0.01", "5.00"),        # CAKE
    (Decimal("719.85"), "0.001", "0.001", "5.00"),      # BNB
    (Decimal("80808.73"), "0.00001", "0.00001", "5.00"),  # BTC
    (Decimal("0.0234"), "1", "1", "5.00"),              # 低价币
    (Decimal("1234.56"), "0.0001", "0.0001", "5.00"),
    (Decimal("100.00"), "0.001", "0.001", "10.00"),     # minNotional 非 5
]


class TestConsistency(unittest.TestCase):
    def test_floor_always_quantizable(self) -> None:
        for price, step, min_qty, min_notional in CASES:
            f = F(step, min_qty, min_notional)
            me = at.min_executable_notional(price, f)
            qty, err = plan_buy_quantity(price, me, f.min_notional, f.step_size, f.min_qty)
            with self.subTest(price=str(price)):
                self.assertIsNotNone(qty, f"floor={me} 无法量化: {err}")

    def test_floor_not_below_min_notional(self) -> None:
        for price, step, min_qty, min_notional in CASES:
            f = F(step, min_qty, min_notional)
            me = at.min_executable_notional(price, f)
            with self.subTest(price=str(price)):
                self.assertGreaterEqual(me, f.min_notional)

    def test_floor_is_minimal_step_count(self) -> None:
        # 抬底应该刚好够，不能离谱地大（否则等于取消 Kronos 缩仓）
        price = Decimal("2.0100")
        f = F("0.01", "0.01", "5.00")
        me = at.min_executable_notional(price, f)
        self.assertLess(me, Decimal("5.10"))


if __name__ == "__main__":
    unittest.main()
