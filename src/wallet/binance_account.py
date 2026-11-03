"""Binance 账户只读访问（鉴权，但**只读**）。

边界：
  * 只允许 GET，且只允许白名单里的路径；
  * 没有任何提币 / 划转 / 下单方法——不是"没调用"，是**没有这条路**；
  * 私钥/密钥永不落盘、永不进日志：密钥从环境变量读，用完即弃。

ADR-0002 的关键论点是：Binance API Key 可以关掉提币权限。
因此本模块最重要的输出不是余额，而是
`AccountPermissions.can_withdraw == False` 这个事实——
它是整个 CEX 方案的安全前提。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from src.risk.models import AccountPermissions

DEFAULT_BASE = "https://api.binance.com"
USER_AGENT = "binance-bsc-auto-trader/p0"

# 只读路径白名单。任何不在表里的路径直接拒绝，包括 /api/v3/order。
READ_ONLY_PATHS: frozenset[str] = frozenset(
    {
        "/api/v3/account",
        "/api/v3/openOrders",
        "/api/v3/myTrades",
        "/sapi/v1/capital/config/getall",
    }
)

ENV_API_KEY = "BINANCE_API_KEY"
ENV_API_SECRET = "BINANCE_API_SECRET"


class CredentialsMissing(RuntimeError):
    """环境里没有 API 凭据。P0 阶段这是**预期状态**，不是故障。"""


class WalletError(RuntimeError):
    pass


def load_credentials(environ: Mapping[str, str]) -> tuple[str, str]:
    key = environ.get(ENV_API_KEY, "").strip()
    secret = environ.get(ENV_API_SECRET, "").strip()
    if not key or not secret:
        raise CredentialsMissing(
            f"{ENV_API_KEY} / {ENV_API_SECRET} not set in the environment; "
            "account-level reads are BLOCKED until they are (see docs/03)"
        )
    return key, secret


def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def read(path: str, params: dict[str, Any], *, api_key: str, api_secret: str,
         base: str = DEFAULT_BASE, recv_window: int = 5000, timeout: int = 15) -> Any:
    if path not in READ_ONLY_PATHS:
        raise WalletError(f"path not in READ_ONLY_PATHS: {path}")

    q = dict(params)
    q["timestamp"] = int(time.time() * 1000)
    q["recvWindow"] = recv_window
    query = urllib.parse.urlencode(q, doseq=True)
    signature = _sign(api_secret, query)
    url = f"{base.rstrip('/')}{path}?{query}&signature={signature}"

    req = urllib.request.Request(
        url, headers={"X-MBX-APIKEY": api_key, "User-Agent": USER_AGENT}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise WalletError(f"{path} -> HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WalletError(f"{path} -> unreachable: {exc}") from exc


def get_account(api_key: str, api_secret: str, *, base: str = DEFAULT_BASE) -> dict:
    return read("/api/v3/account", {}, api_key=api_key, api_secret=api_secret, base=base)


def permissions_from_account(account: dict) -> AccountPermissions:
    """从 /api/v3/account 提取权限事实。

    Binance 在这里给出的 canWithdraw / canTrade / permissions 是
    **API Key 的权限位**，不是用户的本意——所以必须实测而不是相信配置。
    """
    return AccountPermissions(
        can_trade=bool(account.get("canTrade", False)),
        can_withdraw=bool(account.get("canWithdraw", False)),
        can_deposit=bool(account.get("canDeposit", False)),
        permissions=tuple(account.get("permissions") or ()),
    )


def balances(account: dict, assets: tuple[str, ...] = ("USDT", "BNB")) -> dict[str, str]:
    """只返回关心的资产，避免把整个账户余额表写进日志。"""
    out: dict[str, str] = {}
    for b in account.get("balances", []):
        if b.get("asset") in assets:
            out[b["asset"]] = b.get("free", "0")
    return out
