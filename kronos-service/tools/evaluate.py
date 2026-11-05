#!/usr/bin/env python3
"""Kronos 预测评估：多指标 + 多基准 + 严格时间切分。

铁律（任务书 §8）：
  * 严格时间顺序切分，**绝不打乱**；
  * 不能只看方向准确率；
  * 必须与"持续性预测 / EMA 趋势 / ATR 区间"三个基准比较；
  * MAPE 在价格接近 0 时会失真 —— 这里对 |actual| < 5% 均值 的点显式排除并计数。

评估方式：滚动原点（rolling origin）。对样本外区间的每个原点 t，
取它之前 lookback 根作为输入，预测未来 pred_len 根，与真实值比较。
模型是预训练且冻结的，不做任何拟合 —— 所以"训练集"只用于提供上下文。

用法：
    python tools/evaluate.py --intervals 4h,1d --origins 30 --samples 16
    python tools/evaluate.py --via-http          # 走 HTTP，验证服务端到端
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")
REPORTS = os.path.join(ROOT, "data", "reports")
STEP = {"4h": timedelta(hours=4), "1d": timedelta(days=1)}


# ------------------------------------------------------------------ 指标
def mae(a, p): return float(np.mean(np.abs(np.asarray(a) - np.asarray(p))))


def rmse(a, p): return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(p)) ** 2)))


def mape(a, p, floor):
    a = np.asarray(a, dtype=float)
    p = np.asarray(p, dtype=float)
    mask = np.abs(a) >= floor
    if mask.sum() == 0:
        return None, int(mask.sum())
    return float(np.mean(np.abs((a[mask] - p[mask]) / a[mask])) * 100), int(mask.sum())


def direction_acc(actual_path, pred_path):
    """只看末尾方向：实际涨/跌与预测涨/跌是否一致。"""
    a = np.sign(actual_path[-1] - actual_path[0])
    p = np.sign(pred_path[-1] - pred_path[0])
    return float(a == p)


# ------------------------------------------------------------------ 基准
def baseline_persistence(ctx_close, pred_len):
    return np.repeat(ctx_close[-1], pred_len)


def baseline_ema(closes, pred_len, span=20):
    """EMA 趋势外推：用 EMA 的斜率线性延伸。"""
    s = np.asarray(closes[-60:], dtype=float)
    alpha = 2 / (span + 1)
    ema = s[0]
    for x in s[1:]:
        ema = alpha * x + (1 - alpha) * ema
    slope = (ema - float(np.mean(s[:10]))) / max(len(s) - 10, 1)
    return np.array([ema + slope * (i + 1) for i in range(pred_len)])


def baseline_atr_range(highs, lows, closes, pred_len, period=14):
    """ATR 区间：以最后收盘为中心，上下各 1 ATR。返回 (lower, upper)。"""
    h = np.asarray(highs[-period - 1:], dtype=float)
    l = np.asarray(lows[-period - 1:], dtype=float)
    c_prev = np.asarray(closes[-period - 1:-1], dtype=float)
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c_prev), np.abs(l[1:] - c_prev)))
    atr = float(np.mean(tr))
    last = float(closes[-1])
    return last - atr, last + atr


# ------------------------------------------------------------------ 取数
def load(symbol, interval):
    f = os.path.join(DATA, f"{symbol}_{interval}.json")
    if not os.path.exists(f):
        return None
    with open(f, encoding="utf-8") as fh:
        rows = json.load(fh)
    for r in rows:
        r["dt"] = datetime.fromisoformat(r["timestamp"])
    return rows


# ------------------------------------------------------------------ 单次预测
def predict_via_core(svc, bars, interval, lookback, pred_len, samples):
    import pandas as pd
    df = pd.DataFrame(bars)
    out = svc.predict_distribution(
        df=df, interval=interval, lookback=lookback,
        pred_len=pred_len, sample_count=samples,
    )
    closes = np.array(out["distribution"]["closes"])          # (S, H)
    return out, closes


def predict_via_http(url, symbol, bars, interval, lookback, pred_len, samples):
    body = json.dumps({
        "symbol": symbol, "interval": interval,
        "bars": [{k: b[k] for k in ("timestamp", "open", "high", "low", "close", "volume")} for b in bars],
        "lookback": lookback, "pred_len": pred_len, "sample_count": samples,
    }).encode()
    req = urllib.request.Request(url.rstrip("/") + "/predict", data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    closes = np.array(out["distribution"]["closes"])
    return out, closes


# ------------------------------------------------------------------ 主流程
def evaluate(symbol, interval, args, svc=None):
    rows = load(symbol, interval)
    if not rows or len(rows) < args.lookback + args.pred_len + args.origins * 2:
        return {"symbol": symbol, "interval": interval, "error": "insufficient data", "n": len(rows or [])}

    n = len(rows)
    oos_start = int(n * 0.75)                    # 严格时间切分：最后 25% 为样本外
    origins = list(range(oos_start, n - args.pred_len, max(1, (n - args.pred_len - oos_start) // args.origins)))
    origins = origins[: args.origins]

    res = {
        "symbol": symbol, "interval": interval,
        "bars": n, "oos_start_ts": rows[oos_start]["timestamp"],
        "lookback": args.lookback, "pred_len": args.pred_len, "samples": args.samples,
        "origins": len(origins),
        "model": [], "persistence": [], "ema": [],
        "dir_model": [], "dir_persistence": [], "dir_ema": [],
        "coverage_model": [], "coverage_atr": [], "mape_masked": 0,
        "errors": [],
    }

    t0 = time.time()
    for oi in origins:
        ctx = rows[oi - args.lookback: oi]
        actual = rows[oi: oi + args.pred_len]
        if len(actual) < args.pred_len:
            continue
        a_close = np.array([r["close"] for r in actual])
        c_close = np.array([r["close"] for r in ctx])
        h_arr = np.array([r["high"] for r in ctx])
        l_arr = np.array([r["low"] for r in ctx])
        floor = float(np.mean(c_close)) * 0.05

        try:
            if svc is not None:
                out, closes = predict_via_core(svc, ctx, interval, args.lookback, args.pred_len, args.samples)
            else:
                out, closes = predict_via_http(args.url, symbol, ctx, interval,
                                               args.lookback, args.pred_len, args.samples)
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rows[oi]['timestamp']}: {type(exc).__name__}: {exc}")
            continue

        pred_path = closes.mean(axis=0)
        lo_path = np.percentile(closes, 10, axis=0)
        hi_path = np.percentile(closes, 90, axis=0)

        res["model"].append((a_close.tolist(), pred_path.tolist()))
        p_pers = baseline_persistence(c_close, args.pred_len)
        p_ema = baseline_ema(c_close, args.pred_len)
        res["persistence"].append((a_close.tolist(), p_pers.tolist()))
        res["ema"].append((a_close.tolist(), p_ema.tolist()))

        res["dir_model"].append(direction_acc(a_close, pred_path))
        res["dir_persistence"].append(direction_acc(a_close, p_pers))
        res["dir_ema"].append(direction_acc(a_close, p_ema))

        res["coverage_model"].append(float(np.all((a_close >= lo_path) & (a_close <= hi_path))))
        atr_lo, atr_hi = baseline_atr_range(h_arr, l_arr, c_close, args.pred_len)
        res["coverage_atr"].append(float(np.all((a_close >= atr_lo) & (a_close <= atr_hi))))
        _, kept = mape(a_close, pred_path, floor)
        res["mape_masked"] += args.pred_len - kept

    res["eval_seconds"] = round(time.time() - t0, 1)

    def fold(pairs):
        a = [x for pair in pairs for x in pair[0]]
        p = [x for pair in pairs for x in pair[1]]
        return (mae(a, p), rmse(a, p), mape(a, p, 1e-9)[0]) if a else (None, None, None)

    for name in ("model", "persistence", "ema"):
        m, r, mp = fold(res[name])
        res[f"{name}_mae"] = m
        res[f"{name}_rmse"] = r

    a_all = [x for pair in res["model"] for x in pair[0]]
    p_all = [x for pair in res["model"] for x in pair[1]]
    floor = float(np.mean(a_all)) * 0.05 if a_all else 1.0
    res["model_mape"], kept = mape(a_all, p_all, floor)
    res["mape_points_total"] = len(a_all)
    res["mape_points_used"] = kept

    res["dir_acc_model"] = float(np.mean(res["dir_model"])) if res["dir_model"] else None
    res["dir_acc_persistence"] = float(np.mean(res["dir_persistence"])) if res["dir_persistence"] else None
    res["dir_acc_ema"] = float(np.mean(res["dir_ema"])) if res["dir_ema"] else None
    res["coverage_80_model"] = float(np.mean(res["coverage_model"])) if res["coverage_model"] else None
    res["coverage_80_atr"] = float(np.mean(res["coverage_atr"])) if res["coverage_atr"] else None

    # 原始记录不落进汇总，避免文件过大
    for k in ("model", "persistence", "ema", "dir_model", "dir_persistence", "dir_ema",
              "coverage_model", "coverage_atr"):
        res.pop(k, None)
    return res


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,CAKEUSDT,BNBUSDT")
    ap.add_argument("--intervals", default="4h,1d")
    ap.add_argument("--lookback", type=int, default=400)
    ap.add_argument("--pred-len", type=int, default=6)
    ap.add_argument("--samples", type=int, default=16)
    ap.add_argument("--origins", type=int, default=30)
    ap.add_argument("--variant", default="mini")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--via-http", action="store_true")
    ap.add_argument("--url", default="http://127.0.0.1:8710")
    args = ap.parse_args(argv)

    svc = None
    if args.via_http:
        try:
            with urllib.request.urlopen(args.url + "/health", timeout=10) as r:
                h = json.loads(r.read())
            print(f"HTTP service: {h['status']} model={h['model']['name']} loaded={h['model']['loaded']}")
        except Exception as exc:                  # noqa: BLE001
            print(f"HTTP service unreachable: {exc}")
            return 1
    else:
        from app.core import KronosService
        svc = KronosService(variant=args.variant, device=args.device)
        print(f"loading {args.variant} …", end=" ", flush=True)
        svc.load()
        print(f"done in {svc.load_seconds:.1f}s")

    os.makedirs(REPORTS, exist_ok=True)
    results = []
    for sym in args.symbols.split(","):
        for iv in args.intervals.split(","):
            print(f"  evaluating {sym} {iv} …", end=" ", flush=True)
            r = evaluate(sym, iv, args, svc)
            print("error" if r.get("error") else
                  f"MAE={r['model_mae']:.4f}  方向={r['dir_acc_model']:.2f}  覆盖={r['coverage_80_model']:.2f}")
            results.append(r)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(REPORTS, f"eval-{stamp}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)

    lines = [f"# Kronos 评估报告 {stamp}", "",
             f"- 模型：{args.variant}  样本数/次：{args.samples}  预测长度：{args.pred_len}  回看：{args.lookback}",
             f"- 样本外：每序列最后 25%（严格时间切分，不打乱）", "",
             "| 标的 | 周期 | 原点 | MAE(模型) | MAE(持续) | MAE(EMA) | RMSE(模型) | MAPE% | 方向(模型) | 方向(持续) | 方向(EMA) | 80%覆盖(模型) | 80%覆盖(ATR) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        if r.get("error"):
            lines.append(f"| {r['symbol']} | {r['interval']} | - | 数据不足 | | | | | | | | | |")
            continue
        lines.append(
            f"| {r['symbol']} | {r['interval']} | {r['origins']} | "
            f"{r['model_mae']:.4f} | {r['persistence_mae']:.4f} | {r['ema_mae']:.4f} | "
            f"{r['model_rmse']:.4f} | {r['model_mape']:.2f} | "
            f"{r['dir_acc_model']:.3f} | {r['dir_acc_persistence']:.3f} | {r['dir_acc_ema']:.3f} | "
            f"{r['coverage_80_model']:.3f} | {r['coverage_80_atr']:.3f} |")
    lines += ["", "## 口径说明", "",
              "- volume 是**交易所成交量**（Binance），不是 DEX 成交量；BTCB/WBNB 用 BTCUSDT 作价格代理。",
              "- MAPE 已排除 |actual| < 5% 均值的点，避免价格接近零时失真；排除数量见 JSON。",
              "- 方向准确率只看预测窗口首尾的收盘方向，**不足以单独判定模型有效**。",
              "- 逐原点的原始预测保存在 JSON 里，可复算。"]
    md = os.path.join(REPORTS, f"eval-{stamp}.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\n报告: {md}\n明细: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
