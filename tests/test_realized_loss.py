"""风险熔断的真实性与执行性测试。

2026-11-05 发现的问题：
  policy.py 会在 realized_loss_usdt >= max_cumulative_realized_loss_usdt 时
  拒绝买入，但 auto_trader 在构造 PortfolioState 时把这个值硬编码成 0，
  所以熔断**永远不会触发** —— 配置的 1.50 USDT 上限是一纸空文。

本测试锁住三件事：
  1. 累计亏损必须从账本真实计算；
  2. 盈利要能抵扣亏损（净回撤口径）；
  3. 日内亏损单独可算。
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("at", ROOT / "tools" / "auto_trader.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)


def write_ledger(rows) -> Path:
    f = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return f


class TestRealizedPnl(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = at.LEDGER
        import time
        self.today = time.strftime("%Y-%m-%d")

    def tearDown(self) -> None:
        at.LEDGER = self._orig
        at._PNL_CACHE = {"key": None, "total": Decimal("0"), "today": Decimal("0"), "day": ""}

    def _use(self, rows):
        at.LEDGER = write_ledger(rows)
        at._PNL_CACHE = {"key": None, "total": Decimal("0"), "today": Decimal("0"), "day": ""}

    def test_loss_is_positive_after_net_loss(self) -> None:
        self._use([
            {"ts": f"{self.today}T01:00:00+0800", "kind": "sold", "pnl_usdt": -0.30},
            {"ts": f"{self.today}T02:00:00+0800", "kind": "sold", "pnl_usdt": -0.20},
        ])
        self.assertEqual(at.realized_loss(), Decimal("0.50"))

    def test_profit_offsets_loss(self) -> None:
        self._use([
            {"ts": f"{self.today}T01:00:00+0800", "kind": "sold", "pnl_usdt": -0.50},
            {"ts": f"{self.today}T02:00:00+0800", "kind": "sold", "pnl_usdt": 0.80},
        ])
        # 净赚 0.30 -> 无亏损
        self.assertEqual(at.realized_loss(), Decimal("0"))
        total, _ = at.realized_pnl()
        self.assertEqual(total, Decimal("0.30"))

    def test_only_sold_counts(self) -> None:
        self._use([
            {"ts": f"{self.today}T01:00:00+0800", "kind": "bought", "qty": "1"},
            {"ts": f"{self.today}T02:00:00+0800", "kind": "sold", "pnl_usdt": -0.10},
            {"ts": f"{self.today}T03:00:00+0800", "kind": "dust_recovered", "proceeds_usdt": "6.4"},
        ])
        self.assertEqual(at.realized_loss(), Decimal("0.10"))

    def test_daily_separates_from_cumulative(self) -> None:
        self._use([
            {"ts": "2026-01-01T01:00:00+0800", "kind": "sold", "pnl_usdt": -1.00},
            {"ts": f"{self.today}T01:00:00+0800", "kind": "sold", "pnl_usdt": -0.20},
        ])
        self.assertEqual(at.realized_loss(), Decimal("1.20"))
        self.assertEqual(at.daily_loss(), Decimal("0.20"))

    def test_missing_file_is_zero(self) -> None:
        at.LEDGER = Path(tempfile.mkdtemp()) / "nope.jsonl"
        at._PNL_CACHE = {"key": None, "total": Decimal("0"), "today": Decimal("0"), "day": ""}
        self.assertEqual(at.realized_loss(), Decimal("0"))

    def test_malformed_lines_ignored(self) -> None:
        f = Path(tempfile.mkdtemp()) / "ledger.jsonl"
        f.write_text("not json\n" + json.dumps({"kind": "sold", "pnl_usdt": -0.25,
                                                "ts": f"{self.today}T01:00:00+0800"}) + "\n", encoding="utf-8")
        at.LEDGER = f
        at._PNL_CACHE = {"key": None, "total": Decimal("0"), "today": Decimal("0"), "day": ""}
        self.assertEqual(at.realized_loss(), Decimal("0.25"))


class TestPolicyAccessors(unittest.TestCase):
    def test_daily_loss_accessor_exists(self) -> None:
        from src.risk import load_policy
        p = load_policy()
        self.assertTrue(hasattr(p, "max_daily_loss"))
        self.assertGreaterEqual(p.max_daily_loss, Decimal("0"))

    def test_source_no_longer_hardcodes_zero(self) -> None:
        """源码里不能再用 realized_loss_usdt=Decimal("0") 蒙混过关。"""
        src = (ROOT / "tools" / "auto_trader.py").read_text(encoding="utf-8")
        self.assertNotIn('realized_loss_usdt=Decimal("0")', src)
        self.assertIn("realized_loss()", src)


if __name__ == "__main__":
    unittest.main()
