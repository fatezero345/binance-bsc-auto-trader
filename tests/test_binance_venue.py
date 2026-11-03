"""交易场所层的验收测试：执行闸门、只读边界、过滤器解析。

全部离线：不发起任何网络请求，不接触任何凭据。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.execution.binance_spot import (  # noqa: E402
    ORDER_PATH,
    ExecutionRefused,
    build_order_params,
    submit_order,
)
from src.market.binance_public import parse_filters  # noqa: E402
from src.risk import (  # noqa: E402
    AccountPermissions,
    KeyFlags,
    Instrument,
    OrderIntent,
    Policy,
    Side,
    SymbolFilters,
    Venue,
    assess,
    execution_gate,
    load_policy,
)
from src.wallet import binance_account  # noqa: E402
from src.wallet.binance_account import (  # noqa: E402
    READ_ONLY_PATHS,
    CredentialsMissing,
    WalletError,
    load_credentials,
    permissions_from_account,
)

D = Decimal
CAKE = SymbolFilters("CAKEUSDT", "CAKE", "USDT", D("0.01"), D("0.01"), D("0.001"), D("5"))
SAFE_KEY = KeyFlags(
    enable_reading=True, enable_spot_trading=True, enable_withdrawals=False,
    enable_internal_transfer=False, enable_futures=False, enable_margin=False,
    enable_vanilla_options=False, permits_universal_transfer=False,
    enable_portfolio_margin=False, ip_restrict=True,
)

def _unsafe_withdraw() -> KeyFlags:
    """same key but with the withdrawal flag left open."""
    return KeyFlags(
        enable_reading=True, enable_spot_trading=True, enable_withdrawals=True,
        enable_internal_transfer=False, enable_futures=False, enable_margin=False,
        enable_vanilla_options=False, permits_universal_transfer=False,
        enable_portfolio_margin=False, ip_restrict=True,
    )


GOOD = AccountPermissions(True, False, False, ("SPOT",), SAFE_KEY)
BAD_WITHDRAW = AccountPermissions(True, True, True, ("SPOT",), _unsafe_withdraw())


def make_intent() -> OrderIntent:
    return OrderIntent(
        venue=Venue.BINANCE_SPOT,
        symbol="CAKEUSDT",
        side=Side.BUY,
        instrument=Instrument.SPOT,
        quote_notional_usdt=D("5.01568"),
        base_qty=D("2.72"),
        reference_price=D("1.844"),
        stop_distance_bps=500,
        slippage_bps=50,
        fee_bps=10,
        filters=CAKE,
    )


def open_gate(mainnet: bool = False):
    raw = load_policy().raw
    raw["execution"]["enabled"] = True
    raw["execution"]["dry_run"] = False
    raw["execution"]["allow_mainnet"] = mainnet
    policy = Policy(raw)
    return policy, execution_gate(policy, environ={"TRADER_LIVE_ORDERS": "1"})


class TestExecutionBoundary(unittest.TestCase):
    def test_only_order_path_is_exposed(self):
        self.assertEqual(ORDER_PATH, "/api/v3/order")

    def test_no_withdraw_or_transfer_path_in_execution_source(self):
        import src.execution.binance_spot as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        for bad in ("capital/withdraw", "asset/transfer", "universalTransfer"):
            self.assertNotIn(bad, src, f"execution module must not reference {bad}")

    def test_closed_gate_never_submits(self):
        policy = load_policy()
        gate = execution_gate(policy, environ={})
        decision = assess(make_intent(), _state(), GOOD, policy)
        plan = build_order_params(make_intent(), policy, gate, GOOD, decision)
        self.assertFalse(plan.would_submit)
        self.assertTrue(plan.reasons)

    def test_submit_raises_when_gate_closed(self):
        policy = load_policy()
        gate = execution_gate(policy, environ={})
        decision = assess(make_intent(), _state(), GOOD, policy)
        with self.assertRaises(ExecutionRefused):
            submit_order(make_intent(), decision, gate, policy, GOOD, "k", "s")

    def test_submit_raises_when_risk_rejected(self):
        policy = load_policy()
        _p, gate = open_gate()
        decision = assess(make_intent(), _state(), BAD_WITHDRAW, policy)   # 因提币权被拒
        self.assertFalse(decision.approved)
        with self.assertRaises(ExecutionRefused):
            submit_order(make_intent(), decision, gate, policy, BAD_WITHDRAW, "k", "s")

    def test_testnet_is_default_target(self):
        policy, gate = open_gate()
        plan = build_order_params(make_intent(), policy, gate, GOOD)
        self.assertTrue(plan.would_submit, plan.reasons)
        self.assertEqual(plan.base_url, policy.testnet_base_url)
        self.assertNotIn("api.binance.com", plan.base_url)

    def test_mainnet_requires_fourth_switch(self):
        policy, gate = open_gate(mainnet=True)
        plan = build_order_params(make_intent(), policy, gate, GOOD)
        self.assertTrue(plan.would_submit, plan.reasons)
        self.assertEqual(plan.base_url, policy.base_url)

    def test_withdraw_permission_blocks_even_with_open_gate(self):
        policy, gate = open_gate(mainnet=True)
        plan = build_order_params(make_intent(), policy, gate, BAD_WITHDRAW)
        self.assertFalse(plan.would_submit)
        self.assertIn("key_flag_dangerous:enable_withdrawals", plan.reasons)

    def test_off_step_quantity_blocked_at_build(self):
        policy, gate = open_gate()
        bad = make_intent()
        object.__setattr__(bad, "base_qty", D("2.725"))
        object.__setattr__(bad, "quote_notional_usdt", D("5.02490"))
        plan = build_order_params(bad, policy, gate, GOOD)
        self.assertFalse(plan.would_submit)
        self.assertTrue(any(r.startswith("qty_not_on_step") for r in plan.reasons))

    def test_plan_params_shape(self):
        policy, gate = open_gate()
        plan = build_order_params(make_intent(), policy, gate, GOOD)
        self.assertEqual(plan.params["symbol"], "CAKEUSDT")
        self.assertEqual(plan.params["side"], "BUY")
        self.assertEqual(plan.params["type"], "MARKET")
        self.assertEqual(plan.params["quantity"], "2.72")


class TestWalletReadOnly(unittest.TestCase):
    def test_order_path_is_not_read_only(self):
        self.assertNotIn("/api/v3/order", READ_ONLY_PATHS)

    def test_no_private_path_in_whitelist(self):
        for p in READ_ONLY_PATHS:
            self.assertFalse(p.startswith("/sapi/v1/capital/withdraw"))
            self.assertFalse("transfer" in p)

    def test_read_rejects_non_whitelisted_path_without_network(self):
        with self.assertRaises(WalletError):
            binance_account.read("/api/v3/order", {}, api_key="k", api_secret="s")

    def test_credentials_missing_is_explicit(self):
        with self.assertRaises(CredentialsMissing):
            load_credentials({})
        with self.assertRaises(CredentialsMissing):
            load_credentials({"BINANCE_API_KEY": "only-key"})

    def test_credentials_present(self):
        k, s = load_credentials({"BINANCE_API_KEY": "abc", "BINANCE_API_SECRET": "def"})
        self.assertEqual((k, s), ("abc", "def"))

    def test_permissions_mapping(self):
        acct = {
            "canTrade": True,
            "canWithdraw": False,
            "canDeposit": False,
            "permissions": ["SPOT"],
            "balances": [{"asset": "USDT", "free": "29.30"}, {"asset": "BNB", "free": "0.0"}],
        }
        perms = permissions_from_account(acct)
        self.assertTrue(perms.can_trade)
        self.assertFalse(perms.can_withdraw)
        self.assertEqual(perms.permissions, ("SPOT",))
        self.assertEqual(binance_account.balances(acct), {"USDT": "29.30", "BNB": "0.0"})

    def test_balances_filters_unrelated_assets(self):
        acct = {"balances": [{"asset": "USDT", "free": "1"}, {"asset": "SCAM", "free": "999"}]}
        self.assertEqual(binance_account.balances(acct), {"USDT": "1"})


class TestFilterParsing(unittest.TestCase):
    """用 2026-11-03 抓到的 exchangeInfo 片段做离线解析测试。"""

    FIXTURE = {
        "symbols": [
            {
                "symbol": "CAKEUSDT",
                "baseAsset": "CAKE",
                "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "minPrice": "0.00100000",
                     "maxPrice": "1000.00000000", "tickSize": "0.00100000"},
                    {"filterType": "LOT_SIZE", "minQty": "0.01000000",
                     "maxQty": "900000.00000000", "stepSize": "0.01000000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000",
                     "applyMinToMarket": True, "maxNotional": "9000000.00000000"},
                ],
            },
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                    # 旧名 MIN_NOTIONAL 也要兼容
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"},
                    {"filterType": "LOT_SIZE", "minQty": "0.00001000",
                     "stepSize": "0.00001000"},
                ],
            },
        ]
    }

    def test_parses_both_notional_filter_names(self):
        f = parse_filters(self.FIXTURE)
        self.assertEqual(f["CAKEUSDT"].min_notional, D("5.00000000"))
        self.assertEqual(f["BTCUSDT"].min_notional, D("5.00000000"))

    def test_parses_step_and_tick(self):
        f = parse_filters(self.FIXTURE)
        self.assertEqual(f["CAKEUSDT"].step_size, D("0.01000000"))
        self.assertEqual(f["CAKEUSDT"].tick_size, D("0.00100000"))
        self.assertEqual(f["BTCUSDT"].step_size, D("0.00001000"))

    def test_quote_asset_survives(self):
        f = parse_filters(self.FIXTURE)
        self.assertEqual(f["CAKEUSDT"].quote_asset, "USDT")


def _state():
    from src.risk import PortfolioState

    return PortfolioState(equity_usdt=D("29.30"), realized_loss_usdt=D("0"), open_positions=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
