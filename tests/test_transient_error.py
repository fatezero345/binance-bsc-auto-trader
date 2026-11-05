"""网络抖动重试判定的测试。

2026-11-05：08:05/08:11/08:16/09:02 四次 SSL UNEXPECTED_EOF，
钱包层 3 次重试全挂，随后整个 tick 失败并按 300s 周期干等 —— 白丢一个周期。
修复：识别为网络类错误后 60s 短退避重试本轮，最多 2 次。
本测试锁住"什么算网络抖动、什么不算"。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("at", ROOT / "tools" / "auto_trader.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)


class TestTransient(unittest.TestCase):
    def test_ssl_eof_is_transient(self) -> None:
        real = ("tick_error: WalletError: /api/v3/account -> unreachable: "
                "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred "
                "in violation of protocol (_ssl.c:1032)>")
        self.assertTrue(at.is_transient_network_error(real))

    def test_timeout_is_transient(self) -> None:
        self.assertTrue(at.is_transient_network_error("TimeoutError: timed out"))
        self.assertTrue(at.is_transient_network_error("URLError: <urlopen error>"))
        self.assertTrue(at.is_transient_network_error("ConnectionResetError: [Errno 104]"))

    def test_logic_errors_are_not_transient(self) -> None:
        for msg in (
            "KeyError: 'positions'",
            "Decimal('5.02') > Decimal('5.00')",
            "ValueError: invalid literal",
            "ExecutionRefused: notional_exceeds_ceiling",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(at.is_transient_network_error(msg))

    def test_empty_is_not_transient(self) -> None:
        self.assertFalse(at.is_transient_network_error(""))
        self.assertFalse(at.is_transient_network_error(None))  # type: ignore[arg-type]

    def test_constants_sane(self) -> None:
        self.assertGreaterEqual(at.NETWORK_RETRY_MAX, 1)
        self.assertLessEqual(at.NETWORK_RETRY_MAX, 5)
        self.assertLess(at.NETWORK_RETRY_DELAY_S, 300,
                        "重试间隔必须短于正常周期，否则没有意义")


if __name__ == "__main__":
    unittest.main()
