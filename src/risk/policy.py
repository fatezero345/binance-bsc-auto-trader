"""独立风控闸门（v2：Binance 现货）。

原则：fail_closed。任何"读不到 / 没字段 / 值非法 / 算不出来"都返回拒绝，
     绝不因为"看起来没问题"而放行。
"""

from __future__ import annotations

import os
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
        return Decimal(str(self._get("order_limits", "max_notional_per_order_usdt")))

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
    "max_notional", "min_notional", "max_risk_budget", "max_slippage_bps", "max_fee_bps",
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

    # 3) 账户权限：提币必须关闭（CEX 模型下最关键的一条）
    if policy.binance_perms.get("require_can_withdraw_false", True) and permissions.can_withdraw:
        v.append("account_can_withdraw_must_be_false")
    if policy.binance_perms.get("require_can_trade", True) and not permissions.can_trade:
        v.append("account_cannot_trade")
    allowed_perms = set(policy.binance_perms.get("require_permissions_subset_of", []))
    extra = set(permissions.permissions) - allowed_perms
    if extra:
        v.append(f"account_permissions_not_subset:{sorted(extra)}")

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

    # 6) 金额区间（交易所下限 与 本项目上限 双向夹逼）
    notional = intent.quote_notional_usdt
    if notional <= 0:
        v.append("notional_non_positive")
    if notional < f.min_notional:
        v.append(f"below_exchange_min_notional:{notional}<{f.min_notional}")
    if notional < policy.min_notional:
        v.append(f"below_policy_min_notional:{notional}<{policy.min_notional}")
    if notional > policy.max_notional:
        v.append(f"notional_exceeds_cap:{notional}>{policy.max_notional}")

    # 7) 意图自洽：base_qty × reference_price 应约等于 notional
    if intent.reference_price <= 0:
        v.append("reference_price_non_positive")
    else:
        implied = intent.base_qty * intent.reference_price
        if notional > 0:
            drift = abs(implied - notional) / notional
            if drift > NOTIONAL_TOLERANCE:
                v.append(f"notional_inconsistent:{implied:.8f} vs {notional}")

    # 8) 风险预算
    risk = computed_risk_usdt(intent)
    if risk > policy.max_risk_budget:
        v.append(f"risk_budget_exceeded:{risk}>{policy.max_risk_budget}")

    # 9) 滑点与手续费
    if intent.slippage_bps < 0:
        v.append("slippage_negative")
    if intent.slippage_bps > policy.max_slippage_bps:
        v.append(f"slippage_exceeds_cap:{intent.slippage_bps}>{policy.max_slippage_bps}")
    if intent.fee_bps < 0:
        v.append("fee_negative")
    if intent.fee_bps > policy.max_fee_bps:
        v.append(f"fee_exceeds_cap:{intent.fee_bps}>{policy.max_fee_bps}")

    # 10) 持仓数量（只约束开新仓方向）
    if intent.side.value == "buy":
        if state.open_positions >= policy.max_open_positions:
            v.append(
                f"max_open_positions_reached:{state.open_positions}>={policy.max_open_positions}"
            )

    # 11) 熔断
    if state.realized_loss_usdt >= policy.max_cumulative_loss:
        v.append(
            f"circuit_breaker_tripped:{state.realized_loss_usdt}>={policy.max_cumulative_loss}"
        )

    # 12) 风险不得超过本金
    if risk > state.equity_usdt:
        v.append("risk_exceeds_equity")

    return RiskDecision(approved=not v, violations=tuple(v), computed_risk_usdt=risk)


def execution_gate(policy: Policy, environ: dict[str, str] | None = None) -> ExecutionGate:
    """执行闸门：风控通过 ≠ 能落单。三道开关缺一不可。"""
    env = os.environ if environ is None else environ
    ex = policy.execution
    reasons: list[str] = []
    enabled = bool(ex.get("enabled", False))
    dry_run = bool(ex.get("dry_run", True))
    allow_mainnet = bool(ex.get("allow_mainnet", False))
    if not enabled:
        reasons.append("policy.execution.enabled=false")
    if dry_run:
        reasons.append("policy.execution.dry_run=true")
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
