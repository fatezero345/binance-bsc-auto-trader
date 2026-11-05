#!/usr/bin/env python3
"""回测：把生产里那套规则原样搬到历史 15 分钟 K 线上。

规则与 src/strategy 完全一致：
  入场  entry_gate(dev_sigmas=config, momentum_sigmas=config)  —— 用真实配置，不另写一套
  仓位  notional = min(上限, 现金/剩余仓位)，按 stepSize 向下量化，且不低于 minNotional
  离场  -5% 止损 / +6% 止盈 / 4 小时（16 根）超时
  手续费 0.1% 每边
  约束  最多 3 仓、每标的一仓、平仓后冷却 1 根

假设与已知局限（必须写在报告里，不能藏）：
  * 同一根 K 线内同时触及止损与止盈时，**按最坏情况计（先止损）**；
  * 信号在收盘产生，成交取**下一根开盘价**，不使用未来数据；
  * 不模拟滑点（市价单实际会有，但这里没有可靠估计）；
  * 不模拟网络失败、灰尘、量化失败 —— 这些在生产里确实发生过，回测会比实盘乐观。

用法：
    python tools/backtest.py --days 30
    python tools/backtest.py --days 60 --symbols CAKEUSDT,BNBUSDT
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.market import binance_public as bp  # noqa: E402
from src.risk import load_policy, plan_buy_quantity, quantize_down  # noqa: E402
from src.strategy.momentum_gate import entry_gate  # noqa: E402

CACHE = ROOT / "runs" / "backtest"
START_EQUITY = Decimal("28.24316192")
FEE = 0.001                     # 每边 0.1%（float，避免与 Decimal 混算）
STOP_BPS = 500
TP_BPS = 600
MAX_HOLD_BARS = 16              # 4 小时 / 15 分钟
COOLDOWN_BARS = 1
WINDOW = 96                     # entry_gate 需要的历史长度


def fetch(symbol: str, days: int, interval: str = "15m") -> list[list]:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{symbol}_{interval}_{days}d.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    per = {"15m": 96 * 4}.get(interval, 1000)
    need = int(days * 24 * 60 / 15) + WINDOW + 10
    out: list[list] = []
    end = None
    while len(out) < need:
        params: dict = {"symbol": symbol, "interval": interval, "limit": 1000}
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


def simulate(symbol: str, klines: list[list], filters, policy, dev_sigmas: float,
             momentum_sigmas: float, fee_on: bool = True) -> dict:
    """单标的回测。返回成交列表与统计。"""
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    opens = [float(k[1]) for k in klines]

    trades = []
    pos = None
    cooldown_until = -1

    for i in range(WINDOW, len(klines) - 1):
        # ---- 已有仓位：先判离场（用本根 K 线的高低点）----
        if pos is not None:
            hi, lo = highs[i], lows[i]
            exit_reason = None
            exit_price = None
            # 最坏假设：同根内先打止损
            if lo <= pos["stop"]:
                exit_reason, exit_price = "stop", pos["stop"]
            elif hi >= pos["tp"]:
                exit_reason, exit_price = "tp", pos["tp"]
            elif i - pos["bar"] >= MAX_HOLD_BARS:
                exit_reason, exit_price = "max_hold", opens[i]
            if exit_reason:
                gross = (exit_price - pos["entry"]) * pos["qty"]
                fees = (pos["entry"] * pos["qty"] + exit_price * pos["qty"]) * FEE if fee_on else 0.0
                pnl = gross - fees
                trades.append({
                    "symbol": symbol, "entry": pos["entry"], "exit": exit_price,
                    "qty": pos["qty"], "reason": exit_reason,
                    "bars": i - pos["bar"], "pnl": pnl,
                })
                pos = None
                cooldown_until = i + COOLDOWN_BARS
                continue
            continue

        # ---- 空仓：判入场 ----
        if i < cooldown_until:
            continue
        try:
            v = entry_gate(closes[max(0, i - WINDOW + 1):i + 1],
                           dev_sigmas=dev_sigmas, momentum_sigmas=momentum_sigmas)
        except Exception:
            continue
        if not v.allow:
            continue

        entry_price = opens[i + 1]                       # 下一根开盘，不用未来数据
        if entry_price <= 0:
            continue
        qty, err = plan_buy_quantity(Decimal(str(entry_price)), policy.notional_ceiling,
                                     filters.min_notional, filters.step_size, filters.min_qty)
        if qty is None:
            continue
        pos = {
            "entry": entry_price, "qty": float(qty), "bar": i + 1,
            "stop": entry_price * (1 - STOP_BPS / 10_000),
            "tp": entry_price * (1 + TP_BPS / 10_000),
        }

    return {"symbol": symbol, "trades": trades}


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "win": 0, "win_rate": 0.0, "pnl": 0.0, "avg": 0.0,
                "best": 0.0, "worst": 0.0, "by_reason": {}}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    return {
        "n": len(pnls), "win": len(wins), "win_rate": len(wins) / len(pnls) * 100,
        "pnl": sum(pnls), "avg": sum(pnls) / len(pnls),
        "best": max(pnls), "worst": min(pnls), "by_reason": by_reason,
    }


def main(argv: list[str]) -> int:
    global STOP_BPS, TP_BPS, MAX_HOLD_BARS
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--no-fee", action="store_true")
    ap.add_argument("--stop-bps", type=int, default=STOP_BPS)
    ap.add_argument("--tp-bps", type=int, default=TP_BPS)
    ap.add_argument("--hold-bars", type=int, default=MAX_HOLD_BARS)
    args = ap.parse_args(argv)
    STOP_BPS = args.stop_bps
    TP_BPS = args.tp_bps
    MAX_HOLD_BARS = args.hold_bars

    policy = load_policy()
    symbols = (args.symbols.split(",") if args.symbols else sorted(policy.symbols))
    cfg = policy.strategy
    dev = float(cfg.get("entry_dev_sigmas", 1.0))
    mom = float(cfg.get("entry_momentum_sigmas", 2.0))

    print(f"回测：{args.days} 天 15m | 标的 {symbols}")
    print(f"闸门：dev_sigmas={dev} momentum_sigmas={mom}")
    print(f"仓位：上限 {policy.notional_ceiling} | 止损 -{STOP_BPS}bps | "
          f"止盈 +{TP_BPS}bps | 超时 {MAX_HOLD_BARS} 根 | 手续费 {'关' if args.no_fee else '0.1%×2'}")
    print()

    filters = bp.symbol_filters(symbols)
    all_trades: list[dict] = []
    per_symbol = {}

    for sym in symbols:
        kl = fetch(sym, args.days)
        if len(kl) < WINDOW + 10:
            print(f"{sym}: 数据不足（{len(kl)} 根）")
            continue
        r = simulate(sym, kl, filters[sym], policy, dev, mom, fee_on=not args.no_fee)
        per_symbol[sym] = r["trades"]
        all_trades.extend(r["trades"])
        s = stats(r["trades"])
        span = (kl[-1][0] - kl[0][0]) / 86400000
        print(f"{sym}: {len(kl)} 根 ≈ {span:.0f} 天 | 成交 {s['n']:>3} 笔 | "
              f"胜率 {s['win_rate']:>5.1f}% | 净 {s['pnl']:>+8.4f} USDT | "
              f"均 {s['avg']:>+7.4f} | 理由 {s['by_reason']}")

    s = stats(all_trades)
    print()
    print("=" * 62)
    print("  汇总")
    print("=" * 62)
    print(f"成交笔数      {s['n']}")
    print(f"胜率          {s['win_rate']:.1f}%  ({s['win']}/{s['n']})")
    print(f"净盈亏        {s['pnl']:+.4f} USDT")
    print(f"每笔期望      {s['avg']:+.4f} USDT")
    print(f"最好 / 最差   {s['best']:+.4f} / {s['worst']:+.4f}")
    print(f"离场理由      {s['by_reason']}")
    if s["n"]:
        print(f"按 {START_EQUITY} USDT 本金计，收益率 {s['pnl'] / float(START_EQUITY) * 100:+.2f}%")
    print()

    # 基准：等权买入持有
    bh = 0.0
    for sym in symbols:
        kl = fetch(sym, args.days)
        if len(kl) > WINDOW:
            bh += (float(kl[-1][4]) / float(kl[WINDOW][4]) - 1) * 100 / len(symbols)
    print(f"基准（等权买入持有）{bh:+.2f}%")

    out = ROOT / "runs" / "backtest_report.md"
    lines = [
        f"# 回测报告 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 区间：{args.days} 天 15m",
        f"- 闸门：dev_sigmas={dev}，momentum_sigmas={mom}",
        f"- 仓位：上限 {policy.notional_ceiling}，止损 -5%，止盈 +6%，超时 4 小时",
        f"- 手续费：{'关' if args.no_fee else '0.1% 每边'}",
        "",
        "| 标的 | 成交 | 胜率 | 净盈亏 | 每笔期望 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sym, tr in per_symbol.items():
        st = stats(tr)
        lines.append(f"| {sym} | {st['n']} | {st['win_rate']:.1f}% | "
                     f"{st['pnl']:+.4f} | {st['avg']:+.4f} |")
    lines += [
        f"| **合计** | **{s['n']}** | **{s['win_rate']:.1f}%** | "
        f"**{s['pnl']:+.4f}** | **{s['avg']:+.4f}** |",
        "",
        f"基准（等权买入持有）：{bh:+.2f}%",
        "",
        "## 已知局限",
        "",
        "- 同根 K 线内同时触及止损与止盈时按最坏情况计（先止损）",
        "- 不模拟滑点；不模拟网络失败、灰尘、量化失败（生产里这些都发生过）",
        "- 信号在收盘产生，成交取下一根开盘价",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
