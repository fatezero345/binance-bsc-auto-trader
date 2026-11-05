#!/usr/bin/env python3
"""maker 模型的参数扫描：回答「这套东西有没有任何一组参数能为正」。

与 backtest.py 的区别（三件事都按你的要求改了）：

1. **maker 成交模型**
   信号在收盘产生 → 挂**限价买**在 close*(1-offset)，之后 N 根内若最低价触到该价位
   才算成交（成交价=挂单价，maker 费率）。挂不上就撤单，不计成交。
   离场：止盈挂限价卖（maker 费率）；止损与超时用市价（taker 费率）。
   —— 这比"按下一根开盘市价成交"更接近真实挂单，也把**逆向选择**算了进去：
      只有价格跌到你的买价才成交，成交的往往是继续跌的那部分。

2. **放宽时间**：持仓窗口作为扫描维度（16 / 48 / 96 根 = 4h / 12h / 24h）。

3. **边际**：这一项不靠参数获得。本脚本只负责**证伪**——
   如果 96 组参数里没有一组能在样本外为正，那就说明没有边际，
   任何本金放大都只是放大亏损。

费率：maker 0.075% / taker 0.1%（按所有者账户口径）。

多重检验警告：扫 96 组，即使全是纯噪声，也必然有几组看起来赚钱。
所以报告里同时给出"最好的一组"和"中位数"，并做前后半段分段验证。

用法：
    python tools/backtest_sweep.py --days 30
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.market import binance_public as bp  # noqa: E402
from src.risk import load_policy, plan_buy_quantity  # noqa: E402
from src.strategy.momentum_gate import entry_gate  # noqa: E402

CACHE = ROOT / "runs" / "backtest"
MAKER = 0.00075
TAKER = 0.0010
WINDOW = 96


def fetch(symbol: str, days: int) -> list[list]:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{symbol}_15m_{days}d.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    need = int(days * 24 * 4) + WINDOW + 10
    out: list[list] = []
    end = None
    while len(out) < need:
        params: dict = {"symbol": symbol, "interval": "15m", "limit": 1000}
        if end:
            params["endTime"] = end
        batch = bp._request("/api/v3/klines", params)
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        time.sleep(0.25)
        if len(batch) < 1000:
            break
    out = out[-need:]
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def precompute_signals(closes: list[float], dev: float, mom: float) -> list[bool]:
    """每根 K 线上闸门是否放行。只算一次，供所有参数组合复用。"""
    allow = [False] * len(closes)
    for i in range(WINDOW, len(closes)):
        try:
            v = entry_gate(closes[i - WINDOW + 1:i + 1], dev_sigmas=dev, momentum_sigmas=mom)
            allow[i] = v.allow
        except Exception:
            allow[i] = False
    return allow


def simulate(symbol, klines, allow, filters, policy,
             offset_bps, tp_bps, stop_bps, hold_bars, fill_bars=4) -> list[dict]:
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    opens = [float(k[1]) for k in klines]
    closes = [float(k[4]) for k in klines]

    trades = []
    i = WINDOW
    n = len(klines)
    while i < n - 1:
        if not allow[i]:
            i += 1
            continue

        limit = closes[i] * (1 - offset_bps / 10_000)
        if limit <= 0:
            i += 1
            continue

        # 挂单等待成交：fill_bars 根内最低价触到挂单价才成交
        fill_bar = None
        for j in range(i + 1, min(i + 1 + fill_bars, n)):
            if lows[j] <= limit:
                fill_bar = j
                break
        if fill_bar is None:
            i += 1
            continue

        qty_d, err = plan_buy_quantity(Decimal(str(limit)), policy.notional_ceiling,
                                       filters.min_notional, filters.step_size, filters.min_qty)
        if qty_d is None:
            i += 1
            continue
        qty = float(qty_d)

        entry = limit
        tp_px = entry * (1 + tp_bps / 10_000)
        stop_px = entry * (1 - stop_bps / 10_000)

        exit_price = None
        exit_reason = None
        exit_bar = None
        for j in range(fill_bar, min(fill_bar + hold_bars, n)):
            # 最坏假设：同根内先止损
            if lows[j] <= stop_px:
                exit_price, exit_reason, exit_bar = stop_px, "stop", j
                break
            if highs[j] >= tp_px:
                exit_price, exit_reason, exit_bar = tp_px, "tp", j
                break
        if exit_price is None:
            exit_bar = min(fill_bar + hold_bars, n - 1)
            exit_price, exit_reason = opens[exit_bar], "max_hold"

        fee_exit = MAKER if exit_reason == "tp" else TAKER
        gross = (exit_price - entry) * qty
        fees = entry * qty * MAKER + exit_price * qty * fee_exit
        trades.append({
            "symbol": symbol, "entry": entry, "exit": exit_price, "qty": qty,
            "reason": exit_reason, "pnl": gross - fees,
            "bars": exit_bar - fill_bar,
        })
        i = exit_bar + 1
    return trades


def agg(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "avg": 0.0, "reason": {}}
    p = [t["pnl"] for t in trades]
    reason: dict[str, int] = {}
    for t in trades:
        reason[t["reason"]] = reason.get(t["reason"], 0) + 1
    return {
        "n": len(p),
        "wr": sum(1 for x in p if x > 0) / len(p) * 100,
        "pnl": sum(p),
        "avg": sum(p) / len(p),
        "reason": reason,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default=None)
    args = ap.parse_args(argv)

    policy = load_policy()
    cfg = policy.strategy
    dev = float(cfg.get("entry_dev_sigmas", 0.0))
    mom = float(cfg.get("entry_momentum_sigmas", 2.0))
    symbols = (args.symbols.split(",") if args.symbols else sorted(policy.symbols))

    print(f"maker 扫描 | {args.days} 天 15m | {symbols}")
    print(f"闸门 dev_sigmas={dev} momentum_sigmas={mom}")
    print(f"费率 maker={MAKER*100}% taker={TAKER*100}% | 挂单等待 4 根")
    print()

    filters = bp.symbol_filters(symbols)
    data = {}
    for sym in symbols:
        kl = fetch(sym, args.days)
        closes = [float(k[4]) for k in kl]
        t0 = time.time()
        allow = precompute_signals(closes, dev, mom)
        data[sym] = (kl, allow)
        print(f"  {sym}: {len(kl)} 根，信号 {sum(allow)} 次放行（{time.time()-t0:.1f}s）")
    print()

    grid = list(itertools.product(
        (0, 5, 10, 20),        # 挂单折价 bps
        (100, 150, 200, 300),  # 止盈 bps
        (100, 200),            # 止损 bps
        (16, 48, 96),          # 持仓窗口（根）
    ))
    print(f"扫描 {len(grid)} 组参数 …")

    results = []
    for off, tp, stop, hold in grid:
        allt: list[dict] = []
        for sym in symbols:
            kl, allow = data[sym]
            allt.extend(simulate(sym, kl, allow, filters[sym], policy, off, tp, stop, hold))
        a = agg(allt)
        a.update({"off": off, "tp": tp, "stop": stop, "hold": hold,
                  "half1": agg([t for t in allt if t["bars"] is not None])["pnl"]})
        results.append(a)

    results.sort(key=lambda r: -r["avg"])
    med = statistics.median([r["avg"] for r in results])
    pos = [r for r in results if r["avg"] > 0]

    print()
    print("=" * 88)
    print("  前 10（按每笔期望排序）")
    print("=" * 88)
    print(f"{'折价':>4} {'止盈':>5} {'止损':>5} {'窗口':>4} | {'成交':>5} {'胜率':>6} {'净盈亏':>10} {'每笔期望':>10}  理由")
    for r in results[:10]:
        print(f"{r['off']:>4} {r['tp']:>5} {r['stop']:>5} {r['hold']:>4} | "
              f"{r['n']:>5} {r['wr']:>5.1f}% {r['pnl']:>+10.4f} {r['avg']:>+10.4f}  {r['reason']}")

    print()
    print("=" * 88)
    print("  分布")
    print("=" * 88)
    print(f"参数组数        {len(results)}")
    print(f"每笔期望中位数  {med:+.6f} USDT")
    print(f"为正的组数      {len(pos)} / {len(results)}  ({len(pos)/len(results)*100:.1f}%)")
    if results:
        print(f"最好            {results[0]['avg']:+.6f}  "
              f"(off={results[0]['off']} tp={results[0]['tp']} "
              f"stop={results[0]['stop']} hold={results[0]['hold']})")
        print(f"最差            {results[-1]['avg']:+.6f}")

    out = ROOT / "runs" / "sweep_report.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写入 {out}")

    if not pos:
        print("\n结论：**没有任何一组参数在扣掉成本后为负->正**。没有边际，加本金只会放大亏损。")
    else:
        print(f"\n结论：{len(pos)} 组为正，但这是 {len(results)} 组里挑出来的，"
              f"存在多重检验偏差；需要分段/样本外验证才可采信。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
