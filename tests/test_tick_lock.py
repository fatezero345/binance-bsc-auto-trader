"""单实例锁与"状态未知"路径的验收测试。

这两条都是 2026-11-03 实盘暴露出来的：
  * 111 个事件里 30 次间隔 < 60s（最短 8s）—— 计划任务与手动运行重叠；
  * 一笔卖单成交了但响应丢了，本地什么都没记，下一 tick 靠余额猜。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# auto_trader 是脚本不是包，按路径加载
spec = importlib.util.spec_from_file_location("auto_trader", ROOT / "tools" / "auto_trader.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)

from src.execution.binance_spot import ExecutionRefused, OrderStateUnknown  # noqa: E402


class TestTickLock(unittest.TestCase):
    def setUp(self):
        at.LOCK.unlink(missing_ok=True)

    def tearDown(self):
        at.LOCK.unlink(missing_ok=True)

    def test_second_acquire_is_refused(self):
        self.assertTrue(at.acquire_lock())
        self.assertFalse(at.acquire_lock(), "一个 tick 在跑时，第二个必须被拒")
        at.release_lock()

    def test_release_allows_reacquire(self):
        self.assertTrue(at.acquire_lock())
        at.release_lock()
        self.assertTrue(at.acquire_lock())
        at.release_lock()

    def test_stale_lock_is_reclaimed(self):
        at.LOCK.parent.mkdir(parents=True, exist_ok=True)
        at.LOCK.write_text("99999 0", encoding="utf-8")
        old = time.time() - (at.LOCK_STALE_S + 10)
        os.utime(at.LOCK, (old, old))
        self.assertTrue(at.acquire_lock(), "僵尸锁必须能被接管，否则任务会永久卡死")
        at.release_lock()

    def test_fresh_lock_blocks(self):
        at.LOCK.parent.mkdir(parents=True, exist_ok=True)
        at.LOCK.write_text("12345 0", encoding="utf-8")
        self.assertFalse(at.acquire_lock())
        at.release_lock()


class TestOrderStateUnknown(unittest.TestCase):
    def test_is_a_subclass_so_except_order_matters(self):
        """OrderStateUnknown 必须能被 `except ExecutionRefused` 兜住，
        但调用方必须先捕它 —— 否则会被当成"已知失败"直接重试，变成两笔。"""
        self.assertTrue(issubclass(OrderStateUnknown, ExecutionRefused))

    def test_carries_client_order_id(self):
        exc = OrderStateUnknown("boom", "bt123", "CAKEUSDT")
        self.assertEqual(exc.client_order_id, "bt123")
        self.assertEqual(exc.symbol, "CAKEUSDT")
        self.assertIn("boom", str(exc))

    def test_execution_refused_has_no_client_order_id(self):
        exc = ExecutionRefused("gate closed")
        self.assertFalse(hasattr(exc, "client_order_id"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
