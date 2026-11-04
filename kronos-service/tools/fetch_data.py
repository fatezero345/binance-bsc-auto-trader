#!/usr/bin/env python3
"""拉取 OHLCV 并落盘，供推理与评估共用。

标的与口径（重要，必须记录）：
  * 任务书要求 BTCB/WBNB、CAKE/USDT、BNB。
  * BTCB/WBNB 是 PancakeSwap 上的 BSC 交易对，CEX 没有直接对应；
    这里用 Binance 的 BTCUSDT 作为**价格代理**，并在报告里注明。
  * CAKE/USDT -> CAKEUSDT，BNB -> BNBUSDT，均为 Binance 现货。
  * volume 是**交易所成交量**，不是 DEX 成交量。两者口径不同：
    DEX 的 volume 是该交易对池子内的换手，CEX 是全市场撮合；
    同一标的的 CEX 量通常高一个数量级，且没有"池子深度"概念。
    Kronos 的 volume 特征因此不能跨口径泛化，评估结论也只对本口径成立。

未收盘 K 线一律丢弃：最后一根如果 `open_time + interval > now`，直接不取。

用法：
    python tools/fetch_data.py --symbols BTCUSDT,CAKEUSDT,BNBUSDT --intervals 4h,1d --limit 1000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.binance.com"
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
               "1d": 86_400_000, "1w": 604_800_000}


def _get(path: str, params: dict) -> list:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "kronos-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch(symbol: str, interval: str, limit: int) -> list[dict]:
    step = INTERVAL_MS[interval]
    now = int(time.time() * 1000)

    # 从"上一根已收盘"往回翻页
    end = (now // step) * step - 1
    out: list[dict] = []
    while len(out) < limit:
        batch = _get("/api/v3/klines", {
            "symbol": symbol, "interval": interval,
            "limit": min(1000, limit - len(out)), "endTime": end,
        })
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        time.sleep(0.15)
        if len(batch) < 1000:
            # 已经到最早一根
            if len(out) < limit:
                break

    rows = []
    dropped = 0
    for k in out[-limit:]:
        open_time = int(k[0])
        if open_time + step > now:          # 未收盘，丢弃
            dropped += 1
            continue
        rows.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(open_time / 1000)),
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
            "amount": float(k[7]),          # quoteAssetVolume，可作为 amount
        })
    rows.sort(key=lambda r: r["timestamp"])
    if dropped:
        print(f"    (dropped {dropped} unclosed bar(s))")
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,CAKEUSDT,BNBUSDT")
    ap.add_argument("--intervals", default="4h,1d")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args(argv)

    os.makedirs(DATA, exist_ok=True)
    for sym in args.symbols.split(","):
        for iv in args.intervals.split(","):
            rows = fetch(sym, iv, args.limit)
            f = os.path.join(DATA, f"{sym}_{iv}.json")
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            span = ""
            if rows:
                t0 = rows[0]["timestamp"]; t1 = rows[-1]["timestamp"]
                span = f"{t0} → {t1}"
            print(f"{sym:10} {iv:4} {len(rows):>5} 根  {span}  -> {os.path.basename(f)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
