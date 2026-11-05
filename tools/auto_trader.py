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
from decimal import ROUND_CEILING, Decimal
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
    quantize_up,  # noqa: E402
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
from src.strategy.kronos_client import KronosClient  # noqa: E402
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
_KRONOS: "KronosClient | None" = None


def kronos() -> "KronosClient | None":
    """按 policy 的 [kronos] 段懒构造。enabled=false 时返回 None。"""
    global _KRONOS
    cfg = load_policy().raw.get("kronos", {}) or {}
    if not cfg.get("enabled", False):
        return None
    if _KRONOS is None:
        _KRONOS = KronosClient(
            url=cfg.get("url", "http://127.0.0.1:8710"),
            timeout=float(cfg.get("timeout_s", 30)),
            lookback=int(cfg.get("lookback", 200)),
            pred_len=int(cfg.get("pred_len", 6)),
            samples=int(cfg.get("samples", 16)),
            min_factor=float(cfg.get("min_factor", 0.25)),
            repeats=int(cfg.get("repeats", 5)),
        )
    return _KRONOS
RATE_LIMIT_SLEEP_S = 1800    # 撞到 429/418 就停半小时，绝不继续请求        # 下单失败后，该标的 30 分钟内不再重试
                             # 2026-11-04 实测：BNBUSDT 连续 -1013 失败 82 次，
                             # 每 5 分钟一次，纯粹在烧请求权重。


# ------------------------------------------------------------------ 基础
_PNL_CACHE: dict = {"key": None, "total": Decimal("0"), "today": Decimal("0"), "day": ""}


def realized_pnl() -> tuple[Decimal, Decimal]:
    """从账本算出 (累计已实现盈亏, 当日已实现盈亏)。

    2026-11-05 发现：PortfolioState 的 realized_loss_usdt 在两处调用点
    都被硬编码成 Decimal("0")，所以 policy 里的熔断判断
        realized_loss_usdt >= max_cumulative_realized_loss_usdt
    **永远不会成立**，配置的 1.50 USDT 上限是一纸空文。

    以账本为唯一真源（state.json 可能被重建，账本不会丢）。
    按 (大小, mtime) 缓存，避免每轮重复解析 1000+ 行。
    """
    global _PNL_CACHE
    try:
        stat = LEDGER.stat()
        stamp = (stat.st_size, int(stat.st_mtime))
    except OSError:
        return Decimal("0"), Decimal("0")

    today = time.strftime("%Y-%m-%d")
    if _PNL_CACHE["key"] == stamp and _PNL_CACHE["day"] == today:
        return _PNL_CACHE["total"], _PNL_CACHE["today"]

    total = Decimal("0")
    today_sum = Decimal("0")
    try:
        with LEDGER.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"sold"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "sold":
                    continue
                v = rec.get("pnl_usdt")
                if v is None:
                    continue
                try:
                    d = Decimal(str(v))
                except Exception:                            # noqa: BLE001
                    continue
                total += d
                if str(rec.get("ts", ""))[:10] == today:
                    today_sum += d
    except OSError:
        return Decimal("0"), Decimal("0")

    _PNL_CACHE = {"key": stamp, "total": total, "today": today_sum, "day": today}
    return total, today_sum


def realized_loss() -> Decimal:
    """累计已实现**亏损**（正数=亏了）。盈利会抵扣，反映净回撤。"""
    total, _ = realized_pnl()
    return max(Decimal("0"), -total)


def daily_loss() -> Decimal:
    _, today = realized_pnl()
    return max(Decimal("0"), -today)


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
        elif (not has) and free >= filters[sym].min_qty and sym not in st.get("dust", {}):
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


def min_executable_notional(price: Decimal, filters) -> Decimal:
    """该标的最小可成交名义金额。

    必须与 plan_buy_quantity 的回退路径**用同一个式子**，否则两边会打架：
        plan_buy_quantity 回退时取 quantize_up(min_notional / price, step)
    2026-11-05 踩过两次：
      1. 一开始自己按"步长×价"推，算出的预算比实际需要少 0.0001 -> 拒单
      2. 改成向上取整后仍不一致，因为 floor 与 min_qty 的取整先后不同
    最终直接复用同一个公式，保证"抬底之后一定能成交"。
    """
    qty = quantize_up(filters.min_notional / price, filters.step_size)
    if qty < filters.min_qty:
        qty = quantize_up(filters.min_qty, filters.step_size)
    return (qty * price).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)


EXIT_RETRY_S = 1800           # 整笔低于下限时的重试间隔
NETWORK_RETRY_MAX = 2          # 一次 tick 因网络失败，短退避重试的上限
NETWORK_RETRY_DELAY_S = 60     # 比 300s 的正常周期短得多

_TRANSIENT_MARKERS = (
    "unreachable", "URLError", "SSL", "EOF", "Timeout", "timed out",
    "Connection", "RemoteDisconnected", "ConnectionReset", "temporarily",
)


def is_transient_network_error(msg: str) -> bool:
    """这条错误是不是"网络抖了一下"而非逻辑错误。

    2026-11-05 实测：08:05/08:11/08:16/09:02 四次 SSL UNEXPECTED_EOF，
    钱包层已经重试 3 次（1s/2s 退避）仍然全挂 —— 说明链路中断持续了数秒。
    这时按 300s 的正常周期等待，等于白丢一个完整周期。
    正确做法：短退避（60s）重试本轮，最多 2 次。
    """
    m = msg or ""
    return any(k in m for k in _TRANSIENT_MARKERS)


def klines_to_bars(kl: list) -> list:
    """币安 klines 数组 -> Kronos 需要的 bar 字典。"""
    from datetime import datetime, timezone

    out = []
    for k in kl:
        out.append({
            "timestamp": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%S"),
            "open": k[1], "high": k[2], "low": k[3], "close": k[4], "volume": k[5],
        })
    return out


def do_buy(symbol: str, policy: Policy, key: str, secret: str, live: bool,
           free_usdt: Decimal, st: dict, budget_usdt: Decimal | None = None) -> Decimal:
    """尝试开一仓。返回剩余可用 USDT。"""
    try:
        kl = bp._request("/api/v3/klines", {"symbol": symbol, "interval": "15m", "limit": 96})
        st_cfg = policy.strategy
        v = entry_gate(
            closes_from_klines(kl),
            dev_sigmas=float(st_cfg.get("entry_dev_sigmas", 1.0)),
            momentum_sigmas=float(st_cfg.get("entry_momentum_sigmas", 2.0)),
        )
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
    budget = budget_usdt if budget_usdt is not None else policy.notional_ceiling

    # ── Kronos 加权：强制作用于每一笔买单 ──────────────────────────────
    # 系数 = prob_up，夹在 [min_factor, 1.0]。只能缩小仓位、不能放大，
    # 因此永远不会突破 notional_ceiling 与风险预算。
    kc = kronos()
    if kc is not None:
        try:
            _kcfg = policy.raw.get("kronos", {}) or {}
            kl4 = bp._request("/api/v3/klines",
                              {"symbol": symbol, "interval": "4h",
                               "limit": int(_kcfg.get("lookback", 200)) + 5})
            kd = kc.decision(symbol, "4h", klines_to_bars(kl4))
            log("kronos", symbol=symbol, **kd.as_dict())
            if kd.ok and kd.size_factor < 1.0:
                before = budget
                reduced = (budget * Decimal(str(kd.size_factor))).quantize(Decimal("0.0001"))
                # 缩小后的预算可能低于交易所的最小可成交量（例如 BTC 一步 = 0.0001
                # = 7.64 USDT）。这时不是"出错"，而是 Kronos 想要一个买不到的尺寸 ——
                # 抬到最小可成交量并如实记账，否则该标的会被永久排除（实测 38 次被拒）。
                min_exec = min_executable_notional(snap.price, snap.filters)
                if reduced < min_exec:
                    log("kronos_floor", symbol=symbol, factor=round(kd.size_factor, 4),
                        reduced=str(reduced), floor=str(min_exec),
                        note="Kronos 的尺寸低于交易所最小可成交量，抬到最小值")
                    budget = min_exec
                else:
                    budget = reduced
                log("kronos_sized", symbol=symbol, before=str(before), after=str(budget),
                    factor=round(kd.size_factor, 4))
        except Exception as exc:                                # noqa: BLE001
            log("kronos_error", symbol=symbol,
                error=f"{type(exc).__name__}: {str(exc)[:140]}")

    qty, err = plan_buy_quantity(snap.price, budget,
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
    state = PortfolioState(equity_usdt=free_usdt, realized_loss_usdt=realized_loss(),
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
    st.get("untracked", {}).pop(symbol, None)
    save_state(st)
    log("bought", symbol=symbol, qty=pos.qty, entry=pos.entry_price,
        stop=round(pos.stop_price, 6), tp=round(pos.tp_price, 6))
    return free_usdt - notional


def dust_sell_qty(free_base: Decimal, recorded_qty: str | None,
                  step: Decimal) -> Decimal:
    """灰尘回收的卖出量。

    2026-11-04 实盘事故：BNB 有 0.007 灰尘 + 0.009 持仓，
    灰尘回收按**全部余额**卖出，把持仓一起卖掉：
        dust_recovered qty=0.009（灰尘只有 0.007）
        -> 12 分钟后 reconcile 报"账户已空"，那笔仓位的盈亏永久丢失。

    所以回收量必须同时满足：不超过账户余额、不超过记录的灰尘数量。
    """
    qty = quantize_down(free_base, step)
    if recorded_qty:
        try:
            cap = quantize_down(Decimal(str(recorded_qty)), step)
            if cap > 0 and qty > cap:
                qty = cap
        except Exception:                                    # noqa: BLE001
            pass
    return qty


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
    if pos is None:
        # 灰尘回收：卖出量封顶在记录的灰尘数量，绝不碰持仓
        rec = (st.get("dust", {}) or {}).get(sym) or {}
        qty = dust_sell_qty(free_base, rec.get("qty"), snap.filters.step_size)
    else:
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
        if pos is not None:
            # 2026-11-05 实盘：一笔 2.48 CAKE 的持仓超时平仓时名义只有 4.94 < 5.00，
            # 被当成"卖不掉的灰尘"写掉并移出持仓表 —— **一笔约 5 USDT 的真实仓位
            # 就这样从账上消失了**。它其实还躺在账户里，下次建仓才被合并。
            # 正确做法：保留仓位，等价格回升到够得着下单门槛再卖。
            # 这里完全不提交订单，所以不会产生 -1013 拒单，也就没有 418 风险；
            # 只加 30 分钟冷却，避免每轮都白读一次行情与余额。
            st.setdefault("exit_cooldown", {})[sym] = int(time.time()) + EXIT_RETRY_S
            save_state(st)
            log("position_below_min_notional", symbol=sym, qty=str(qty),
                value_usdt=str(sellable), min_notional=str(snap.filters.min_notional),
                retry_in_s=EXIT_RETRY_S,
                note="整笔低于交易所下限，保留仓位等价格回升，不写死成灰尘")
            return
        st.setdefault("dust", {})[sym] = {"qty": str(qty), "value_usdt": str(sellable)}
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
    state = PortfolioState(equity_usdt=free_usdt, realized_loss_usdt=realized_loss(),
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
        st.get("untracked", {}).pop(sym, None)   # 回收完就没有未跟踪余额了
        save_state(st)
        log("dust_recovered", symbol=sym, qty=str(qty), exit=float(snap.price),
            proceeds_usdt=str((qty * snap.price).quantize(Decimal("0.0001"))))
        return
    # 盈亏只能按**持仓量**算。卖出量取的是真实余额，里面可能还混着
    # 之前被打成灰尘的同币残量（2026-11-05：CAKE 持仓 2.49 + 灰尘 2.48，
    # 若按 4.97 算盈亏，这一笔的收益会被虚增近一倍）。
    pos_qty = Decimal(str(pos.qty))
    pnl_qty = min(Decimal(str(qty)), pos_qty)
    pnl = (float(snap.price) - pos.entry_price) * float(pnl_qty)
    extra = Decimal(str(qty)) - pnl_qty
    if extra > 0:
        st.get("dust", {}).pop(sym, None)
        st.get("untracked", {}).pop(sym, None)
        log("dust_cleared", symbol=sym, extra_qty=str(extra),
            proceeds_usdt=str((extra * snap.price).quantize(Decimal("0.0001"))),
            note="随持仓一起卖掉的同币残量，未计入本笔盈亏")
    st["positions"].pop(sym, None)
    st["closed_ts"][sym] = int(time.time())
    st.pop("exit_cooldown", {}).pop(sym, None) if isinstance(st.get("exit_cooldown"), dict) else None
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
        # 整笔低于交易所下限时会被放进冷却（见 do_exit），等价格回升再试
        _cool = (st.get("exit_cooldown") or {}).get(sym, 0)
        if _cool and now < _cool:
            log("exit_cooldown", symbol=sym, remaining_s=_cool - now,
                note="整笔低于交易所下限，等价格回升后再卖")
            continue
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
        # 有持仓时绝不能做灰尘回收：do_exit 按全部余额下单，会把仓位一起卖掉。
        # 等仓位平掉之后再回收，灰尘不会消失。
        if sym in st.get("positions", {}):
            log("dust_skip_position", symbol=sym,
                note="该标的有持仓，跳过灰尘回收以免误卖仓位")
            continue
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

    # 1.8) 日内亏损上限：当天亏到阈值就停止开新仓（已有仓位照常平）
    _daily_cap = policy.max_daily_loss
    if _daily_cap > 0:
        _dl = daily_loss()
        if _dl >= _daily_cap:
            log("daily_loss_halt", daily_loss=str(_dl), cap=str(_daily_cap),
                note="当日已实现亏损达到上限，停止开新仓")
            return

    # 2) 入场：按「剩余现金 / 剩余仓位」动态分配，避免前两仓吃满、第三仓买不起
    for sym in symbols:
        slots = policy.max_open_positions - len(st["positions"])
        if slots <= 0:
            break
        if sym in st["positions"]:
            continue
        closed = st["closed_ts"].get(sym, 0)
        if closed and (now - closed) < COOLDOWN_S:
            log("cooldown", symbol=sym, remaining_s=int(COOLDOWN_S - (now - closed)))
            continue
        fail_until = st.get("fail_until", {}).get(sym, 0)
        if fail_until > now:
            log("fail_backoff", symbol=sym, remaining_s=int(fail_until - now))
            continue
        budget = min(policy.notional_ceiling, (free_usdt / slots).quantize(Decimal("0.0001")))
        if budget < policy.min_notional:
            log("no_cash", symbol=sym, free=str(free_usdt), slots=slots, budget=str(budget))
            break
        before_n = len(st["positions"])
        free_usdt = do_buy(sym, policy, key, secret, live, free_usdt, st, budget_usdt=budget)
        if len(st["positions"]) > before_n:
            continue

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
        retries = 0
        while True:
            sleep_s = max(30, args.interval)
            try:
                tick()
                retries = 0
            except Exception as exc:                           # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                log("tick_error", error=msg[:300])
                # 2026-11-04：82 次连续失败把 IP 打进了 418 封禁。
                # 限流/封禁时必须**停下来**，而不是按原节奏继续请求。
                if "418" in msg or "429" in msg or "-1003" in msg:
                    sleep_s = RATE_LIMIT_SLEEP_S
                    retries = 0
                    log("rate_limited", sleep_s=sleep_s,
                        note="backing off instead of hammering the API")
                elif retries < NETWORK_RETRY_MAX and is_transient_network_error(msg):
                    retries += 1
                    sleep_s = NETWORK_RETRY_DELAY_S
                    log("tick_retry", attempt=retries, delay_s=sleep_s,
                        note="网络类错误，短退避后重试本轮而不是等满一个周期")
                else:
                    retries = 0
            time.sleep(sleep_s)
    if args.status:
        return status()
    if args.flatten:
        return flatten()
    return tick()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
