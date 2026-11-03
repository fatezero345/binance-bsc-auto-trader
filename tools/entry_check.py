#!/usr/bin/env python3
"""入场信号检查（**不需要 API Key**，只用公开行情）。

设计上刻意只用公开端点：盯盘本身不该需要凭据。真正下单才需要 Key。

用法：
    python tools/entry_check.py            # 检查白名单全部标的
    python tools/entry_check.py --symbol CAKEUSDT --interval 15m

输出每一项都包含：
  * 判定（ALLOW / SKIP）与理由；
  * 距离翻转还有多远（价格要走多少个百分点）；
  * 若 ALLOW，会下的那笔单是什么样。
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.market import binance_public as bp  # noqa: E402
from src.risk import load_policy, plan_buy_quantity  # noqa: E402
from src.strategy.momentum_gate import closes_from_klines, entry_gate  # noqa: E402


def check(symbol: str, interval: str, limit: int, policy) -> dict:
    kl = bp._request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    closes = closes_from_klines(kl)
    v = entry_gate(closes)
    f = bp.symbol_filters([symbol])[symbol]
    price = Decimal(str(v.price))

    # 距离翻转：需要 price 涨到 ma*(1 - tolerance) 之上
    flip_level = v.ma20 * (1 - v.tolerance)
    need_pct = (flip_level - v.price) / v.price * 100

    qty, err = plan_buy_quantity(price, policy.notional_ceiling, f.min_notional,
                                 f.step_size, f.min_qty)
    out = {
        "symbol": symbol,
        "allow": v.allow,
        "reason": v.reason,
        "price": v.price,
        "ma20": v.ma20,
        "sigma": v.sigma_15m,
        "dev_pct": v.dev_ma * 100,
        "chg_1h_pct": v.chg_1h * 100,
        "tolerance_pct": v.tolerance * 100,
        "flip_level": flip_level,
        "need_pct": need_pct,
        "qty": str(qty) if qty else None,
        "notional": str((qty * price).quantize(Decimal("0.0001"))) if qty else None,
        "plan_error": err,
        "whitelisted": symbol in policy.symbols,
    }
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--limit", type=int, default=96)
    args = ap.parse_args(argv)

    policy = load_policy()
    symbols = [args.symbol] if args.symbol else sorted(policy.symbols | policy.observer_symbols)

    print(f"entry gate @ {args.interval} x{args.limit}  (thresholds derived from sigma, not constants)")
    print()
    any_allow = False
    for sym in symbols:
        try:
            r = check(sym, args.interval, args.limit, policy)
        except Exception as exc:                      # noqa: BLE001
            print(f"{sym:<10} ERROR  {exc}")
            continue
        mark = "ALLOW" if r["allow"] else "SKIP "
        wl = "" if r["whitelisted"] else "  (observer only)"
        print(f"{sym:<10} {mark}  price={r['price']:.6f}  ma20={r['ma20']:.6f} "
              f"dev={r['dev_pct']:+.3f}%  sigma={r['sigma']*100:.3f}%  1h={r['chg_1h_pct']:+.3f}%{wl}")
        if r["allow"]:
            any_allow = True
            if r["qty"]:
                print(f"{'':10}       -> BUY {r['qty']} @ ~{r['price']:.6f} = {r['notional']} USDT")
            else:
                print(f"{'':10}       -> 但下不进去：{r['plan_error']}")
        else:
            print(f"{'':10}       -> 需涨到 {r['flip_level']:.6f}（{r['need_pct']:+.3f}%）才翻")
        print()

    print("结论：", "有标的通过" if any_allow else "全部 SKIP —— 不买")
    print("注：本检查只用公开行情，不需要 API Key，也不会下任何单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
