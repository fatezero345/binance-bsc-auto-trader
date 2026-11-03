"""Binance 现货下单——仓库里唯一能触达交易场所的模块。

P0 状态：**默认永远不落单。** 三道开关（policy.enabled / dry_run / 环境解锁）
加上第四道（allow_mainnet）全部打开才可能发出 POST /api/v3/order。

设计上刻意把"构造参数"和"发出去"分开：
`build_order_params()` 是纯函数，可以在没有任何凭据、不打网络的情况下被测试；
`submit_order()` 是唯一含网络写入的函数，且必须在四道门全开时才被调用。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.risk.models import AccountPermissions, ExecutionGate, OrderIntent, RiskDecision, Side
from src.risk.policy import Policy, quantize_down

USER_AGENT = "binance-bsc-auto-trader/p0"

# 唯一允许提交的路径。
# 提币与划转在这里不是"被注释掉了"，而是**根本不存在对应的函数与常量**。
ORDER_PATH = "/api/v3/order"
# 交易所的订单测试端点：校验签名、参数、过滤器，**不进入撮合**。
# dry_run 用这条路径，而不是"什么都不发"——否则 dry_run 永远证明不了接线是通的。
TEST_ORDER_PATH = "/api/v3/order/test"


class ExecutionRefused(RuntimeError):
    """闸门未开。这不是错误路径，是 P0 的正常行为。"""


@dataclass(frozen=True)
class OrderPlan:
    base_url: str
    path: str
    params: dict[str, Any]
    would_submit: bool
    reasons: tuple[str, ...]


def _base_url(policy: Policy, gate: ExecutionGate) -> tuple[str | None, list[str]]:
    """主网必须显式 allow_mainnet；否则只允许测试网。"""
    reasons: list[str] = []
    if gate.allow_mainnet:
        return policy.base_url, reasons
    testnet = policy.testnet_base_url
    if not testnet:
        reasons.append("no_testnet_configured_and_mainnet_not_allowed")
        return None, reasons
    return testnet, reasons


def build_order_params(
    intent: OrderIntent,
    policy: Policy,
    gate: ExecutionGate,
    permissions: AccountPermissions,
    decision: RiskDecision | None = None,
    *,
    for_preflight: bool = False,
) -> OrderPlan:
    """纯函数：算出"如果真要下，会发什么"，并给出所有拦住它的理由。

    ``for_preflight=True`` 时不要求"真实下单解锁"环境变量 —— 因为预检走的是
    /order/test，不撮合、不动钱。真实下单（submit_order）仍然强制要求它。
    """
    reasons: list[str] = [
        r for r in gate.reasons
        if not (for_preflight and r.startswith("env "))
    ]

    if decision is not None and not decision.approved:
        reasons.extend(decision.violations)

    # Key 级权限：红线拒绝、署名接受放行、没登账的拒绝。
    # 这一段必须与 src/risk/policy.py::assess() 保持同一套语义，
    # 否则会出现"风控放行、执行拦下"的自相矛盾（2026-11-03 实际踩到）。
    key = permissions.key
    if key is None:
        reasons.append("key_permissions_unavailable")
    else:
        forbidden_flags = set(policy.binance_perms.get("forbidden_key_flags", []))
        accepted_flags = dict(policy.accepted_risk_flags)
        for flag in key.dangerous():
            if flag in forbidden_flags:
                reasons.append(f"key_flag_dangerous:{flag}")
            elif flag not in accepted_flags:
                reasons.append(f"key_flag_unaccounted:{flag}")
        if not key.ip_restrict and policy.binance_perms.get("require_ip_restrict", True):
            reasons.append("key_ip_restrict_disabled")

    side = "BUY" if intent.side is Side.BUY else "SELL"
    if intent.instrument.value != "spot":
        reasons.append(f"instrument_not_allowed:{intent.instrument.value}")

    # 下单前再量化一次：风控放行的数量也必须能被交易所接受
    qty = quantize_down(intent.base_qty, intent.filters.step_size)
    if qty != intent.base_qty:
        reasons.append(f"qty_not_on_step:{intent.base_qty}->{qty}")
    notional = qty * intent.reference_price
    # 金额区间只约束开仓，**卖出必须放行** —— 否则会出现"能买不能卖"，
    # 2026-11-03 实盘就是这么被卡了 3 小时的（18 次重试全失败）。
    if intent.side is Side.BUY:
        if notional < intent.filters.min_notional:
            reasons.append(f"below_exchange_min_notional:{notional}<{intent.filters.min_notional}")
        if notional > policy.notional_ceiling:
            reasons.append(f"notional_exceeds_ceiling:{notional}>{policy.notional_ceiling}")

    base_url, more = _base_url(policy, gate)
    reasons.extend(more)

    params: dict[str, Any] = {
        "symbol": intent.symbol,
        "side": side,
        "type": "MARKET",                     # 只做市价；限价单留到 P3 再评估
        "quantity": format(qty.normalize(), "f"),
        "newOrderRespType": "FULL",
    }

    # dry_run 不是"不发"，而是"发到 /order/test"。这样每一次 dry_run 都在
    # 真实校验签名、时钟、参数与过滤器 —— 而不是在本地自我安慰。
    path = TEST_ORDER_PATH if gate.dry_run else ORDER_PATH
    would_submit = bool(base_url) and not reasons
    return OrderPlan(
        base_url=base_url or "",
        path=path,
        params=params,
        would_submit=would_submit,
        reasons=tuple(reasons),
    )


def _signed_post(path: str, params: dict[str, Any], *, api_key: str, api_secret: str,
                 base_url: str, recv_window: int, timeout: int) -> dict:
    q = dict(params)
    q["timestamp"] = int(time.time() * 1000)
    q["recvWindow"] = recv_window
    query = urllib.parse.urlencode(q)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url.rstrip('/')}{path}?{query}&signature={signature}"

    req = urllib.request.Request(
        url,
        data=b"",
        headers={"X-MBX-APIKEY": api_key, "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {"ok": True}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ExecutionRefused(f"{path} rejected HTTP {exc.code}: {body}") from exc


def preflight_order(
    intent: OrderIntent,
    gate: ExecutionGate,
    policy: Policy,
    permissions: AccountPermissions,
    api_key: str,
    api_secret: str,
    *,
    timeout: int = 15,
) -> dict:
    """打交易所的 /order/test：不撮合、不动钱，但会真校验签名与过滤器。

    刻意**不要求** RiskDecision：这条路径不产生成交，它的用途是验证接线。
    真正的下单仍然必须过 assess()。
    """
    plan = build_order_params(intent, policy, gate, permissions, None, for_preflight=True)
    if not plan.would_submit:
        raise ExecutionRefused("; ".join(plan.reasons) or "gate closed")
    if plan.path != TEST_ORDER_PATH:
        raise ExecutionRefused("preflight refused: dry_run is off, this would place a real order")
    return _signed_post(
        plan.path,
        plan.params,
        api_key=api_key,
        api_secret=api_secret,
        base_url=plan.base_url,
        recv_window=int(policy.raw["venue"]["recv_window_ms"]),
        timeout=timeout,
    )


def submit_order(
    intent: OrderIntent,
    decision: RiskDecision,
    gate: ExecutionGate,
    policy: Policy,
    permissions: AccountPermissions,
    api_key: str,
    api_secret: str,
    *,
    timeout: int = 15,
) -> dict:
    """真正下单。四道门不全开时抛 ExecutionRefused，不触网。"""
    if not decision.approved:
        raise ExecutionRefused("risk not approved: " + "; ".join(decision.violations))

    plan = build_order_params(intent, policy, gate, permissions, decision)
    if not plan.would_submit:
        raise ExecutionRefused("; ".join(plan.reasons) or "gate closed")

    return _signed_post(
        plan.path,
        plan.params,
        api_key=api_key,
        api_secret=api_secret,
        base_url=plan.base_url,
        recv_window=int(policy.raw["venue"]["recv_window_ms"]),
        timeout=timeout,
    )


def describe(intent: OrderIntent, plan: OrderPlan) -> str:
    """给日报 / 账本用的一行摘要。不含任何凭据。"""
    return (
        f"{intent.side.value.upper()} {plan.params['quantity']} {intent.symbol} "
        f"@~{intent.reference_price} ~{intent.quote_notional_usdt} USDT "
        f"would_submit={plan.would_submit} reasons={list(plan.reasons)}"
    )
