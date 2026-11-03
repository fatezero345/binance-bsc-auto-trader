"""执行层 —— 唯一允许触达交易场所的包。

ADR-0002：场所 = Binance 现货（默认目标为测试网）。

四道门必须全部打开才可能发出 POST /api/v3/order：
  1. ``src.risk.assess`` 返回 approved（含「提币权必须关闭」这一条）；
  2. ``policy.execution.enabled = true``；
  3. ``policy.execution.dry_run = false``；
  4. 环境变量 ``TRADER_LIVE_ORDERS`` 已设置；
     且主网还需 ``policy.execution.allow_mainnet = true``（第四道）。
P0 出厂状态：1–4 全部为关。

本包内**不存在**提币、划转、杠杆、合约的任何函数或常量。
"""
