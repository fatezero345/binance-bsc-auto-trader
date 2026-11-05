"""独立风控闸门（v2：Binance 现货）。

原则：fail_closed。任何"读不到 / 没字段 / 值非法 / 算不出来"都返回拒绝，
     绝不因为"看起来没问题"而放行。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from .models import (
    AccountPermissions,
    ExecutionGate,
    Instrument,
    OrderIntent,
    PortfolioState,
    RiskDecision,
    Venue,
)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "risk_policy.toml"

BPS = Decimal(10_000)
# 名义本金与 base_qty×price 之间允许的相对误差（价格选取时点不同会有微小漂移）
NOTIONAL_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True)
class Policy:
    raw: dict

    def _get(self, *keys: str):
        node = self.raw
        for k in keys:
            node = node[k]
        return node

    # ---- 资金 ----
    @property
    def max_notional(self) -> Decimal:
        """单笔**目标**金额。取整后的实际值可以略高，见 notional_ceiling。"""
        return Decimal(str(self._get("order_limits", "max_notional_per_order_usdt")))

    @property
    def overrun_bps(self) -> int:
        return int(self._get("order_limits", "max_quantization_overrun_bps"))

    @property
    def notional_ceiling(self) -> Decimal:
        """硬上限 = 目标 × (1 + 量化上浮容许度)。

        存在的理由：stepSize 是离散的，取整后的名义本金不可能恰好等于目标值。
        CAKEUSDT 在 5.00 目标下最小合法单是 5.0184 USDT —— 没有这点容许度，
        整条策略在任何价格下都会被自己的 cap 拒死。
        """
        return (self.max_notional * (Decimal(1) + Decimal(self.overrun_bps) / BPS)).quantize(
            Decimal("0.0001")
        )

    @property
    def min_notional(self) -> Decimal:
        return Decimal(str(self._get("order_limits", "min_notional_usdt")))

    @property
    def max_risk_budget(self) -> Decimal:
        return Decimal(str(self._get("order_limits", "max_risk_budget_per_order_usdt")))

    @property
    def max_slippage_bps(self) -> int:
        return int(self._get("order_limits", "max_slippage_bps"))

    @property
    def max_fee_bps(self) -> int:
        return int(self._get("order_limits", "max_fee_bps"))

    # ---- 组合 ----
    @property
    def max_open_positions(self) -> int:
        return int(self._get("portfolio", "max_open_positions"))

    @property
    def max_daily_loss(self) -> Decimal:
        """当日已实现亏损上限。0 或缺失表示不启用。

        与 max_cumulative_loss 的区别：累计熔断管的是"总账"，反应慢；
        日内上限管的是"今天手气不对就收手"，能在坏日子及时止损。
        """
        try:
            return Decimal(str(self._get("circuit_breaker", "max_daily_realized_loss_usdt")))
        except Exception:                                    # noqa: BLE001
            return Decimal("0")

    @property
    def max_cumulative_loss(self) -> Decimal:
        return Decimal(str(self._get("circuit_breaker", "max_cumulative_realized_loss_usdt")))

    # ---- 场所 ----
    @property
    def venue_primary(self) -> str:
        return str(self._get("venue", "primary"))

    @property
    def archived_venues(self) -> frozenset[str]:
        return frozenset(self._get("venue", "archived"))

    @property
    def base_url(self) -> str:
        return str(self._get("venue", "base_url"))

    @property
    def testnet_base_url(self) -> str:
        return str(self._get("venue", "testnet_base_url"))

    # ---- Binance 权限要求 ----
    @property
    def binance_perms(self) -> dict:
        return self._get("venue", "binance")

    @property
    def strategy(self) -> dict:
        return self.raw.get("strategy", {}) or {}

    @property
    def accepted_risk_flags(self) -> dict:
        """所有者已署名接受的风险位（flag -> 理由）。放行但记账。"""
        return self.binance_perms.get("accepted_risk_flags", {}) or {}

    # ---- 标的 ----
    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(s.upper() for s in self._get("whitelist", "symbols"))

    @property
    def observer_symbols(self) -> frozenset[str]:
        return frozenset(s.upper() for s in self._get("whitelist", "observer_symbols"))

    @property
    def quote_assets(self) -> frozenset[str]:
        return frozenset(self._get("whitelist", "quote_assets"))

    @property
    def forbidden(self) -> dict:
        return self._get("forbidden")

    @property
    def execution(self) -> dict:
        return self._get("execution")


_REQUIRED_PROBES = (
    "max_notional", "overrun_bps", "notional_ceiling", "min_notional",
    "max_risk_budget", "max_slippage_bps", "max_fee_bps",
    "max_open_positions", "max_cumulative_loss", "venue_primary", "archived_venues",
    "base_url", "testnet_base_url", "binance_perms", "symbols", "observer_symbols",
    "quote_assets", "forbidden", "execution",
)


def load_policy(path: Path | str | None = None) -> Policy:
    p = Path(path) if path else DEFAULT_POLICY_PATH
    with p.open("rb") as fh:                     # tomllib 只收二进制
        raw = tomllib.load(fh)
    probe = Policy(raw)
    for name in _REQUIRED_PROBES:               # 启动期全量校验，不等到第一笔订单
        getattr(probe, name)
    return probe


# ---------------------------------------------------------------- 量化


def quantize_down(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_up(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    q = (qty / step).to_integral_value(rounding="ROUND_CEILING") * step
    return q


def plan_buy_quantity(
    price: Decimal, budget_usdt: Decimal, min_notional: Decimal, step: Decimal, min_qty: Decimal
) -> tuple[Decimal | None, str | None]:
    """在「minNotional ≤ qty×price ≤ budget」内找一个合法的、按步长对齐的数量。

    返回 (qty, None) 或 (None, 拒绝原因)。

    这个函数存在的理由很具体：步长会把单子挤到 minNotional 以下。
    例：CAKEUSDT step=0.01、price=1.844、minNotional=5
        5/1.844 = 2.7115 CAKE → 向下取整 2.71 → 2.71×1.844 = 4.997 < 5  ❌
        最小合法量是 2.72（5.016 USDT）。
    四舍五入到 2.71 会得到一个"看起来下了、其实被交易所拒掉"的订单，
    所以这里一律拒绝而不是猜。
    """
    if price <= 0 or budget_usdt <= 0:
        return None, "price_or_budget_non_positive"

    # 2026-11-03 实盘发现：从 minNotional 向上取整会让每一单都只落在
    # 交易所最小合法量上（上限 8.00 却只用了 5.5），资金根本投不出去。
    # 正确做法是**按预算向下取整**；只有当向下取整跌破 minNotional 时，
    # 才回退到"最小合法量"那条路径（旧的 5 元上限场景正是这样）。
    qty = quantize_down(budget_usdt / price, step)
    if qty <= 0 or qty * price < min_notional:
        qty = quantize_up(min_notional / price, step)
    if qty < min_qty:
        qty = quantize_up(min_qty, step)
    if qty <= 0:
        return None, "quantized_to_zero"

    notional = qty * price
    if notional < min_notional:
        return None, f"quantized_below_min_notional:{notional}<{min_notional}"
    if notional > budget_usdt:
        return None, f"quantized_above_budget:{notional}>{budget_usdt}"
    return qty, None


# ---------------------------------------------------------------- 风控


def computed_risk_usdt(intent: OrderIntent) -> Decimal:
    """单笔最大可能亏损 = 名义本金 × 止损距离。"""
    return (intent.quote_notional_usdt * Decimal(intent.stop_distance_bps) / BPS).quantize(
        Decimal("0.0001")
    )


def assess(
    intent: OrderIntent,
    state: PortfolioState,
    permissions: AccountPermissions,
    policy: Policy,
) -> RiskDecision:
    """对单个意图做硬检查。任何一条失败即整体拒绝。"""
    v: list[str] = []
    f = intent.filters

    # 1) 场所：必须是 primary，且不能是已归档场所
    if intent.venue.value != policy.venue_primary:
        v.append(f"venue_not_primary:{intent.venue.value}!={policy.venue_primary}")
    if intent.venue.value in policy.archived_venues:
        v.append(f"venue_archived:{intent.venue.value}")

    # 2) 品种：只允许现货
    if intent.instrument is not Instrument.SPOT:
        v.append(f"instrument_not_allowed:{intent.instrument.value}")
    forbidden_modes = set(policy.binance_perms.get("forbidden_modes", []))
    if intent.instrument.value in forbidden_modes:
        v.append(f"forbidden_mode:{intent.instrument.value}")

    # 3) 账户与 Key 权限
    if policy.binance_perms.get("require_can_trade", True) and not permissions.can_trade:
        v.append("account_cannot_trade")

    # ★ 提币权与所有危险开关，一律以 **Key 级**权限位为准。
    #   账户级 canWithdraw 不等于这把 Key 能不能提币（2026-11-03 实测两者不一致）。
    key = permissions.key
    warnings: list[str] = []
    if key is None:
        v.append("key_permissions_unavailable")
    else:
        forbidden_flags = set(policy.binance_perms.get("forbidden_key_flags", []))
        accepted_flags = dict(policy.accepted_risk_flags)
        for flag in key.dangerous():
            if flag in forbidden_flags:
                v.append(f"key_flag_dangerous:{flag}")
            elif flag in accepted_flags:
                # 放行，但必须留下痕迹：谁签的、为什么
                warnings.append(f"key_flag_accepted:{flag}")
            else:
                # 既没禁、也没人签 —— 不允许静默通过
                v.append(f"key_flag_unaccounted:{flag}")
        if policy.binance_perms.get("require_ip_restrict", True) and not key.ip_restrict:
            v.append("key_ip_restrict_disabled")
        elif not key.ip_restrict:
            warnings.append("key_ip_restrict_disabled_accepted")
        if not key.enable_reading:
            v.append("key_reading_disabled")
        if not key.enable_spot_trading:
            v.append("key_spot_trading_disabled")

    # 权限标记：允许清单（SPOT / TRD_GRP_<n>）+ 禁止词，双段判定。
    # 2026-11-03 实测：真实账户返回 ["TRD_GRP_236"]，纯 SPOT 白名单会误杀。
    allowed = [re.compile(p) for p in policy.binance_perms.get("allow_permission_patterns", [])]
    banned = {m.upper() for m in policy.binance_perms.get("forbidden_permission_markers", [])}
    for perm in permissions.permissions:
        up = perm.upper()
        if any(b in up for b in banned):
            v.append(f"account_permission_forbidden:{perm}")
        elif allowed and not any(a.search(perm) for a in allowed):
            v.append(f"account_permission_not_allowed:{perm}")

    # 4) 标的白名单
    if intent.symbol.upper() not in policy.symbols:
        if intent.symbol.upper() in policy.observer_symbols:
            v.append(f"symbol_observer_only:{intent.symbol}")
        else:
            v.append(f"symbol_not_allowed:{intent.symbol}")
    if f.symbol.upper() != intent.symbol.upper():
        v.append(f"filters_symbol_mismatch:{f.symbol}!={intent.symbol}")
    if f.quote_asset not in policy.quote_assets:
        v.append(f"quote_asset_not_allowed:{f.quote_asset}")

    # 5) 交易所过滤器：步长 / 最小量 / 最小名义
    if f.step_size <= 0:
        v.append("filters_step_size_invalid")
    elif intent.base_qty % f.step_size != 0:
        v.append(f"base_qty_off_step:{intent.base_qty}%{f.step_size}!=0")
    if intent.base_qty < f.min_qty:
        v.append(f"base_qty_below_min:{intent.base_qty}<{f.min_qty}")

    # 6) 金额区间 —— **只约束开仓方向（BUY）**。
    #    2026-11-03 实盘暴露：把 minNotional / ceiling 也套在 SELL 上，
    #    导致"能买不能卖"——持仓 3 小时无法退出，18 次重试全部失败。
    #    退出的优先级高于任何资金上限：不让人离场的风控不是风控，是牢笼。
    notional = intent.quote_notional_usdt
    is_buy = intent.side.value == "buy"
    if notional <= 0:
        v.append("notional_non_positive")
    if is_buy:
        if notional < f.min_notional:
            v.append(f"below_exchange_min_notional:{notional}<{f.min_notional}")
        if notional < policy.min_notional:
            v.append(f"below_policy_min_notional:{notional}<{policy.min_notional}")
        if notional > policy.notional_ceiling:
            v.append(f"notional_exceeds_ceiling:{notional}>{policy.notional_ceiling}")

    # 7) 意图自洽：base_qty × reference_price 应约等于 notional
    if intent.reference_price <= 0:
        v.append("reference_price_non_positive")
    else:
        implied = intent.base_qty * intent.reference_price
        if notional > 0:
            drift = abs(implied - notional) / notional
            if drift > NOTIONAL_TOLERANCE:
                v.append(f"notional_inconsistent:{implied:.8f} vs {notional}")

    # 8) 风险预算 —— 只对开仓有意义；卖出是了结风险，不受预算限制
    risk = computed_risk_usdt(intent)
    if is_buy and risk > policy.max_risk_budget:
        v.append(f"risk_budget_exceeded:{risk}>{policy.max_risk_budget}")

    # 9) 滑点与手续费（两个方向都管）
    if intent.slippage_bps < 0:
        v.append("slippage_negative")
    if intent.slippage_bps > policy.max_slippage_bps:
        v.append(f"slippage_exceeds_cap:{intent.slippage_bps}>{policy.max_slippage_bps}")
    if intent.fee_bps < 0:
        v.append("fee_negative")
    if intent.fee_bps > policy.max_fee_bps:
        v.append(f"fee_exceeds_cap:{intent.fee_bps}>{policy.max_fee_bps}")

    # 10) 持仓数量（只约束开新仓方向）
    if is_buy:
        if state.open_positions >= policy.max_open_positions:
            v.append(
                f"max_open_positions_reached:{state.open_positions}>={policy.max_open_positions}"
            )

    # 11) 熔断 —— 只拦新的开仓。**卖出永远放行**：
    #     熔断的目的是停止冒险，不是把人锁在仓位里（2026-11-03 实盘教训）。
    if is_buy and state.realized_loss_usdt >= policy.max_cumulative_loss:
        v.append(
            f"circuit_breaker_tripped:{state.realized_loss_usdt}>={policy.max_cumulative_loss}"
        )

    # 12) 风险不得超过本金（开仓侧）
    if is_buy and risk > state.equity_usdt:
        v.append("risk_exceeds_equity")

    # 13) 可用余额：买不起就是买不起。
    #     2026-11-03 实测账户只有 0.00000142 USDT 时才发现的缺口 ——
    #     没有这一条，风控会放行一单 5 USDT 的订单，然后在交易所被 -2010 拒掉。
    if intent.side.value == "buy" and intent.quote_notional_usdt > state.quote_free_usdt:
        v.append(
            f"insufficient_free_balance:{intent.quote_notional_usdt}>{state.quote_free_usdt}"
        )

    return RiskDecision(
        approved=not v, violations=tuple(v), computed_risk_usdt=risk, warnings=tuple(warnings)
    )


def execution_gate(policy: Policy, environ: dict[str, str] | None = None) -> ExecutionGate:
    """执行闸门。

    dry_run **不是**拒绝理由——它决定请求打到 `/order/test` 还是 `/order`。
    真正拦住真实下单的只有两个：enabled=false、以及环境解锁变量未设置。
    """
    env = os.environ if environ is None else environ
    ex = policy.execution
    reasons: list[str] = []
    enabled = bool(ex.get("enabled", False))
    dry_run = bool(ex.get("dry_run", True))
    allow_mainnet = bool(ex.get("allow_mainnet", False))
    if not enabled:
        reasons.append("policy.execution.enabled=false")
    unlock_name = str(ex.get("require_env_unlock", ""))
    unlock_present = bool(env.get(unlock_name))
    if not unlock_present:
        reasons.append(f"env {unlock_name} not set")
    return ExecutionGate(
        policy_enabled=enabled,
        dry_run=dry_run,
        env_unlock_present=unlock_present,
        allow_mainnet=allow_mainnet,
        reasons=tuple(reasons),
    )
