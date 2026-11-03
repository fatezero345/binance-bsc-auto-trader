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
) -> OrderPlan:
    """纯函数：算出"如果真要下，会发什么"，并给出所有拦住它的理由。"""
    reasons: list[str] = list(gate.reasons)

    if decision is not None and not decision.approved:
        reasons.extend(decision.violations)

    if permissions.can_withdraw:
        reasons.append("account_can_withdraw_must_be_false")

    side = "BUY" if intent.side is Side.BUY else "SELL"
    if intent.instrument.value != "spot":
        reasons.append(f"instrument_not_allowed:{intent.instrument.value}")

    # 下单前再量化一次：风控放行的数量也必须能被交易所接受
    qty = quantize_down(intent.base_qty, intent.filters.step_size)
    if qty != intent.base_qty:
        reasons.append(f"qty_not_on_step:{intent.base_qty}->{qty}")
    notional = qty * intent.reference_price
    if notional < intent.filters.min_notional:
        reasons.append(f"below_exchange_min_notional:{notional}<{intent.filters.min_notional}")
    if notional > policy.max_notional:
        reasons.append(f"notional_exceeds_cap:{notional}>{policy.max_notional}")

    base_url, more = _base_url(policy, gate)
    reasons.extend(more)

    params: dict[str, Any] = {
        "symbol": intent.symbol,
        "side": side,
        "type": "MARKET",                     # 只做市价；限价单留到 P3 再评估
        "quantity": format(qty.normalize(), "f"),
        "newOrderRespType": "FULL",
    }

    would_submit = bool(base_url) and not reasons
    return OrderPlan(
        base_url=base_url or "",
        path=ORDER_PATH,
        params=params,
        would_submit=would_submit,
        reasons=tuple(reasons),
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
    """唯一含网络写入的函数。四道门不全开时抛 ExecutionRefused，不触网。"""
    if not decision.approved:
        raise ExecutionRefused("risk not approved: " + "; ".join(decision.violations))

    plan = build_order_params(intent, policy, gate, permissions, decision)
    if not plan.would_submit:
        raise ExecutionRefused("; ".join(plan.reasons) or "gate closed")

    q = dict(plan.params)
    q["timestamp"] = int(time.time() * 1000)
    q["recvWindow"] = int(policy.raw["venue"]["recv_window_ms"])
    query = urllib.parse.urlencode(q)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{plan.base_url}{plan.path}?{query}&signature={signature}"

    req = urllib.request.Request(
        url,
        data=b"",
        headers={"X-MBX-APIKEY": api_key, "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ExecutionRefused(f"order rejected HTTP {exc.code}: {body}") from exc


def describe(intent: OrderIntent, plan: OrderPlan) -> str:
    """给日报 / 账本用的一行摘要。不含任何凭据。"""
    return (
        f"{intent.side.value.upper()} {plan.params['quantity']} {intent.symbol} "
        f"@~{intent.reference_price} ~{intent.quote_notional_usdt} USDT "
        f"would_submit={plan.would_submit} reasons={list(plan.reasons)}"
    )
