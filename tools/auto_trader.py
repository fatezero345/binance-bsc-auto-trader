#!/usr/bin/env python3
"""全自动交易循环（多标的，单进程常驻）。

一次 tick：
  1. 对齐状态与账户（每个标的）；
  2. **先判离场**（止损 / 止盈 / 超时）—— 平仓优先，把仓位和资金腾出来；
  3. 再判入场：白名单里未持仓的标的、冷却已过、入场闸放行、还有仓位与现金。

为什么先离场后入场：组合上限只有 3 仓，平仓才腾出仓位；反过来会出现
"想买新的但仓位满了"而白白错过。

凭据：``~/.binance-sandbox/live.json``，其中 ``"live": true`` 是真实下单的
显式开关。删文件或改 false，脚本只检查不下单。

用法：
    python tools/auto_trader.py --once           # 跑一个 tick
    python tools/auto_trader.py --loop           # 常驻（计划任务/看门狗用它）
    python tools/auto_trader.py --status         # 看持仓
    python tools/auto_trader.py --flatten        # 市价清掉所有持仓
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# pythonw.exe（无控制台）下 sys.stdout/stderr 是 None，print 会直接抛异常。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

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
LOCK = ROOT / "runs" / "tick.lock"
LOCK_STALE_S = 240

STOP_BPS = 500               # -5%
TP_BPS = 600                 # +6%
MAX_HOLD_S = 4 * 3600
COOLDOWN_S = 900
FAIL_BACKOFF_S = 1800
RATE_LIMIT_SLEEP_S = 1800    # 撞到 429/418 就停半小时，绝不继续请求        # 下单失败后，该标的 30 分钟内不再重试
                             # 2026-11-04 实测：BNBUSDT 连续 -1013 失败 82 次，
                             # 每 5 分钟一次，纯粹在烧请求权重。


# ------------------------------------------------------------------ 基础
def log(kind: str, **kw) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, **kw}
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        print(json.dumps(rec, ensure_ascii=False))
    except Exception:                                          # noqa: BLE001
        pass


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


def migrate(st: dict) -> dict:
    """v1 {'position': {...}, 'closed_ts': int} -> v2 多标的结构。

    改成多标的之前只有一个仓位，字段是单数。就地升级，否则下一次 tick
    会把已有持仓当成"没有仓位"，再买一笔。
    """
    if "positions" not in st:
        positions: dict[str, dict] = {}
        closed: dict[str, int] = {}
        single = st.get("position")
        if single:
            positions[single["symbol"]] = single
            if st.get("closed_ts"):
                closed[single["symbol"]] = int(st["closed_ts"])
        st = {"version": 2, "positions": positions, "closed_ts": closed}
    st.setdefault("version", 2)
    st.setdefault("positions", {})
    st.setdefault("closed_ts", {})
    return st


def load_creds() -> tuple[str, str, bool]:
    if not CRED.exists():
        raise SystemExit(f"credentials file missing: {CRED}")
    d = json.loads(CRED.read_text(encoding="utf-8"))
    return d["api_key"], d["api_secret"], bool(d.get("live", False))


def price_of(symbol: str) -> float:
    return float(bp.last_price(symbol))


def acquire_lock() -> bool:
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


# ------------------------------------------------------------------ 对账
def reconcile(key: str, secret: str, st: dict, symbols: list[str]) -> dict:
    """状态文件 vs 账户真实余额，逐标的对齐。"""
    try:
        acct = wa.get_account(key, secret)
        filters = bp.symbol_filters(symbols)
    except Exception as exc:                                   # noqa: BLE001
        log("reconcile_error", error=str(exc)[:200])
        return st

    changed = False
    for sym in symbols:
        base = filters[sym].base_asset
        # ★ 2026-11-03 踩过的坑：balances(acct) 不带 assets 过滤时**只返回部分资产**
        #   （实测只给了 BNB/USDT，把 CAKE 漏了），于是这里读到 0，
        #   把真实持仓当成"账户已空"删掉 —— 币还在，状态没了。
        #   必须逐标的显式查询，且"读不到"与"余额为零"是两件事。
        try:
            free = Decimal(wa.balances(acct, assets=(base,)).get(base, "0"))
        except Exception as exc:                               # noqa: BLE001
            log("reconcile_balance_unknown", symbol=sym, error=str(exc)[:160])
            continue
        dust = filters[sym].step_size
        has = sym in st["positions"]
        if has and free < dust:
            log("reconcile", symbol=sym, note="state had a position but account is flat")
            st["positions"].pop(sym, None)
            st["closed_ts"][sym] = int(time.time())
            changed = True
        elif (not has) and free >= filters[sym].min_qty:
            log("reconcile", symbol=sym, note="untracked balance", free=str(free))
            st.setdefault("untracked", {})[sym] = str(free)
            changed = True
    if changed:
        save_state(st)
    return st


# ------------------------------------------------------------------ 下单
def _position_from(symbol: str, res: dict, fallback_qty: str, fallback_price: float) -> Position:
    filled = float(res.get("executedQty") or fallback_qty)
    fills = res.get("fills") or []
    entry = float(fills[0].get("price", fallback_price)) if fills else float(fallback_price)
    return Position(symbol=symbol, qty=str(filled), entry_price=entry,
                    stop_price=entry * (1 - STOP_BPS / 10_000),
                    tp_price=entry * (1 + TP_BPS / 10_000),
                    opened_ts=int(time.time()))


def _confirm(key: str, secret: str, exc: OrderStateUnknown, side: str) -> dict | None:
    try:
        o = wa.query_order(key, secret, symbol=exc.symbol, client_order_id=exc.client_order_id)
    except Exception as e:                                     # noqa: BLE001
        msg = str(e)
        if "-2013" in msg:
            # 交易所明确说"没有这笔订单" —— 这是**确定性结论**，不是查询失败。
            # 2026-11-03 23:15 真实发生过：响应丢了，回查证明单子根本没到。
            log("order_not_found", side=side, symbol=exc.symbol, cid=exc.client_order_id)
            return None
        log("order_confirm_failed", side=side, cid=exc.client_order_id, error=msg[:200])
        return None
    log("order_confirmed", side=side, symbol=exc.symbol, cid=exc.client_order_id,
        status=o.get("status"), executed=o.get("executedQty"))
    return o if o.get("status") in ("FILLED", "PARTIALLY_FILLED") else None


def _live(policy: Policy) -> tuple[Policy, object]:
    raw = policy.raw
    raw["execution"]["dry_run"] = False
    lp = Policy(raw)
    return lp, execution_gate(lp, environ={"TRADER_LIVE_ORDERS": "1"})


def do_buy(symbol: str, policy: Policy, key: str, secret: str, live: bool,
           free_usdt: Decimal, st: dict) -> Decimal:
    """尝试开一仓。返回剩余可用 USDT。"""
    try:
        kl = bp._request("/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 96})
        v = entry_gate(closes_from_klines(kl))
    except Exception as exc:                                   # noqa: BLE001
        log("gate_error", symbol=symbol, error=str(exc)[:160])
        return free_usdt
    if not v.allow:
        log("skip", symbol=symbol, reason=v.reason, dev=round(v.dev_ma, 6),
            sigma=round(v.sigma_15m, 6))
        return free_usdt

    try:
        snap = bp.snapshot(symbol)
    except Exception as exc:                                   # noqa: BLE001
        log("market_error", symbol=symbol, error=str(exc)[:160])
        return free_usdt
    qty, err = plan_buy_quantity(snap.price, policy.notional_ceiling,
                                 snap.filters.min_notional, snap.filters.step_size,
                                 snap.filters.min_qty)
    if qty is None:
        log("plan_rejected", symbol=symbol, error=err)
        return free_usdt

    notional = (qty * snap.price).quantize(Decimal("0.0001"))
    intent = OrderIntent(venue=Venue.BINANCE_SPOT, symbol=symbol, side=Side.BUY,
                         instrument=Instrument.SPOT, quote_notional_usdt=notional,
                         base_qty=qty, reference_price=snap.price,
                         stop_distance_bps=STOP_BPS, slippage_bps=50, fee_bps=10,
                         filters=snap.filters)
    state = PortfolioState(equity_usdt=free_usdt, realized_loss_usdt=Decimal("0"),
                           open_positions=len(st["positions"]), quote_free_usdt=free_usdt)
    perms = wa.full_permissions(key, secret)
    d = assess(intent, state, perms, policy)
    if not d.approved:
        log("risk_rejected", symbol=symbol, violations=list(d.violations))
        return free_usdt

    if not live:
        log("would_buy", symbol=symbol, qty=str(qty), notional=str(notional),
            price=str(snap.price), note="live=false in credentials file")
        return free_usdt

    lp, gate = _live(policy)
    if not gate.may_submit:
        log("gate_closed", reasons=list(gate.reasons))
        return free_usdt
    try:
        res = submit_order(intent, d, gate, lp, perms, key, secret)
    except OrderStateUnknown as exc:
        log("order_state_unknown", side="BUY", symbol=symbol, cid=exc.client_order_id)
        res = _confirm(key, secret, exc, "BUY")
        if not res:
            return free_usdt
    except ExecutionRefused as exc:
        st.setdefault("fail_until", {})[symbol] = int(time.time()) + FAIL_BACKOFF_S
        save_state(st)
        log("order_failed", side="BUY", symbol=symbol, error=str(exc)[:300],
            qty=str(qty), price=str(snap.price), notional=str(notional),
            backoff_s=FAIL_BACKOFF_S)
        return free_usdt

    pos = _position_from(symbol, res, str(qty), snap.price)
    st["positions"][symbol] = pos.as_dict()
    st["closed_ts"].pop(symbol, None)
    save_state(st)
    log("bought", symbol=symbol, qty=pos.qty, entry=pos.entry_price,
        stop=round(pos.stop_price, 6), tp=round(pos.tp_price, 6))
    return free_usdt - notional


def do_exit(policy: Policy, key: str, secret: str, live: bool, sym: str,
            pos: "Position | None", why: str, st: dict) -> None:
    """平仓。按**真实可用余额**卖出（手续费从基础币扣，不能用 pos.qty）。"""
    try:
        snap = bp.snapshot(sym)
        acct = wa.get_account(key, secret)
        base = snap.filters.base_asset
        free_base = Decimal(wa.balances(acct, assets=(base,)).get(base, "0"))
    except Exception as exc:                                   # noqa: BLE001
        log("exit_balance_error", symbol=sym, error=str(exc)[:200])
        return
    qty = quantize_down(free_base, snap.filters.step_size)
    if qty <= 0:
        st["positions"].pop(sym, None)
        st["closed_ts"][sym] = int(time.time())
        save_state(st)
        log("flat_no_balance", symbol=sym, why=why)
        return

    # 2026-11-04 实盘：残量落到 minNotional 以下是**卖不掉的**。
    # BNB 持有 0.00793447，按 step 0.001 向下 = 0.007 -> 0.007*693.81 = 4.86 < 5
    # 卖 0.007 被 NOTIONAL 拒，卖 0.008 又超过持仓 —— 死循环 82 次，最后把 IP 打进 418。
    # 正确做法：认赔这笔灰尘，记进账本并从持仓里移除，**不要每 5 分钟重试一次**。
    sellable = qty * snap.price
    if sellable < snap.filters.min_notional:
        st.setdefault("dust", {})[sym] = {"qty": str(qty), "value_usdt": str(sellable)}
        st["positions"].pop(sym, None)
        st["closed_ts"][sym] = int(time.time())
        save_state(st)
        log("dust_unsellable", symbol=sym, qty=str(qty), value_usdt=str(sellable),
            min_notional=str(snap.filters.min_notional),
            note="residual below exchange minimum; written off instead of retrying forever")
        return

    intent = OrderIntent(venue=Venue.BINANCE_SPOT, symbol=sym, side=Side.SELL,
                         instrument=Instrument.SPOT,
                         quote_notional_usdt=(qty * snap.price).quantize(Decimal("0.0001")),
                         base_qty=qty, reference_price=snap.price,
                         stop_distance_bps=0, slippage_bps=50, fee_bps=10,
                         filters=snap.filters)
    if not live:
        log("would_sell", symbol=sym, qty=str(qty), why=why, price=str(snap.price))
        return

    lp, gate = _live(policy)
    perms = wa.full_permissions(key, secret)
    free_usdt = Decimal(wa.balances(acct).get("USDT", "0"))
    state = PortfolioState(equity_usdt=free_usdt, realized_loss_usdt=Decimal("0"),
                           open_positions=len(st["positions"]), quote_free_usdt=free_usdt)
    d = assess(intent, state, perms, lp)
    if not d.approved:
        log("sell_blocked", symbol=sym, violations=list(d.violations), qty=str(qty))
        return
    try:
        res = submit_order(intent, d, gate, lp, perms, key, secret)
    except OrderStateUnknown as exc:
        log("order_state_unknown", side="SELL", symbol=sym, cid=exc.client_order_id)
        res = _confirm(key, secret, exc, "SELL")
        if not res:
            return
    except ExecutionRefused as exc:
        log("order_failed", side="SELL", symbol=sym, error=str(exc)[:300], qty=str(qty))
        return

    if pos is None:
        # 灰尘回收：没有可靠的建仓成本，不编 pnl
        st.get("dust", {}).pop(sym, None)
        save_state(st)
        log("dust_recovered", symbol=sym, qty=str(qty), exit=float(snap.price),
            proceeds_usdt=str((qty * snap.price).quantize(Decimal("0.0001"))))
        return
    pnl = (float(snap.price) - pos.entry_price) * float(qty)
    st["positions"].pop(sym, None)
    st["closed_ts"][sym] = int(time.time())
    save_state(st)
    log("sold", symbol=sym, qty=str(qty), exit=float(snap.price), why=why,
        pnl_usdt=round(pnl, 6))


# ------------------------------------------------------------------ 主循环
def tick() -> int:
    if not acquire_lock():
        log("locked", note="another tick is running; skipping")
        return 0
    try:
        return _tick_locked()
    finally:
        release_lock()


def _tick_locked() -> int:
    key, secret, live = load_creds()
    policy = load_policy()
    symbols = list(policy.symbols)
    st = migrate(load_state())
    st = reconcile(key, secret, st, symbols)

    try:
        acct = wa.get_account(key, secret)
        free_usdt = Decimal(wa.balances(acct).get("USDT", "0"))
    except Exception as exc:                                   # noqa: BLE001
        log("tick_error", error=f"balance: {exc}"[:200])
        return 1

    now = int(time.time())

    # 1) 离场优先
    for sym in list(st["positions"]):
        pos = Position.from_dict(st["positions"][sym])
        try:
            price = price_of(sym)
        except Exception as exc:                               # noqa: BLE001
            log("price_error", symbol=sym, error=str(exc)[:120])
            continue
        why = exit_decision(pos, price, now, MAX_HOLD_S)
        log("hold", symbol=sym, price=price, entry=pos.entry_price,
            stop=pos.stop_price, tp=pos.tp_price, decision=why)
        if why:
            do_exit(policy, key, secret, live, sym, pos, why, st)

    # 1.5) 灰尘回收：残量涨回 minNotional 以上就卖掉，别白扔
    for sym in list(st.get("dust", {})):
        try:
            snap = bp.snapshot(sym)
            price = Decimal(str(snap.price))
            qty = quantize_down(Decimal(st["dust"][sym]["qty"]), snap.filters.step_size)
            value = qty * price
            if value >= snap.filters.min_notional:
                log("dust_recoverable", symbol=sym, qty=str(qty), value_usdt=str(value))
                do_exit(policy, key, secret, live, sym, None, "dust_recovery", st)
            else:
                log("dust_waiting", symbol=sym, value_usdt=str(value),
                    need=str(snap.filters.min_notional))
        except Exception as exc:                               # noqa: BLE001
            log("dust_error", symbol=sym, error=str(exc)[:160])

    # 2) 入场
    for sym in symbols:
        if sym in st["positions"]:
            continue
        if len(st["positions"]) >= policy.max_open_positions:
            break
        closed = st["closed_ts"].get(sym, 0)
        if closed and (now - closed) < COOLDOWN_S:
            log("cooldown", symbol=sym, remaining_s=int(COOLDOWN_S - (now - closed)))
            continue
        fail_until = st.get("fail_until", {}).get(sym, 0)
        if fail_until > now:
            log("fail_backoff", symbol=sym, remaining_s=int(fail_until - now))
            continue
        if free_usdt < policy.notional_ceiling:
            log("no_cash", symbol=sym, free=str(free_usdt),
                need=str(policy.notional_ceiling))
            break
        free_usdt = do_buy(sym, policy, key, secret, live, free_usdt, st)

    return 0


def status() -> int:
    st = migrate(load_state())
    print(json.dumps(st, ensure_ascii=False, indent=2))
    total = Decimal("0")
    for sym, raw in st["positions"].items():
        pos = Position.from_dict(raw)
        price = price_of(sym)
        pnl = (price - pos.entry_price) * float(pos.qty)
        total += Decimal(str(pnl))
        print(f"{sym}: price={price} entry={pos.entry_price} qty={pos.qty} "
              f"stop={pos.stop_price:.6f} tp={pos.tp_price:.6f} unrealized={pnl:+.6f}")
    if total:
        print(f"total unrealized: {total:+.6f} USDT")
    return 0


def flatten() -> int:
    key, secret, live = load_creds()
    policy = load_policy()
    st = migrate(load_state())
    if not st["positions"]:
        print("no positions")
        return 0
    for sym in list(st["positions"]):
        do_exit(policy, key, secret, live, sym,
                Position.from_dict(st["positions"][sym]), "manual_flatten", st)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--flatten", action="store_true")
    args = ap.parse_args(argv)

    if args.loop:
        log("daemon_start", interval=args.interval, pid=os.getpid())
        while True:
            sleep_s = max(30, args.interval)
            try:
                tick()
            except Exception as exc:                           # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                log("tick_error", error=msg[:300])
                # 2026-11-04：82 次连续失败把 IP 打进了 418 封禁。
                # 限流/封禁时必须**停下来**，而不是按原节奏继续请求。
                if "418" in msg or "429" in msg or "-1003" in msg:
                    sleep_s = RATE_LIMIT_SLEEP_S
                    log("rate_limited", sleep_s=sleep_s,
                        note="backing off instead of hammering the API")
            time.sleep(sleep_s)
    if args.status:
        return status()
    if args.flatten:
        return flatten()
    return tick()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
