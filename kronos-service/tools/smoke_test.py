#!/usr/bin/env python3
"""HTTP 接口冒烟测试：正常路径 + 四种必须被拒的脏数据。

验收点：
  * /predict 返回完整分布（不是单点）；
  * 未收盘 K 线被拒（422）；
  * 重复时间戳被拒；
  * OHLC 非法被拒；
  * 模型未加载时返回 503 而不是崩掉（用错 variant 模拟不了，这里跳过）。

用法：
    python tools/smoke_test.py --url http://127.0.0.1:8710
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def post(url, path, body, timeout=300):
    req = urllib.request.Request(url.rstrip("/") + path,
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8710")
    ap.add_argument("--symbol", default="CAKEUSDT")
    ap.add_argument("--interval", default="4h")
    args = ap.parse_args(argv)

    rows = json.load(open(os.path.join(DATA, f"{args.symbol}_{args.interval}.json"), encoding="utf-8"))
    bars = [{k: r[k] for k in ("timestamp", "open", "high", "low", "close", "volume")} for r in rows[-400:]]
    base = {"symbol": args.symbol, "interval": args.interval, "bars": bars,
            "lookback": 400, "pred_len": 6, "sample_count": 16}

    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    print("=== 1. 正常预测 ===")
    t0 = time.time()
    st, out = post(args.url, "/predict", base)
    wall = time.time() - t0
    check("HTTP 200", st == 200, f"got {st}: {str(out)[:200]}")
    if st == 200:
        d = out["distribution"]
        check("有分布而非单点", "closes" in d and len(d["closes"]) == 16, f"samples={len(d.get('closes', []))}")
        check("每条路径 6 根", len(d["closes"][0]) == 6)
        check("有 p10/p90", "p10" in d["final_close"] and "p90" in d["final_close"])
        check("有 prob_up", "prob_up" in d["close_change_pct"])
        check("有数据质量报告", out["data_quality"]["ok"] is True)
        check("有推理耗时", isinstance(out["elapsed_ms"], int))
        check("预测时间范围 6 根", len(out["mean_ohlcv"]) == 6)
        print(f"        模型={out['model']}  耗时={out['elapsed_ms']}ms  实际往返={wall*1000:.0f}ms")
        print(f"        最后收盘={out['last_close']}  均值变动={d['close_change_pct']['mean']:.3f}%  "
              f"prob_up={d['close_change_pct']['prob_up']:.3f}")
        # 原始输出必须保留：closes 是逐路径逐根的数值
        check("保留原始路径（不伪造）", all(isinstance(x, float) for row in d["closes"] for x in row))

    print("=== 2. 未收盘 K 线必须被拒 ===")
    bad = copy.deepcopy(base)
    # 伪造一根"开盘于现在"的 K 线
    now = time.gmtime()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() // 14400 * 14400))
    bad["bars"][-1] = {"timestamp": ts, "open": 1.9, "high": 1.95, "low": 1.85, "close": 1.92, "volume": 1000}
    st2, out2 = post(args.url, "/predict", bad)
    # drop_unclosed=true（默认）会剔除最后一根后继续；显式关闭则必须 422
    bad["drop_unclosed"] = False
    st3, out3 = post(args.url, "/predict", bad)
    check("drop_unclosed=false 时 422", st3 == 422, f"got {st3}: {str(out3)[:160]}")
    check("拒绝原因提到未收盘", "not closed" in json.dumps(out3), str(out3)[:160])

    print("=== 3. 重复时间戳必须被拒 ===")
    bad = copy.deepcopy(base)
    bad["bars"][-1]["timestamp"] = bad["bars"][-2]["timestamp"]
    st4, out4 = post(args.url, "/predict", bad)
    check("422", st4 == 422, f"got {st4}: {str(out4)[:160]}")

    print("=== 4. OHLC 非法必须被拒 ===")
    bad = copy.deepcopy(base)
    bad["bars"][-1]["high"] = bad["bars"][-1]["low"] - 1     # high < low
    st5, out5 = post(args.url, "/predict", bad)
    check("422", st5 == 422, f"got {st5}: {str(out5)[:160]}")

    print("=== 5. 样本数越界必须被拒 ===")
    bad = copy.deepcopy(base)
    bad["sample_count"] = 999
    st6, out6 = post(args.url, "/predict", bad)
    check("422", st6 == 422, f"got {st6}")

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
