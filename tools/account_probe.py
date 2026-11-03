#!/usr/bin/env python3
"""Binance 账户与 API Key 权限探针（鉴权，只读）。

回答三个问题：
  1. **这把 Key** 的危险开关是否全关（提币 / 内部划转 / 合约 / 杠杆 / 期权 / 万能划转）；
  2. 是否开了 IP 白名单；
  3. USDT / BNB 可用余额是多少。

为什么必须读 `/sapi/v1/account/apiRestrictions`：
    2026-11-03 实测，账户级 `/api/v3/account` 的 `canWithdraw=True`，
    而这把 Key 的 `enableWithdrawals=False`。**两者不是一回事**，
    只读账户级字段会误报"提币权开着"。

**故意不做的事**：不打印密钥、不落盘、不写日志、不发起任何写操作。

用法：
    $env:BINANCE_API_KEY    = "<key>"
    $env:BINANCE_API_SECRET = "<secret>"
    python tools/account_probe.py [--testnet]

退出码：
    0 = Key 权限与账户状态满足安全前提
    2 = BLOCKED（没有密钥）
    3 = 权限不满足安全前提（危险开关开着 / 无 IP 白名单 / 权限标记越界）—— 必须停手
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.risk import assess, load_policy  # noqa: E402
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

    section("account (GET /api/v3/account)")
    try:
        account = wa.get_account(key, secret, base=base)
    except wa.WalletError as exc:
        print(f"FAILED: {exc}")
        return 3
    acct = wa.permissions_from_account(account)
    print(f"canTrade    = {acct.can_trade}")
    print(f"canWithdraw = {acct.can_withdraw}   <-- ACCOUNT-level, not the key's flag")
    print(f"canDeposit  = {acct.can_deposit}")
    print(f"permissions = {list(acct.permissions)}")

    section("key flags (GET /sapi/v1/account/apiRestrictions)  <-- authoritative")
    try:
        restrictions = wa.get_api_restrictions(key, secret, base=base)
        keyflags = wa.permissions_from_restrictions(restrictions)
    except wa.WalletError as exc:
        print(f"FAILED to read key flags: {exc}")
        print("BLOCKED: without key-level flags the risk gate fails closed.")
        return 3

    dangerous = keyflags.dangerous()
    print(f"enableReading                = {keyflags.enable_reading}")
    print(f"enableSpotAndMarginTrading   = {keyflags.enable_spot_trading}")
    print(f"ipRestrict                   = {keyflags.ip_restrict}   <-- must be True")
    print("--- these must ALL be False ---")
    for name in wa.RISKY_KEYS:
        value = bool(restrictions.get(name))
        mark = "  <-- OPEN" if value else ""
        print(f"{name:<30} = {value}{mark}")

    section("balances (read-only, spot wallet only)")
    for asset, free in wa.balances(account).items():
        print(f"{asset:<5} free = {free}")

    section("verdict")
    perms = wa.AccountPermissions(
        can_trade=acct.can_trade,
        can_withdraw=acct.can_withdraw,
        can_deposit=acct.can_deposit,
        permissions=acct.permissions,
        key=keyflags,
    )

    problems: list[str] = []
    for flag in dangerous:
        problems.append(f"key flag {flag} is OPEN -- turn it off")
    if not keyflags.ip_restrict:
        problems.append("ipRestrict is false -- enable IP whitelist on the key")
    if not keyflags.enable_spot_trading:
        problems.append("enableSpotAndMarginTrading is false -- key cannot trade")

    allowed_pats = policy.binance_perms.get("allow_permission_patterns", [])
    banned = {m.upper() for m in policy.binance_perms.get("forbidden_permission_markers", [])}
    import re as _re

    compiled = [_re.compile(p) for p in allowed_pats]
    for perm in acct.permissions:
        up = perm.upper()
        if any(b in up for b in banned):
            problems.append(f"permission {perm!r} contains a forbidden marker")
        elif compiled and not any(c.search(perm) for c in compiled):
            problems.append(f"permission {perm!r} matches none of {allowed_pats}")

    if problems:
        for p in problems:
            print(f"  !! {p}")
        print("\nSTOP. The key does not satisfy the P0 safety precondition (docs/02 section 2).")
        print("No order code path is reachable from here anyway: execution ships disabled.")
        return 3

    print("  ok: no dangerous key flags, IP whitelist on, permissions within the allow-list")
    print("  note: this probe never places an order; execution is disabled in config/risk_policy.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
