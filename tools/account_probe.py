#!/usr/bin/env python3
"""Binance 账户探针（鉴权，只读）。

它只回答两个问题：
  1. Key 的权限位是否满足本项目安全前提（**提币权必须关闭**）；
  2. USDT / BNB 可用余额是多少。

**故意不做的事**：不打印密钥、不落盘、不写日志、不发起任何写操作。
输出里除余额与权限位之外没有别的账户信息（不下单历史、不拉全量余额表）。

用法：
    $env:BINANCE_API_KEY    = "<key>"
    $env:BINANCE_API_SECRET = "<secret>"
    python tools/account_probe.py
    python tools/account_probe.py --testnet       # 打测试网

退出码：
    0 = 权限满足前提且读到余额
    2 = BLOCKED（没有密钥）
    3 = 权限不满足安全前提（提币权开着 / 不能交易 / 权限超集）—— 必须停手
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.risk import load_policy  # noqa: E402
from src.wallet import binance_account as wa  # noqa: E402


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 68 - len(title)))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--testnet", action="store_true", help="target testnet.binance.vision")
    args = ap.parse_args(argv)

    policy = load_policy()
    base = policy.testnet_base_url if args.testnet else policy.base_url
    print(f"target = {base}")

    section("credentials")
    try:
        key, secret = wa.load_credentials(os.environ)
    except wa.CredentialsMissing as exc:
        print(f"BLOCKED: {exc}")
        print("\nsee docs/02-wallet-audit.md section 7 for the manual steps.")
        return 2
    print(f"{wa.ENV_API_KEY} present (len={len(key)}), secret present (len={len(secret)})")

    section("account permissions (must-have: canWithdraw == False)")
    try:
        account = wa.get_account(key, secret, base=base)
    except wa.WalletError as exc:
        print(f"FAILED: {exc}")
        return 3

    perms = wa.permissions_from_account(account)
    print(f"canTrade    = {perms.can_trade}")
    print(f"canWithdraw = {perms.can_withdraw}      <-- MUST be False")
    print(f"canDeposit  = {perms.can_deposit}")
    print(f"permissions = {list(perms.permissions)}")

    section("balances (read-only)")
    for asset, free in wa.balances(account).items():
        print(f"{asset:<5} free = {free}")

    section("verdict")
    problems: list[str] = []
    if perms.can_withdraw:
        problems.append("canWithdraw is TRUE -- revoke withdrawal permission before doing anything else")
    if not perms.can_trade:
        problems.append("canTrade is FALSE -- this key cannot trade at all")
    allowed = set(policy.binance_perms.get("require_permissions_subset_of", []))
    extra = set(perms.permissions) - allowed
    if extra:
        problems.append(f"permissions outside {sorted(allowed)}: {sorted(extra)}")

    if problems:
        for p in problems:
            print(f"  !! {p}")
        print("\nSTOP. The account does not satisfy the P0 safety precondition (docs/02 section 2).")
        print("No order code path is reachable from here anyway: execution ships disabled.")
        return 3

    print("  ok: withdrawal disabled, spot trading enabled, permissions ⊆ SPOT")
    print("  note: this probe never places an order; execution is disabled in config/risk_policy.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
