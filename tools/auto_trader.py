#!/usr/bin/env python3
"""全自动交易循环（每次调用跑一个 tick，由计划任务每 N 分钟拉起）。

一次 tick 做三件事之一：
  1. 有持仓 -> 判离场（止损 / 止盈 / 超时）-> 卖；或不动。
  2. 无持仓 -> 跑入场闸 -> ALLOW 且能组单 -> 买。
  3. 其它情况什么都不做，只写一行 log。

凭据：从 ``~/.binance-sandbox/live.json`` 读取（ACL 只给当前用户）。
该文件里的 ``"live": true`` 是"允许真实下单"的显式开关 —— 缺它或为 false，
本脚本只做检查、不发单。**这是替代环境变量的那一环**：计划任务拿不到交互式环境，
所以把开关放进一个可删除、可审计的文件里。

用法：
    python tools/auto_trader.py --once          # 跑一个 tick
    python tools/auto_trader.py --status        # 看当前持仓与状态
    python tools/auto_trader.py --flatten       # 市价平掉全部持仓（人工干预用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.execution.binance_spot import ExecutionRefused, submit_order  # noqa: E402
from src.market import binance_public as bp  # noqa: E402
from src.risk import (  # noqa: E402
    Instrument,
    OrderIntent,
    Policy,
    PortfolioState,
    Side,
    Venue,
    assess,
    execution_gate,
    load_policy,
    plan_buy_quantity,
)
from src.strategy.exit_rules import Position, exit_decision  # noqa: E402
from src.strategy.momentum_gate import closes_from_klines, entry_gate  # noqa: E402
from src.wallet import binance_account as wa  # noqa: E402

HOME = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
CRED = HOME / ".binance-sandbox" / "live.json"
STATE = ROOT / "runs" / "state.json"
LEDGER = ROOT / "runs" / "ledger.jsonl"

SYMBOL = "CAKEUSDT"          # 5.05 上限下唯一可成交标的（见 docs/07 R14）
STOP_BPS = 500               # -5%
TP_BPS = 600                 # +6%（覆盖来回 0.2% 手续费后仍有余量）
MAX_HOLD_S = 4 * 3600        # 4 小时未触发则市价退出
COOLDOWN_S = 900             # 平仓后 15 分钟内不重新开仓


def log(kind: str, **kw) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, **kw}
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def load_creds() -> tuple[str, str, bool]:
    if not CRED.exists():
        raise SystemExit(f"credentials file missing: {CRED}")
    d = json.loads(CRED.read_text(encoding="utf-8"))
    return d["api_key"], d["api_secret"], bool(d.get("live", False))


def price_of(symbol: str) -> float:
    return float(bp.last_price(symbol))


def do_buy(policy: Policy, key: str, secret: str, live: bool) -> None:
    kl = bp._request("/api/v3/klines", {"symbol": SYMBOL, "interval": "15m", "limit": 96})
    v = entry_gate(closes_from_klines(kl))
    if not v.allow:
        log("skip", symbol=SYMBOL, reason=v.reason, dev=round(v.dev_ma, 6),
            sigma=round(v.sigma_15m, 6))
        return

    snap = bp.snapshot(SYMBOL)
    qty, err = plan_buy_quantity(snap.price, policy.notional_ceiling,
                                 snap.filters.min_notional, snap.filters.step_size,
                                 snap.filters.min_qty)
    if qty is None:
        log("plan_rejected", symbol=SYMBOL, error=err)
        return

    notional = (qty * snap.price).quantize(Decimal("0.0001"))
    acct = wa.get_account(key, secret)
    free = Decimal(wa.balances(acct).get("USDT", "0"))
    intent = OrderIntent(venue=Venue.BINANCE_SPOT, symbol=SYMBOL, side=Side.BUY,
                         instrument=Instrument.SPOT, quote_notional_usdt=notional,
                         base_qty=qty, reference_price=snap.price,
                         stop_distance_bps=STOP_BPS, slippage_bps=50, fee_bps=10,
                         filters=snap.filters)
    st = PortfolioState(equity_usdt=free, realized_loss_usdt=Decimal("0"),
                        open_positions=0, quote_free_usdt=free)
    perms = wa.full_permissions(key, secret)
    d = assess(intent, st, perms, policy)
    if not d.approved:
        log("risk_rejected", symbol=SYMBOL, violations=list(d.violations))
        return

    if not live:
        log("would_buy", symbol=SYMBOL, qty=str(qty), notional=str(notional),
            price=str(snap.price), note="live=false in credentials file")
        return

    raw = policy.raw
    raw["execution"]["dry_run"] = False
    live_policy = Policy(raw)
    gate = execution_gate(live_policy, environ={"TRADER_LIVE_ORDERS": "1"})
    if not gate.may_submit:
        log("gate_closed", reasons=list(gate.reasons))
        return
    try:
        res = submit_order(intent, d, gate, live_policy, perms, key, secret)
    except ExecutionRefused as exc:
        log("order_failed", side="BUY", error=str(exc)[:400])
        return

    filled = float(res.get("executedQty") or qty)
    entry = float(res.get("fills", [{}])[0].get("price", snap.price)) if res.get("fills") else float(snap.price)
    pos = Position(symbol=SYMBOL, qty=str(filled), entry_price=entry,
                   stop_price=entry * (1 - STOP_BPS / 10_000),
                   tp_price=entry * (1 + TP_BPS / 10_000),
                   opened_ts=int(time.time()))
    save_state({"position": pos.as_dict(), "closed_ts": 0})
    log("bought", symbol=SYMBOL, qty=str(filled), entry=entry,
        stop=round(pos.stop_price, 6), tp=round(pos.tp_price, 6), response=res)


def do_exit(policy: Policy, key: str, secret: str, live: bool, pos: Position, why: str) -> None:
    snap = bp.snapshot(SYMBOL)
    intent = OrderIntent(venue=Venue.BINANCE_SPOT, symbol=SYMBOL, side=Side.SELL,
                         instrument=Instrument.SPOT,
                         quote_notional_usdt=(Decimal(pos.qty) * snap.price).quantize(Decimal("0.0001")),
                         base_qty=Decimal(pos.qty), reference_price=snap.price,
                         stop_distance_bps=0, slippage_bps=50, fee_bps=10,
                         filters=snap.filters)
    if not live:
        log("would_sell", symbol=SYMBOL, qty=pos.qty, why=why, price=str(snap.price))
        return

    raw = policy.raw
    raw["execution"]["dry_run"] = False
    live_policy = Policy(raw)
    gate = execution_gate(live_policy, environ={"TRADER_LIVE_ORDERS": "1"})
    perms = wa.full_permissions(key, secret)
    acct = wa.get_account(key, secret)
    free = Decimal(wa.balances(acct).get("USDT", "0"))
    st = PortfolioState(equity_usdt=free, realized_loss_usdt=Decimal("0"),
                        open_positions=1, quote_free_usdt=free)
    d = assess(intent, st, perms, live_policy)
    # 卖出方向不受持仓数约束，但仍走同一条风控（符号白名单/过滤器/上限）
    if not d.approved:
        log("sell_blocked", violations=list(d.violations))
        return
    try:
        res = submit_order(intent, d, gate, live_policy, perms, key, secret)
    except ExecutionRefused as exc:
        log("order_failed", side="SELL", error=str(exc)[:400])
        return
    pnl = (snap.price - pos.entry_price) * float(pos.qty)
    save_state({"position": None, "closed_ts": int(time.time()),
                "last_exit": why, "last_pnl_usdt": round(pnl, 6)})
    log("sold", symbol=SYMBOL, qty=pos.qty, exit=float(snap.price), why=why,
        pnl_usdt=round(pnl, 6), response=res)


def tick() -> int:
    key, secret, live = load_creds()
    policy = load_policy()
    st = load_state()
    raw_pos = st.get("position")

    if raw_pos:
        pos = Position.from_dict(raw_pos)
        price = price_of(SYMBOL)
        why = exit_decision(pos, price, int(time.time()), MAX_HOLD_S)
        log("hold", symbol=SYMBOL, price=price, entry=pos.entry_price,
            stop=pos.stop_price, tp=pos.tp_price, decision=why)
        if why:
            do_exit(policy, key, secret, live, pos, why)
        return 0

    if st.get("closed_ts") and (time.time() - st["closed_ts"]) < COOLDOWN_S:
        log("cooldown", remaining_s=int(COOLDOWN_S - (time.time() - st["closed_ts"])))
        return 0

    do_buy(policy, key, secret, live)
    return 0


def status() -> int:
    st = load_state()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if st.get("position"):
        pos = Position.from_dict(st["position"])
        price = price_of(SYMBOL)
        print(f"current price {price} | entry {pos.entry_price} | "
              f"unrealized {(price-pos.entry_price)*float(pos.qty):+.6f} USDT")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--flatten", action="store_true")
    args = ap.parse_args(argv)

    if args.status:
        return status()
    if args.flatten:
        key, secret, live = load_creds()
        st = load_state()
        if not st.get("position"):
            print("no position")
            return 0
        do_exit(load_policy(), key, secret, live,
                Position.from_dict(st["position"]), "manual_flatten")
        return 0
    return tick()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
