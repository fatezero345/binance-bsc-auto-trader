#!/usr/bin/env python3
"""Binance 现货场所探针（只读，无需鉴权）。

回答三个问题：
  1. 场所通不通（服务器时间 + 时钟偏差）；
  2. 三条白名单交易对的过滤器是什么（stepSize / minNotional / tickSize）；
3. **在当前业务窗口下（目标 5.00 / 硬上限 5.05 / 交易所下限 5.00），每个交易对能不能真的下出一单**
     —— 这一条靠 plan_buy_quantity 实算，不靠印象。

用法：
    python tools/binance_probe.py            # 打印
    python tools/binance_probe.py --write    # 同时刷新 docs/evidence/raw/binance_*.json

只读：不触碰账户、不签名、不下单。
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
from src.risk import load_policy, plan_buy_quantity  # noqa: E402

RAW = ROOT / "docs" / "evidence" / "raw"


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 68 - len(title)))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    policy = load_policy()
    base = policy.base_url
    symbols = sorted(policy.symbols | policy.observer_symbols)

    section(f"connectivity ({base})")
    skew = bp.clock_skew_ms(int(time.time() * 1000), base=base)
    print(f"server_time reachable, clock skew = {skew} ms (limit {policy.raw['venue']['max_clock_skew_ms']})")
    if abs(skew) > policy.raw["venue"]["max_clock_skew_ms"]:
        print("WARNING: clock skew exceeds the policy limit; signed requests would be rejected")

    section("exchange filters + 24h liquidity")
    filters = bp.symbol_filters(symbols, base=base)
    tickers = bp.ticker_24h(symbols, base=base)
    rows = []
    for s in symbols:
        f = filters[s]
        t = tickers[s]
        rows.append(
            {
                "symbol": s,
                "status": t.get("symbol") and "TRADING",
                "price": t["lastPrice"],
                "quoteVolume24h_M": round(float(t["quoteVolume"]) / 1e6, 1),
                "stepSize": str(f.step_size),
                "minQty": str(f.min_qty),
                "tickSize": str(f.tick_size),
                "minNotional": str(f.min_notional),
                "whitelisted": s in policy.symbols,
            }
        )
    width = max(len(r["symbol"]) for r in rows)
    hdr = f"{'symbol':<{width}}  {'price':>12}  {'24hVol(M)':>9}  {'step':>11}  {'minNot':>7}  {'wl':>2}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['symbol']:<{width}}  {r['price']:>12}  {r['quoteVolume24h_M']:>9}  "
            f"{r['stepSize']:>11}  {r['minNotional']:>7}  {'Y' if r['whitelisted'] else 'n':>2}"
        )

    section("feasibility: can we actually place one order?")
    print(
        f"equity = {policy.raw['capital']['initial_equity_usdt']} USDT | "
        f"per-order target {policy.max_notional} USDT, ceiling {policy.notional_ceiling}, "
        f"exchange floor {policy.min_notional}"
    )
    window = policy.notional_ceiling - policy.min_notional
    print(f"  quantisation window width = {window:.4f} USDT "
          f"(ceiling - exchange floor)")
    print(f"  nominal step = stepSize x price ; a symbol is RELIABLY tradeable only if "
          f"nominal step <= window")
    print()
    feasible = 0
    for s in sorted(policy.symbols):
        f = filters[s]
        price = Decimal(tickers[s]["lastPrice"])
        nominal_step = f.step_size * price
        qty, err = plan_buy_quantity(
            price, policy.notional_ceiling, f.min_notional, f.step_size, f.min_qty
        )
        alignment = "reliable" if nominal_step <= window else "price-alignment-only"
        if err:
            print(f"  {s:<10} NO  nominal_step={nominal_step:.4f}  {alignment}\n"
                  f"             {err}")
            continue
        notional = qty * price
        util = (notional / Decimal(policy.raw["capital"]["initial_equity_usdt"])) * 100
        print(
            f"  {s:<10} OK  nominal_step={nominal_step:.4f}  {alignment}\n"
            f"             qty={qty} @ {price} = {notional:.4f} USDT ({util:.1f}% of equity)"
        )
        feasible += 1

    if feasible == 0:
        print("\n  !! no whitelisted symbol can be traded inside the current window")
    else:
        print(
            f"\n  {feasible}/{len(policy.symbols)} whitelisted symbols can place an order "
            f"inside the {policy.min_notional}-{policy.notional_ceiling} USDT window"
        )

    section("execution state")
    print(f"policy.execution.enabled   = {policy.execution['enabled']}")
    print(f"policy.execution.dry_run   = {policy.execution['dry_run']}")
    print(f"policy.execution.allow_mainnet = {policy.execution['allow_mainnet']}")
    print("no order was attempted; this probe is read-only")

    if args.write:
        RAW.mkdir(parents=True, exist_ok=True)
        targets = {
            "binance_exchange_info.json": {
                "symbols": [
                    {
                        "symbol": s,
                        "baseAsset": filters[s].base_asset,
                        "quoteAsset": filters[s].quote_asset,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": str(filters[s].tick_size)},
                            {"filterType": "LOT_SIZE", "minQty": str(filters[s].min_qty),
                             "stepSize": str(filters[s].step_size)},
                            {"filterType": "NOTIONAL", "minNotional": str(filters[s].min_notional)},
                        ],
                    }
                    for s in symbols
                ]
            },
            "binance_ticker_24hr.json": tickers,
        }
        for name, payload in targets.items():
            dest = RAW / name
            if dest.exists() and not args.force:
                print(f"kept   {name} (exists; --force to replace)")
                continue
            dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
