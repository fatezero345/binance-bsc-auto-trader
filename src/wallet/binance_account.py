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

from src.risk.models import AccountPermissions, KeyFlags

DEFAULT_BASE = "https://api.binance.com"
USER_AGENT = "binance-bsc-auto-trader/p0"

# 只读路径白名单。任何不在表里的路径直接拒绝，包括 /api/v3/order。
READ_ONLY_PATHS: frozenset[str] = frozenset(
    {
        "/api/v3/account",
        "/api/v3/openOrders",
        "/api/v3/myTrades",
        "/sapi/v1/capital/config/getall",
        # ★ 每一把 Key 自己的权限位。比 /api/v3/account 的 canWithdraw 更权威：
        #   canWithdraw 可能反映账户能力，enableWithdrawals 才是这把 Key 的开关。
        "/sapi/v1/account/apiRestrictions",
    }
)

ENV_API_KEY = "BINANCE_API_KEY"
ENV_API_SECRET = "BINANCE_API_SECRET"

# 语义只读、但 Binance 要求用 POST 的查询接口。
# 单独一张表 + 独立的 read_post()，是为了让"POST"这件事不能随便扩散：
# 想加一条必须改这个集合，而 transfer / withdraw / order 都不在这里。
READ_ONLY_POST_PATHS: frozenset[str] = frozenset(
    {
        "/sapi/v1/asset/get-funding-asset",   # 资金钱包余额
        "/sapi/v3/asset/getUserAsset",        # 全部钱包余额（含 valuation）
    }
)


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



# ---------------------------------------------------------------- 时钟补偿
# 2026-11-03 实测：到 api.binance.com 的 RTT 中位数 2.3s（最长 6.6s），
# 本机时钟比服务器慢 1.4–2.7s。两者叠加会超过 recvWindow，导致 -1021。
# 光调大 recvWindow 不够（RTT 抖动会持续吃余量），还要把偏差本身补掉。
_OFFSET_MS: int | None = None
_OFFSET_AT: float = 0.0
_OFFSET_TTL_S = 600.0


def server_offset_ms(refresh: bool = False) -> int:
    """server - local（毫秒），用往返中点估算，带 10 分钟缓存。"""
    global _OFFSET_MS, _OFFSET_AT
    now = time.time()
    if refresh or _OFFSET_MS is None or (now - _OFFSET_AT) > _OFFSET_TTL_S:
        try:
            from src.market.binance_public import server_time

            t0 = time.time()
            st = server_time()
            t1 = time.time()
            mid = t0 + (t1 - t0) / 2.0
            _OFFSET_MS = int(st - mid * 1000)
            _OFFSET_AT = t1
        except Exception:                                     # noqa: BLE001
            _OFFSET_MS = 0
            _OFFSET_AT = now
    return _OFFSET_MS


def now_ms() -> int:
    return int(time.time() * 1000) + server_offset_ms()


def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def read(path: str, params: dict[str, Any], *, api_key: str, api_secret: str,
         base: str = DEFAULT_BASE, recv_window: int = 20000, timeout: int = 15) -> Any:
    if path not in READ_ONLY_PATHS:
        raise WalletError(f"path not in READ_ONLY_PATHS: {path}")
    return _signed("GET", path, params, api_key=api_key, api_secret=api_secret,
                   base=base, recv_window=recv_window, timeout=timeout)


def read_post(path: str, params: dict[str, Any], *, api_key: str, api_secret: str,
              base: str = DEFAULT_BASE, recv_window: int = 20000, timeout: int = 15) -> Any:
    """语义只读、但交易所要求 POST 的查询。白名单独立，见 READ_ONLY_POST_PATHS。"""
    if path not in READ_ONLY_POST_PATHS:
        raise WalletError(f"path not in READ_ONLY_POST_PATHS: {path}")
    return _signed("POST", path, params, api_key=api_key, api_secret=api_secret,
                   base=base, recv_window=recv_window, timeout=timeout)


def _signed(method: str, path: str, params: dict[str, Any], *, api_key: str, api_secret: str,
            base: str, recv_window: int, timeout: int) -> Any:
    q = dict(params)
    q["timestamp"] = now_ms()
    q["recvWindow"] = recv_window
    query = urllib.parse.urlencode(q, doseq=True)
    signature = _sign(api_secret, query)
    url = f"{base.rstrip('/')}{path}?{query}&signature={signature}"

    def _once():
        req = urllib.request.Request(
            url,
            data=b"" if method == "POST" else None,
            headers={"X-MBX-APIKEY": api_key, "User-Agent": USER_AGENT},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -1021 = 时间戳落在 recvWindow 外。2026-11-03 实测偏差只有 ~0.75s（窗口 5s），
    # 真正的原因是 Windows 时间服务校时瞬间把本地时钟**跳**了一下：
    # 请求签名时用旧时间、服务端校验时时钟已变。稳定偏移不会触发，跳变才会。
    # 只对 **GET（只读）** 重试一次；POST（下单）绝不自动重试 —— 那可能重复成交。
    # GET 只读幂等 -> 网络抖动可重试；POST 绝不（可能重复成交）
    attempts = 3 if method == "GET" else 1
    last = None
    for i in range(attempts):
        try:
            return _once()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            last = WalletError(f"{path} -> HTTP {exc.code}: {body}")
            if exc.code == 400 and '"code":-1021' in body and i + 1 < attempts:
                time.sleep(1.0)
                continue
            raise last from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = WalletError(f"{path} -> unreachable: {exc}")
            if i + 1 < attempts:
                time.sleep(1.0 * (i + 1))   # 链路 RTT 中位数 2.3s，退避要够
                continue
            raise last from exc
    raise last if last else WalletError(f"{path} -> failed")


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


def query_order(api_key: str, api_secret: str, *, symbol: str,
                client_order_id: str | None = None, order_id: int | None = None,
                base: str = DEFAULT_BASE) -> dict:
    """GET /api/v3/order —— **只读**确认某笔订单的真实状态。

    存在的理由：下单请求如果没拿到响应（TLS 断/超时），本地不知道成交没有。
    这时唯一可靠的答案是回查交易所。**不能用余额去猜。**

    刻意不走 READ_ONLY_PATHS：那张表语义是"账户与余额查询"，
    而这条是"按 id 查订单"，用途不同，单独一个函数更容易审计。
    它只有 GET —— 下单必须走 src/execution，不能从这里溜进去。
    """
    if not client_order_id and order_id is None:
        raise WalletError("query_order needs client_order_id or order_id")
    params: dict[str, Any] = {"symbol": symbol}
    if client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        params["orderId"] = order_id
    return _signed("GET", "/api/v3/order", params, api_key=api_key, api_secret=api_secret,
                   base=base, recv_window=20000, timeout=15)

def balances(account: dict, assets: tuple[str, ...] = ("USDT", "BNB")) -> dict[str, str]:
    """只返回关心的资产，避免把整个账户余额表写进日志。"""
    out: dict[str, str] = {}
    for b in account.get("balances", []):
        if b.get("asset") in assets:
            out[b["asset"]] = b.get("free", "0")
    return out


# ---------------------------------------------------------------- 每把 Key 的权限位

# 真正危险的开关：出现任意一个为 True 都意味着这把 Key 超出本项目的授权范围
RISKY_KEYS = (
    "enableWithdrawals",
    "enableInternalTransfer",
    "enableFutures",
    "enableMargin",
    "enableVanillaOptions",
    "permitsUniversalTransfer",
    "enablePortfolioMarginTrading",
)


def get_api_restrictions(api_key: str, api_secret: str, *, base: str = DEFAULT_BASE) -> dict:
    """GET /sapi/v1/account/apiRestrictions —— 这把 Key 自己的权限位。

    这是判断「提币权到底关没关」的权威来源：
      * ``/api/v3/account`` 的 ``canWithdraw`` 描述的是**账户**能力，不是 Key 的开关；
      * 本接口的 ``enableWithdrawals`` 才是**这把 Key**的提币权限。
    两者可能不一致，不一致时必须按更严格的那个处理。
    """
    return read(
        "/sapi/v1/account/apiRestrictions", {}, api_key=api_key, api_secret=api_secret, base=base
    )


def risky_flags(restrictions: dict) -> dict[str, bool]:
    """把危险开关挑出来。缺字段不等于关闭——缺字段单独报告。"""
    return {k: bool(restrictions[k]) for k in RISKY_KEYS if k in restrictions}


def permissions_from_restrictions(restrictions: dict) -> KeyFlags:
    """把 /sapi/v1/account/apiRestrictions 映射成 KeyFlags。

    缺字段一律按 **True**（危险）处理：读不到就是不知道，不知道就不能放行。
    """
    def flag(name: str, missing_default: bool) -> bool:
        return bool(restrictions[name]) if name in restrictions else missing_default

    return KeyFlags(
        enable_reading=flag("enableReading", False),
        enable_spot_trading=flag("enableSpotAndMarginTrading", False),
        enable_withdrawals=flag("enableWithdrawals", True),
        enable_internal_transfer=flag("enableInternalTransfer", True),
        enable_futures=flag("enableFutures", True),
        enable_margin=flag("enableMargin", True),
        enable_vanilla_options=flag("enableVanillaOptions", True),
        permits_universal_transfer=flag("permitsUniversalTransfer", True),
        enable_portfolio_margin=flag("enablePortfolioMarginTrading", True),
        ip_restrict=flag("ipRestrict", False),
    )


def full_permissions(api_key: str, api_secret: str, *, base: str = DEFAULT_BASE) -> AccountPermissions:
    """一次读齐账户级 + Key 级权限，供风控使用。"""
    account = get_account(api_key, api_secret, base=base)
    base_perms = permissions_from_account(account)
    try:
        restrictions = get_api_restrictions(api_key, api_secret, base=base)
        key = permissions_from_restrictions(restrictions)
    except WalletError:
        key = None                    # 读不到 Key 级权限 → assess() 会 fail_closed
    return AccountPermissions(
        can_trade=base_perms.can_trade,
        can_withdraw=base_perms.can_withdraw,
        can_deposit=base_perms.can_deposit,
        permissions=base_perms.permissions,
        key=key,
    )
