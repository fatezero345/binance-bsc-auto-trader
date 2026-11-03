"""硬风控的验收测试（v2：Binance 现货）。

运行：  python -m unittest discover -s tests -v
不依赖 pytest：只要 Python ≥3.11 就能跑，CI 与本地口径一致。
过滤器数值取自 2026-11-03 的 GET /api/v3/exchangeInfo（见 docs/evidence/raw/）。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.risk import (  # noqa: E402
    AccountPermissions,
    KeyFlags,
    Instrument,
    OrderIntent,
    Policy,
    PortfolioState,
    Side,
    SymbolFilters,
    Venue,
    assess,
    computed_risk_usdt,
    execution_gate,
    load_policy,
    plan_buy_quantity,
    quantize_down,
    quantize_up,
)

# --- 2026-11-03 实测的交易所过滤器（不要凭印象改） ---------------------------
D = Decimal
FILTERS = {
    "BTCUSDT": SymbolFilters("BTCUSDT", "BTC", "USDT", D("0.00001000"), D("0.00001000"), D("0.01000000"), D("5.00000000")),
    "BNBUSDT": SymbolFilters("BNBUSDT", "BNB", "USDT", D("0.00100000"), D("0.00100000"), D("0.01000000"), D("5.00000000")),
    "CAKEUSDT": SymbolFilters("CAKEUSDT", "CAKE", "USDT", D("0.01000000"), D("0.01000000"), D("0.00100000"), D("5.00000000")),
    "ETHUSDT": SymbolFilters("ETHUSDT", "ETH", "USDT", D("0.00010000"), D("0.00010000"), D("0.01000000"), D("5.00000000")),
}
PRICE = {"BTCUSDT": D("76842.01"), "BNBUSDT": D("696.53"), "CAKEUSDT": D("1.844"), "ETHUSDT": D("2254.64")}

SAFE_KEY = KeyFlags(
    enable_reading=True, enable_spot_trading=True, enable_withdrawals=False,
    enable_internal_transfer=False, enable_futures=False, enable_margin=False,
    enable_vanilla_options=False, permits_universal_transfer=False,
    enable_portfolio_margin=False, ip_restrict=True,
)

GOOD_PERMS = AccountPermissions(
    can_trade=True, can_withdraw=False, can_deposit=False,
    permissions=("SPOT",), key=SAFE_KEY,
)


def intent(symbol: str = "CAKEUSDT", qty: str = "2.72", notional: str = "5.01568", **over) -> OrderIntent:
    base = dict(
        venue=Venue.BINANCE_SPOT,
        symbol=symbol,
        side=Side.BUY,
        instrument=Instrument.SPOT,
        quote_notional_usdt=D(notional),
        base_qty=D(qty),
        reference_price=PRICE[symbol],
        stop_distance_bps=500,
        slippage_bps=50,
        fee_bps=10,
        filters=FILTERS[symbol],
    )
    base.update(over)
    return OrderIntent(**base)


def state(**over) -> PortfolioState:
    base = dict(
        equity_usdt=D("29.30"),
        realized_loss_usdt=D("0"),
        open_positions=0,
    )
    base.update(over)
    return PortfolioState(**base)


class TestPolicyLoad(unittest.TestCase):
    def test_venue_is_binance_spot(self):
        p = load_policy()
        self.assertEqual(p.venue_primary, "binance_spot")
        self.assertIn("gmgn_bsc", p.archived_venues)

    def test_hard_limits(self):
        p = load_policy()
        self.assertEqual(p.max_notional, D("5.00"))
        self.assertEqual(p.overrun_bps, 100)
        self.assertEqual(p.notional_ceiling, D("5.0500"))
        self.assertEqual(p.min_notional, D("5.00"))
        self.assertEqual(p.max_risk_budget, D("0.30"))
        self.assertEqual(p.max_cumulative_loss, D("1.50"))
        self.assertEqual(p.max_open_positions, 1)

    def test_ceiling_leaves_a_non_empty_order_window(self):
        """cap 5.00 + 交易所下限 5.00 → 窗口全靠量化上浮容许度撑开，否则恒不可成交。"""
        p = load_policy()
        self.assertLess(p.min_notional, p.notional_ceiling)
        self.assertAlmostEqual(float(p.notional_ceiling - p.min_notional), 0.05, places=4)

    def test_execution_posture_after_owner_authorisation(self):
        """ADR-0004：路径已打开，dry_run 语义变成「打 /order/test」。

        唯一还拦着真实下单的是环境解锁变量 —— 这条不能松。
        """
        p = load_policy()
        self.assertTrue(p.execution["enabled"])
        self.assertTrue(p.execution["dry_run"])
        self.assertTrue(p.execution["allow_mainnet"])
        self.assertEqual(p.execution["require_env_unlock"], "TRADER_LIVE_ORDERS")

    def test_symbol_whitelist(self):
        p = load_policy()
        self.assertEqual(p.symbols, frozenset({"BTCUSDT", "BNBUSDT", "CAKEUSDT"}))
        self.assertIn("ETHUSDT", p.observer_symbols)

    def test_circuit_breaker_is_fail_closed(self):
        self.assertEqual(load_policy().raw["circuit_breaker"]["mode"], "fail_closed")

    def test_forbidden_modes_cover_withdraw_and_leverage(self):
        modes = set(load_policy().binance_perms["forbidden_modes"])
        for m in ("withdraw", "transfer", "margin", "futures"):
            self.assertIn(m, modes)


class TestQuantization(unittest.TestCase):
    def test_quantize_helpers(self):
        self.assertEqual(quantize_down(D("2.7115"), D("0.01")), D("2.71"))
        self.assertEqual(quantize_up(D("2.7115"), D("0.01")), D("2.72"))

    def test_the_cake_trap_floor_is_below_min_notional(self):
        """向下取整会得到 2.71 → 4.9972 USDT < 5 的 minNotional。"""
        floored = quantize_down(D("5") / PRICE["CAKEUSDT"], FILTERS["CAKEUSDT"].step_size)
        self.assertEqual(floored, D("2.71"))
        self.assertLess(floored * PRICE["CAKEUSDT"], FILTERS["CAKEUSDT"].min_notional)

    def test_plan_buy_steps_up_past_min_notional(self):
        qty, err = plan_buy_quantity(PRICE["CAKEUSDT"], D("10"), D("5"), D("0.01"), D("0.01"))
        self.assertIsNone(err, qty)
        self.assertEqual(qty, D("2.72"))
        self.assertGreaterEqual(qty * PRICE["CAKEUSDT"], D("5"))

    def test_plan_buy_btc(self):
        qty, err = plan_buy_quantity(PRICE["BTCUSDT"], D("10"), D("5"), D("0.00001"), D("0.00001"))
        self.assertIsNone(err)
        self.assertEqual(qty, D("0.00007"))

    def test_plan_buy_rejects_when_step_overshoots_budget(self):
        qty, err = plan_buy_quantity(PRICE["BTCUSDT"], D("5.10"), D("5"), D("0.00001"), D("0.00001"))
        self.assertIsNone(qty)
        self.assertIn("quantized_above_budget", err)

    def test_nominal_step_decides_whether_a_five_dollar_cap_is_reachable(self):
        """把单笔压到 5.00 后的通用判据：名义步长 = stepSize × price。

        只有当「名义步长 ≤ 量化窗口宽度(= ceiling − 交易所下限 = 0.05 USDT)」时，
        该标的才**恒**可成交；否则要看价格是否恰好落在某个步长格点上。
        """
        window = D("0.05")          # 5.05 ceiling - 5.00 floor
        for symbol, price in PRICE.items():
            nominal = FILTERS[symbol].step_size * price
            with self.subTest(symbol=symbol):
                if symbol == "CAKEUSDT":
                    self.assertLessEqual(nominal, window)      # 0.01 × 1.844 ≈ 0.0184
                else:
                    self.assertGreater(nominal, window)        # BNB ≈ 0.70 / BTC ≈ 0.77

    def test_plan_buy_rejects_degenerate_inputs(self):
        self.assertIsNotNone(plan_buy_quantity(D("0"), D("10"), D("5"), D("0.01"), D("0.01"))[1])
        self.assertIsNotNone(plan_buy_quantity(PRICE["CAKEUSDT"], D("0"), D("5"), D("0.01"), D("0.01"))[1])


class TestRiskGate(unittest.TestCase):
    def setUp(self):
        self.p = load_policy()

    def test_happy_path_approved(self):
        d = assess(intent(), state(), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)

    def test_key_withdraw_flag_open_is_rejected(self):
        """ADR-0002 的核心：**Key 级**提币权必须关闭，否则整个 CEX 方案不成立。"""
        unsafe = KeyFlags(
            enable_reading=True, enable_spot_trading=True, enable_withdrawals=True,
            enable_internal_transfer=False, enable_futures=False, enable_margin=False,
            enable_vanilla_options=False, permits_universal_transfer=False,
            enable_portfolio_margin=False, ip_restrict=True,
        )
        perms = AccountPermissions(True, False, False, ("SPOT",), unsafe)
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertIn("key_flag_dangerous:enable_withdrawals", d.violations)

    def test_account_level_can_withdraw_alone_is_not_a_violation(self):
        """2026-11-03 实测：账户 canWithdraw=True 而 Key 的 enableWithdrawals=False。

        账户级字段描述账户能力，不是这把 Key 的开关。以它为准会误报，
        导致用户关掉提币权后仍被拦住（这正是本次踩到的坑）。
        """
        perms = AccountPermissions(True, True, True, ("SPOT",), SAFE_KEY)
        d = assess(intent(), state(), perms, self.p)
        self.assertTrue(d.approved, d.violations)

    def _key(self, **over) -> KeyFlags:
        kw = dict(
            enable_reading=True, enable_spot_trading=True, enable_withdrawals=False,
            enable_internal_transfer=False, enable_futures=False, enable_margin=False,
            enable_vanilla_options=False, permits_universal_transfer=False,
            enable_portfolio_margin=False, ip_restrict=True,
        )
        kw.update(over)
        return KeyFlags(**kw)

    def test_hard_red_line_flags_are_rejected(self):
        """红线只有两条：能把钱搬出平台的（提币 / 万能划转）。"""
        for flag in ("enable_withdrawals", "permits_universal_transfer"):
            with self.subTest(flag=flag):
                perms = AccountPermissions(True, False, False, ("SPOT",), self._key(**{flag: True}))
                d = assess(intent(), state(), perms, self.p)
                self.assertFalse(d.approved)
                self.assertIn(f"key_flag_dangerous:{flag}", d.violations)

    def test_owner_accepted_flags_pass_but_are_recorded(self):
        """ADR-0003：所有者署名接受的权限位放行，但必须留下 warnings 痕迹。

        这两个断言合起来才是完整的：既不能因为"接受了"就静默通过，
        也不能因为"危险"就把已经签过的决定反复推翻。
        """
        for flag in ("enable_futures", "enable_internal_transfer", "enable_margin",
                     "enable_portfolio_margin", "enable_vanilla_options"):
            with self.subTest(flag=flag):
                perms = AccountPermissions(True, False, False, ("SPOT",), self._key(**{flag: True}))
                d = assess(intent(), state(), perms, self.p)
                self.assertTrue(d.approved, d.violations)
                self.assertIn(f"key_flag_accepted:{flag}", d.warnings)

    def test_unaccounted_flag_is_rejected(self):
        """既不在红线上、也没有署名接受记录的风险位 —— 不允许静默通过。"""
        # 用一个不在任何一张表里的危险位（KeyFlags.dangerous() 里没有的不会被扫到，
        # 所以这里用配置层面能构造的方式验证：把 accepted 表清空后 futures 应被拒）
        from src.risk import Policy as _P

        raw = load_policy().raw
        raw["venue"]["binance"]["accepted_risk_flags"] = {}
        p2 = _P(raw)
        perms = AccountPermissions(True, False, False, ("SPOT",), self._key(enable_futures=True))
        d = assess(intent(), state(), perms, p2)
        self.assertFalse(d.approved)
        self.assertIn("key_flag_unaccounted:enable_futures", d.violations)

    def test_missing_key_flags_fail_closed(self):
        perms = AccountPermissions(True, False, False, ("SPOT",), None)
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertIn("key_permissions_unavailable", d.violations)

    def test_ip_restrict_disabled_is_a_warning_not_a_block(self):
        """ADR-0003：实验账户接受无 IP 白名单 —— 放行但记账。"""
        perms = AccountPermissions(True, False, False, ("SPOT",), self._key(ip_restrict=False))
        d = assess(intent(), state(), perms, self.p)
        self.assertTrue(d.approved, d.violations)
        self.assertIn("key_ip_restrict_disabled_accepted", d.warnings)

    def test_insufficient_free_balance_is_rejected(self):
        """2026-11-03 实测账户只有 0.00000142 USDT 时补上的缺口。"""
        broke = PortfolioState(
            equity_usdt=D("29.30"), realized_loss_usdt=D("0"), open_positions=0,
            quote_free_usdt=D("0.00000142"),
        )
        d = assess(intent(), broke, GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("insufficient_free_balance") for v in d.violations))

    def test_account_cannot_trade_is_rejected(self):
        perms = AccountPermissions(False, False, False, ("SPOT",), SAFE_KEY)
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertIn("account_cannot_trade", d.violations)

    def test_margin_permission_is_rejected(self):
        perms = AccountPermissions(True, False, False, ("SPOT", "MARGIN"), SAFE_KEY)
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertIn("account_permission_forbidden:MARGIN", d.violations)

    def test_trade_group_permission_is_accepted(self):
        """2026-11-03 实测：真实账户返回 ["TRD_GRP_236"]。

        纯 SPOT 白名单会把自己的账户误杀——这是真数据打出来的 bug，已改成
        「允许清单（SPOT | TRD_GRP_<n>）+ 禁止词」双段判定。
        """
        perms = AccountPermissions(True, False, False, ("TRD_GRP_236",), SAFE_KEY)
        d = assess(intent(), state(), perms, self.p)
        self.assertTrue(d.approved, d.violations)

    def test_unknown_permission_marker_is_rejected(self):
        perms = AccountPermissions(True, False, False, ("SOMETHING_NEW",), SAFE_KEY)
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("account_permission_not_allowed") for v in d.violations))

    def test_observer_symbol_rejected_with_specific_reason(self):
        d = assess(intent("ETHUSDT", "0.0023", "5.18567"), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertIn("symbol_observer_only:ETHUSDT", d.violations)

    def test_unknown_symbol_rejected(self):
        i = intent()
        object.__setattr__(i, "symbol", "DOGEUSDT")
        d = assess(i, state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertIn("symbol_not_allowed:DOGEUSDT", d.violations)

    def test_archived_venue_rejected(self):
        d = assess(intent(venue=Venue.GMGN_BSC), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertIn("venue_archived:gmgn_bsc", d.violations)
        self.assertTrue(any(v.startswith("venue_not_primary") for v in d.violations))

    def test_notional_target_and_ceiling(self):
        # 目标 5.00，硬上限 5.05。2.72@1.845=5.0184 在窗口内
        d = assess(intent(qty="2.72", notional="5.0184", stop_distance_bps=300),
                   state(), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)
        # 2.73@1.845=5.03685 仍在上浮容许度内
        d2 = assess(intent(qty="2.73", notional="5.03685", stop_distance_bps=300),
                    state(), GOOD_PERMS, self.p)
        self.assertTrue(d2.approved, d2.violations)
        # 2.74@1.845=5.0553 超硬上限
        d3 = assess(intent(qty="2.74", notional="5.0553", stop_distance_bps=300),
                    state(), GOOD_PERMS, self.p)
        self.assertFalse(d3.approved)
        self.assertTrue(any(v.startswith("notional_exceeds_ceiling") for v in d3.violations))

    def test_five_dollar_cap_makes_btc_and_bnb_untradeable(self):
        """把单笔压到 5.00 的直接后果：BTC/BNB 的最小合法单就超过硬上限 5.05。

        这是算术，不是配置失误 —— 步长粒度决定最小可成交量：
          BNBUSDT step=0.001 @ 696.53 → 最小 0.008 BNB = 5.5722 USDT
          BTCUSDT step=0.00001 @ 76842.01 → 最小 0.00007 BTC = 5.3789 USDT
        """
        for symbol in ("BTCUSDT", "BNBUSDT"):
            with self.subTest(symbol=symbol):
                f = FILTERS[symbol]
                qty, err = plan_buy_quantity(
                    PRICE[symbol], self.p.notional_ceiling, f.min_notional, f.step_size, f.min_qty
                )
                self.assertIsNone(qty)
                self.assertIn("quantized_above_budget", err)

        # CAKE 的步长够细，5.00 目标仍然可成交
        f = FILTERS["CAKEUSDT"]
        cake_qty, cake_err = plan_buy_quantity(
            PRICE["CAKEUSDT"], self.p.notional_ceiling, f.min_notional, f.step_size, f.min_qty
        )
        self.assertIsNone(cake_err)
        self.assertEqual(cake_qty, D("2.72"))

    def test_notional_below_exchange_min(self):
        d = assess(intent(qty="2.71", notional="4.99724"), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("below_exchange_min_notional") for v in d.violations))

    def test_qty_off_step_rejected(self):
        d = assess(intent(qty="2.725", notional="5.02490"), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("base_qty_off_step") for v in d.violations))

    def test_qty_below_min_rejected(self):
        d = assess(intent("CAKEUSDT", qty="0.005", notional="5.0"), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("base_qty_below_min") for v in d.violations))

    def test_risk_budget(self):
        # 5.0184 USDT × 6% = 0.3011 > 0.30
        d = assess(intent(qty="2.72", notional="5.0184", stop_distance_bps=600),
                   state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("risk_budget_exceeded") for v in d.violations))
        # 5.0184 × 5.97% = 0.29960 ≤ 0.30，不超即通过
        d2 = assess(intent(qty="2.72", notional="5.0184", stop_distance_bps=597),
                    state(), GOOD_PERMS, self.p)
        self.assertTrue(d2.approved, d2.violations)
        self.assertEqual(d2.computed_risk_usdt, D("0.2996"))

    def test_sell_is_exempt_from_entry_side_limits(self):
        """2026-11-03 实盘：把 minNotional / ceiling / 熔断套在 SELL 上，
        导致持仓 3 小时无法退出（18 次重试全失败）。**退出优先于任何资金上限。**

        下面每一条都单独构造出"按买入标准会被拒"的卖出单，断言它仍然放行。
        """
        # a) 名义本金低于交易所下限（卖出落到底部时会发生）
        low = intent(qty="2.68", notional="4.94192", side=Side.SELL,
                     stop_distance_bps=0)
        d = assess(low, state(), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)

        # b) 名义本金高于单笔上限（价格上涨后卖出时会发生）
        high = intent(qty="2.74", notional="5.05256", side=Side.SELL,
                      stop_distance_bps=0)
        d = assess(high, state(), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)

        # c) 熔断已触发 + 已满仓：仍然必须放行卖出
        d = assess(intent(qty="2.72", notional="5.0184", side=Side.SELL,
                          stop_distance_bps=0),
                   state(realized_loss_usdt=D("1.50"), open_positions=1),
                   GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)

    def test_sell_still_obeys_structural_checks(self):
        """放行退出 ≠ 无脑放行。结构性检查对卖出同样有效。"""
        # 非白名单标的
        bad = intent(side=Side.SELL, stop_distance_bps=0)
        object.__setattr__(bad, "symbol", "DOGEUSDT")
        self.assertFalse(assess(bad, state(), GOOD_PERMS, self.p).approved)
        # 步长不对
        off = intent(qty="2.725", notional="5.0249", side=Side.SELL, stop_distance_bps=0)
        self.assertFalse(assess(off, state(), GOOD_PERMS, self.p).approved)
        # 提币权红线
        unsafe = AccountPermissions(True, False, False, ("SPOT",), self._key(enable_withdrawals=True))
        self.assertFalse(
            assess(intent(side=Side.SELL, stop_distance_bps=0), state(), unsafe, self.p).approved
        )

    def test_circuit_breaker_blocks_everything(self):
        d = assess(intent(), state(realized_loss_usdt=D("1.50")), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("circuit_breaker_tripped") for v in d.violations))
        d2 = assess(intent(), state(realized_loss_usdt=D("1.49")), GOOD_PERMS, self.p)
        self.assertTrue(d2.approved, d2.violations)

    def test_second_concurrent_position_rejected(self):
        d = assess(intent(), state(open_positions=1), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("max_open_positions_reached") for v in d.violations))

    def test_sell_not_blocked_by_position_count(self):
        d = assess(intent(side=Side.SELL), state(open_positions=1), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)

    def test_non_spot_instruments_rejected(self):
        for inst in (Instrument.MARGIN, Instrument.ISOLATED_MARGIN, Instrument.FUTURES,
                     Instrument.LEVERAGED_TOKEN, Instrument.WITHDRAW, Instrument.TRANSFER):
            with self.subTest(inst=inst):
                d = assess(intent(instrument=inst), state(), GOOD_PERMS, self.p)
                self.assertFalse(d.approved)
                self.assertTrue(any(v.startswith("instrument_not_allowed") for v in d.violations))

    def test_slippage_and_fee_caps(self):
        self.assertFalse(assess(intent(slippage_bps=301), state(), GOOD_PERMS, self.p).approved)
        self.assertFalse(assess(intent(fee_bps=21), state(), GOOD_PERMS, self.p).approved)
        self.assertTrue(assess(intent(slippage_bps=300, fee_bps=20), state(), GOOD_PERMS, self.p).approved)

    def test_notional_must_be_consistent_with_qty_times_price(self):
        d = assess(intent(qty="2.72", notional="7.00"), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("notional_inconsistent") for v in d.violations))

    def test_filters_symbol_mismatch(self):
        d = assess(intent(filters=FILTERS["BTCUSDT"]), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("filters_symbol_mismatch") for v in d.violations))

    def test_raise_if_rejected_raises(self):
        from src.risk import RiskViolation

        with self.assertRaises(RiskViolation):
            assess(intent(qty="55", notional="101"), state(), GOOD_PERMS, self.p).raise_if_rejected()


class TestExecutionGate(unittest.TestCase):
    def setUp(self):
        self.p = load_policy()

    def test_real_orders_need_the_env_unlock(self):
        gate = execution_gate(self.p, environ={})
        self.assertFalse(gate.may_submit)
        self.assertTrue(gate.dry_run)
        self.assertIn("env TRADER_LIVE_ORDERS not set", gate.reasons)
        # dry_run 不再是拒绝理由，它选择路径（/order/test）
        self.assertFalse(any("dry_run" in r for r in gate.reasons))

    def test_env_unlock_alone_is_not_enough(self):
        gate = execution_gate(self.p, environ={"TRADER_LIVE_ORDERS": "1"})
        self.assertFalse(gate.may_submit)   # dry_run 仍为 true

    def test_live_orders_require_env_and_dry_run_off(self):
        raw = load_policy().raw
        raw["execution"]["dry_run"] = False
        gate = execution_gate(Policy(raw), environ={"TRADER_LIVE_ORDERS": "1"})
        self.assertTrue(gate.may_submit, gate.reasons)

        # 只关 dry_run 而没有环境解锁 -> 仍然是关的
        gate2 = execution_gate(Policy(raw), environ={})
        self.assertFalse(gate2.may_submit)

    def test_computed_risk_formula(self):
        self.assertEqual(computed_risk_usdt(intent(stop_distance_bps=1000)), D("0.5016"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
