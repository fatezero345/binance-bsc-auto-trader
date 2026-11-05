"""风控与执行之间唯一的数据契约。

设计约束来自 P0 任务书 §5：所有资金限制必须由独立代码执行。
因此这里只放不可变值对象，不放任何"策略建议"或"置信度"字段——
策略层给不出绕过风控的表达能力。

v2（ADR-0002）：交易场所改为 Binance 现货，标的以交易对符号（symbol）表达，
金额以 USDT 计价，数量受交易所 LOT_SIZE / NOTIONAL 过滤器约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Instrument(str, Enum):
    SPOT = "spot"                    # 现货，唯一允许
    MARGIN = "margin"                # 杠杆：禁止
    ISOLATED_MARGIN = "isolated_margin"
    FUTURES = "futures"              # 合约：禁止
    LEVERAGED_TOKEN = "leveraged_token"
    WITHDRAW = "withdraw"            # 提币：禁止
    TRANSFER = "transfer"            # 划转：禁止


class Venue(str, Enum):
    BINANCE_SPOT = "binance_spot"
    GMGN_BSC = "gmgn_bsc"            # 已归档，见 ADR-0002


@dataclass(frozen=True)
class SymbolFilters:
    """交易所侧的硬约束，来自 GET /api/v3/exchangeInfo。

    P0 实测（2026-11-03）：BTCUSDT/BNBUSDT/CAKEUSDT/ETHUSDT/SOLUSDT
    minNotional 均为 5.00000000，stepSize 见 docs/04。
    """

    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal
    min_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class OrderIntent:
    """策略层产出的、尚未获准执行的意图。

    本对象**没有** is_approved 字段。批准只能由
    `src.risk.policy.assess()` 返回的 RiskDecision 表达，
    避免上游自己构造一个"已批准"的意图。
    """

    venue: Venue
    symbol: str
    side: Side
    instrument: Instrument
    quote_notional_usdt: Decimal            # 以 USDT 计的名义本金
    base_qty: Decimal                       # 已按 stepSize 量化的基础币数量
    reference_price: Decimal                # 用于换算与限额判断的参考价
    stop_distance_bps: int
    slippage_bps: int
    fee_bps: int
    filters: SymbolFilters
    strategy_id: str = ""
    notes: str = ""


@dataclass(frozen=True)
class PortfolioState:
    """账本对外的只读快照。金额一律 USDT 计价。"""

    equity_usdt: Decimal
    realized_loss_usdt: Decimal             # 累计已实现亏损（正数=亏了）
    open_positions: int
    open_notional_usdt: Decimal = Decimal("0")
    # 计价币的**可用**余额（现货钱包 free）。没有它，风控会放行一单
    # 明明买不起的订单 —— 这是 2026-11-03 实测余额 0.00000142 USDT 时发现的缺口。
    quote_free_usdt: Decimal = Decimal("999999")


@dataclass(frozen=True)
class KeyFlags:
    """**这把 API Key 自己**的权限位，来自 GET /sapi/v1/account/apiRestrictions。

    为什么必须区分账户级与 Key 级：
      * ``/api/v3/account`` 的 ``canWithdraw`` 描述的是**账户**能力；
      * 本对象描述的是**这把 Key** 能做什么。
    2026-11-03 实测：账户 ``canWithdraw=True``，而 Key 的 ``enableWithdrawals=False``
    —— 用户已经关了提币权，读错字段的检查会误报。
    """

    enable_reading: bool
    enable_spot_trading: bool
    enable_withdrawals: bool
    enable_internal_transfer: bool
    enable_futures: bool
    enable_margin: bool
    enable_vanilla_options: bool
    permits_universal_transfer: bool
    enable_portfolio_margin: bool
    ip_restrict: bool

    def dangerous(self) -> tuple[str, ...]:
        """返回所有「开着的危险开关」。空元组才算干净。"""
        pairs = {
            "enable_withdrawals": self.enable_withdrawals,
            "enable_internal_transfer": self.enable_internal_transfer,
            "enable_futures": self.enable_futures,
            "enable_margin": self.enable_margin,
            "enable_vanilla_options": self.enable_vanilla_options,
            "permits_universal_transfer": self.permits_universal_transfer,
            "enable_portfolio_margin": self.enable_portfolio_margin,
        }
        return tuple(k for k, v in pairs.items() if v)


@dataclass(frozen=True)
class AccountPermissions:
    """账户级权限（来自 GET /api/v3/account）+ Key 级权限位。

    ``key`` 为 None 表示没有读到 Key 级权限位 —— 按 fail_closed 处理，
    由 assess() 直接拒绝，绝不当成"没有危险开关"。
    """

    can_trade: bool
    can_withdraw: bool
    can_deposit: bool
    permissions: tuple[str, ...] = ()
    key: KeyFlags | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    violations: tuple[str, ...] = ()
    computed_risk_usdt: Decimal = Decimal("0")
    # 不阻断、但必须记账的项：所有者显式接受的风险位、以及"能过但值得看一眼"的状态。
    # 与 violations 分开，是为了不把"已授权的风险"伪装成"通过"，也不让它静默消失。
    warnings: tuple[str, ...] = ()

    def raise_if_rejected(self) -> None:
        if not self.approved:
            raise RiskViolation("; ".join(self.violations) or "rejected")


class RiskViolation(RuntimeError):
    """风控拒绝。调用方不得捕获后继续下单。"""


@dataclass(frozen=True)
class ExecutionGate:
    """执行侧的闸门。与 RiskDecision 相互独立，两者都为真才可能落单。"""

    policy_enabled: bool
    dry_run: bool
    env_unlock_present: bool
    allow_mainnet: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def may_submit(self) -> bool:
        return self.policy_enabled and not self.dry_run and self.env_unlock_present
