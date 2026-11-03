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


@dataclass(frozen=True)
class AccountPermissions:
    """来自 GET /api/v3/account 的权限事实。

    这是 CEX 模型下最重要的一道结构性检查：提币权必须是关的。
    """

    can_trade: bool
    can_withdraw: bool
    can_deposit: bool
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    violations: tuple[str, ...] = ()
    computed_risk_usdt: Decimal = Decimal("0")

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
