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

from src.execution.binance_spot import (  # noqa: E402
    ExecutionRefused,
    OrderStateUnknown,
    submit_order,
)
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
    quantize_down,
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
LOCK = ROOT / "runs" / "tick.lock"
LOCK_STALE_S = 240           # 超过 4 分钟视为僵尸锁（正常 tick 只有几秒）


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


def acquire_lock() -> bool:
    """单实例锁。

    2026-11-03 实测：计划任务每 5 分钟 + 手动运行会重叠，
    111 个事件里有 30 次间隔 < 60s（最短 8s）。两个 tick 同时看到"无持仓"
    就会各买一笔 —— 直接突破 max_open_positions = 1。
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
        except FileNotFoundError:
            age = LOCK_STALE_S + 1
        if age < LOCK_STALE_S:
            return False
        LOCK.unlink(missing_ok=True)
    LOCK.write_text(f"{os.getpid()} {time.time()}", encoding="utf-8")
    return True


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


def _position_from(res: dict, fallback_qty: str, fallback_price: float) -> Position:
    filled = float(res.get("executedQty") or fallback_qty)
    fills = res.get("fills") or []
    entry = float(fills[0].get("price", fallback_price)) if fills else float(fallback_price)
    return Position(symbol=SYMBOL, qty=str(filled), entry_price=entry,
                    stop_price=entry * (1 - STOP_BPS / 10_000),
                    tp_price=entry * (1 + TP_BPS / 10_000),
                    opened_ts=int(time.time()))


def _confirm(key: str, secret: str, exc, side: str) -> dict | None:
    """按 clientOrderId 回查交易所，确认那笔状态未知的订单到底成没成。

    返回已成交的订单 dict；返回 None 表示"没成交"或"查不到"，调用方不得建仓/清仓。
    """
    try:
        o = wa.query_order(key, secret, symbol=exc.symbol or SYMBOL,
                           client_order_id=exc.client_order_id)
    except Exception as e:                                   # noqa: BLE001
        log("order_confirm_failed", side=side, cid=exc.client_order_id, error=str(e)[:200])
        return None
    status = o.get("status")
    log("order_confirmed", side=side, cid=exc.client_order_id, status=status,
        executed=o.get("executedQty"))
    if status in ("FILLED", "PARTIALLY_FILLED"):
        return o
    return None


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
    except OrderStateUnknown as exc:
        # 请求发出后没拿到响应 —— 立刻回查，绝不重试
        log("order_state_unknown", side="BUY", cid=exc.client_order_id, error=str(exc)[:200])
        res = _confirm(key, secret, exc, "BUY")
        if not res:
            return
    except ExecutionRefused as exc:
        log("order_failed", side="BUY", error=str(exc)[:400])
        return

    pos = _position_from(res, str(qty), snap.price)
    save_state({"position": pos.as_dict(), "closed_ts": 0})
    log("bought", symbol=SYMBOL, qty=pos.qty, entry=pos.entry_price,
        stop=round(pos.stop_price, 6), tp=round(pos.tp_price, 6), response=res)


def do_exit(policy: Policy, key: str, secret: str, live: bool, pos: Position, why: str) -> None:
    """平仓。

    2026-11-03 实盘教训（18 次 -2010）：
      1. 不能按 ``pos.qty`` 卖 —— 手续费是从**基础币**里扣的，实际余额 2.71728
         而不是 2.72。多卖 0.00272 会被交易所判成余额不足。
      2. 所以必须读**真实可用余额**，再按 stepSize 向下量化。
    """
    snap = bp.snapshot(SYMBOL)
    acct = wa.get_account(key, secret)
    base = snap.filters.base_asset
    free_base = Decimal(wa.balances(acct, assets=(base,)).get(base, "0"))
    qty = quantize_down(free_base, snap.filters.step_size)

    if qty <= 0:
        # 已经没货了（上一次卖出其实成功了，或人工平掉了）—— 清状态，别卡死
        save_state({"position": None, "closed_ts": int(time.time()),
                    "last_exit": "already_flat"})
        log("flat_no_balance", symbol=SYMBOL, why=why)
        return

    intent = OrderIntent(venue=Venue.BINANCE_SPOT, symbol=SYMBOL, side=Side.SELL,
                         instrument=Instrument.SPOT,
                         quote_notional_usdt=(qty * snap.price).quantize(Decimal("0.0001")),
                         base_qty=qty, reference_price=snap.price,
                         stop_distance_bps=0, slippage_bps=50, fee_bps=10,
                         filters=snap.filters)
    if not live:
        log("would_sell", symbol=SYMBOL, qty=str(qty), why=why, price=str(snap.price))
        return

    raw = policy.raw
    raw["execution"]["dry_run"] = False
    live_policy = Policy(raw)
    gate = execution_gate(live_policy, environ={"TRADER_LIVE_ORDERS": "1"})
    perms = wa.full_permissions(key, secret)
    free_usdt = Decimal(wa.balances(acct).get("USDT", "0"))
    st = PortfolioState(equity_usdt=free_usdt, realized_loss_usdt=Decimal("0"),
                        open_positions=1, quote_free_usdt=free_usdt)
    d = assess(intent, st, perms, live_policy)
    if not d.approved:
        log("sell_blocked", violations=list(d.violations), qty=str(qty), price=str(snap.price))
        return
    try:
        res = submit_order(intent, d, gate, live_policy, perms, key, secret)
    except OrderStateUnknown as exc:
        log("order_state_unknown", side="SELL", cid=exc.client_order_id, error=str(exc)[:200])
        res = _confirm(key, secret, exc, "SELL")
        if not res:
            return
    except ExecutionRefused as exc:
        log("order_failed", side="SELL", error=str(exc)[:400], qty=str(qty))
        return
    pnl = (snap.price - pos.entry_price) * float(qty)
    save_state({"position": None, "closed_ts": int(time.time()),
                "last_exit": why, "last_pnl_usdt": round(pnl, 6)})
    log("sold", symbol=SYMBOL, qty=str(qty), exit=float(snap.price), why=why,
        pnl_usdt=round(pnl, 6), response=res)


def reconcile(key: str, secret: str, st: dict) -> dict:
    """把状态文件与账户真实余额对齐。

    2026-11-03 的第四个 bug：一笔卖单**已经在交易所成交**，但 HTTP 响应在 TLS 层丢了，
    本地什么都没记。下一 tick 靠"余额为 0"猜出已经平仓 —— 猜对了是运气。
    「状态未知」是这类系统最危险的失败模式：钱动了没有，不知道。

    这一步不能修复"响应丢失"，但能保证**每个 tick 开始时，状态不会长期偏离账户**。
    真正的修复需要按 clientOrderId 查 /api/v3/order 做幂等确认（留给 P3）。
    """
    try:
        snap = bp.snapshot(SYMBOL)
        acct = wa.get_account(key, secret)
        base = snap.filters.base_asset
        free = Decimal(wa.balances(acct, assets=(base,)).get(base, "0"))
    except Exception as exc:                                   # noqa: BLE001
        log("reconcile_error", error=str(exc)[:200])
        return st

    has_pos = bool(st.get("position"))
    dust = snap.filters.step_size                              # 小于一步长 = 灰尘
    if has_pos and free < dust:
        log("reconcile", note="state had a position but the account is flat -> mark flat")
        st = {"position": None, "closed_ts": int(time.time()), "last_exit": "reconciled_flat"}
        save_state(st)
    elif (not has_pos) and free >= snap.filters.min_qty:
        log("reconcile", note="untracked balance: the account holds more than the state knows",
            free=str(free))
        st["untracked_balance"] = str(free)
        save_state(st)
    return st


def tick() -> int:
    if not acquire_lock():
        log("locked", note="another tick is running; skipping this one")
        return 0
    try:
        return _tick_locked()
    finally:
        release_lock()


def _tick_locked() -> int:
    key, secret, live = load_creds()
    policy = load_policy()
    st = reconcile(key, secret, load_state())
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
