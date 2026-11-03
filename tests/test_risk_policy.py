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

GOOD_PERMS = AccountPermissions(can_trade=True, can_withdraw=False, can_deposit=False, permissions=("SPOT",))


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
        self.assertEqual(p.max_notional, D("10.00"))
        self.assertEqual(p.min_notional, D("5.00"))
        self.assertEqual(p.max_risk_budget, D("0.30"))
        self.assertEqual(p.max_cumulative_loss, D("1.50"))
        self.assertEqual(p.max_open_positions, 1)

    def test_execution_ships_closed(self):
        p = load_policy()
        self.assertFalse(p.execution["enabled"])
        self.assertTrue(p.execution["dry_run"])
        self.assertFalse(p.execution["allow_mainnet"])
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

    def test_plan_buy_rejects_degenerate_inputs(self):
        self.assertIsNotNone(plan_buy_quantity(D("0"), D("10"), D("5"), D("0.01"), D("0.01"))[1])
        self.assertIsNotNone(plan_buy_quantity(PRICE["CAKEUSDT"], D("0"), D("5"), D("0.01"), D("0.01"))[1])


class TestRiskGate(unittest.TestCase):
    def setUp(self):
        self.p = load_policy()

    def test_happy_path_approved(self):
        d = assess(intent(), state(), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)

    def test_account_withdraw_enabled_is_rejected(self):
        """ADR-0002 的核心：提币权必须关闭，否则整个 CEX 方案不成立。"""
        perms = AccountPermissions(True, True, True, ("SPOT",))
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertIn("account_can_withdraw_must_be_false", d.violations)

    def test_account_cannot_trade_is_rejected(self):
        perms = AccountPermissions(False, False, False, ("SPOT",))
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertIn("account_cannot_trade", d.violations)

    def test_extra_permissions_are_rejected(self):
        perms = AccountPermissions(True, False, False, ("SPOT", "MARGIN"))
        d = assess(intent(), state(), perms, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("account_permissions_not_subset") for v in d.violations))

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

    def test_notional_above_cap(self):
        d = assess(intent(qty="5.42", notional="9.99448", stop_distance_bps=300),
                   state(), GOOD_PERMS, self.p)
        self.assertTrue(d.approved, d.violations)          # 9.99 合法
        d2 = assess(intent(qty="5.50", notional="10.142", stop_distance_bps=300),
                    state(), GOOD_PERMS, self.p)
        self.assertFalse(d2.approved)
        self.assertTrue(any(v.startswith("notional_exceeds_cap") for v in d2.violations))

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
        # 10 USDT × 5% = 0.50 > 0.30
        d = assess(intent(qty="5.42", notional="9.99448", stop_distance_bps=500), state(), GOOD_PERMS, self.p)
        self.assertFalse(d.approved)
        self.assertTrue(any(v.startswith("risk_budget_exceeded") for v in d.violations))
        # 10 USDT × 3% = 0.30 == 上限，不超即通过
        d2 = assess(intent(qty="5.42", notional="9.99448", stop_distance_bps=300), state(), GOOD_PERMS, self.p)
        self.assertTrue(d2.approved, d2.violations)
        self.assertEqual(d2.computed_risk_usdt, D("0.2998"))

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

    def test_default_is_closed_and_mainnet_refused(self):
        gate = execution_gate(self.p, environ={})
        self.assertFalse(gate.may_submit)
        self.assertFalse(gate.allow_mainnet)
        self.assertIn("policy.execution.enabled=false", gate.reasons)
        self.assertIn("policy.execution.dry_run=true", gate.reasons)

    def test_env_unlock_alone_is_not_enough(self):
        gate = execution_gate(self.p, environ={"TRADER_LIVE_ORDERS": "1"})
        self.assertFalse(gate.may_submit)

    def test_all_three_switches_required(self):
        raw = load_policy().raw
        raw["execution"]["enabled"] = True
        raw["execution"]["dry_run"] = False
        gate = execution_gate(Policy(raw), environ={"TRADER_LIVE_ORDERS": "1"})
        self.assertTrue(gate.may_submit, gate.reasons)
        self.assertFalse(gate.allow_mainnet)      # 第四道门仍然关着

    def test_computed_risk_formula(self):
        self.assertEqual(computed_risk_usdt(intent(stop_distance_bps=1000)), D("0.5016"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
