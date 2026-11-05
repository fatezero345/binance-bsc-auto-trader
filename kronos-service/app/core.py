"""Kronos 推理核心：模型加载、数据校验、采样分布。

设计约束（来自 P1 任务书）：
  * 模型只加载一次；
  * 不修改官方模型代码（Kronos/ 目录保持上游原样，只 import）；
  * 不伪造模型输出 —— 这里返回的一切都是模型 token 解码后的原始数值；
  * 只在本地监听，不对外。

获取"分布"的做法值得单独说明：
官方 KronosPredictor.generate() 在内部执行 `np.mean(preds, axis=1)`，
即 sample_count 条采样路径会被平均掉，公共 API 拿不到单条路径。
但 predict_batch() 的 batch 维不会被平均 —— 所以把同一段历史复制 N 份、
每份 sample_count=1 走一次批量前向，就能拿到 N 条**互相独立**的采样路径。
这既没有改模型，也没有绕开采样，只是换了一个能得到分布的入口。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

KRONOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Kronos")
if KRONOS_DIR not in sys.path:
    sys.path.insert(0, KRONOS_DIR)

from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

VERSION = "1.0.0"

# 每个 Kronos 变体配一个固定的 tokenizer 与上下文上限（见官方 README 表格）
VARIANTS = {
    "mini": {"model": "NeoQuasar/Kronos-mini", "tokenizer": "NeoQuasar/Kronos-Tokenizer-2k", "max_context": 2048, "params": "4.1M"},
    "small": {"model": "NeoQuasar/Kronos-small", "tokenizer": "NeoQuasar/Kronos-Tokenizer-base", "max_context": 512, "params": "24.7M"},
}

# 周期 -> 秒，用于"未收盘 K 线"判断
INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400,
    "1d": 86400, "1w": 604800,
}


class DataQualityError(ValueError):
    """输入数据未通过校验。一律拒绝推理，不猜。"""


@dataclass
class QualityReport:
    rows: int
    first_ts: str
    last_ts: str
    duplicates: int
    gaps: int
    ohlc_violations: int
    zero_volume: int
    volume_outliers: int
    unclosed_bar: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "duplicates": self.duplicates,
            "gaps": self.gaps,
            "ohlc_violations": self.ohlc_violations,
            "zero_volume": self.zero_volume,
            "volume_outliers": self.volume_outliers,
            "unclosed_bar": self.unclosed_bar,
            "ok": not self.unclosed_bar and self.ohlc_violations == 0 and self.duplicates == 0,
            "notes": self.notes,
        }


def validate_ohlcv(df: pd.DataFrame, interval: str, now: float | None = None) -> QualityReport:
    """严格校验 OHLCV。任何一项不过都不推理。

    重点：**未收盘的 K 线必须剔除**，否则模型会把"半根 K 线"当成完整历史，
    最后一根的高低价随时会变，预测等于用了未来函数。
    """
    need = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise DataQualityError(f"missing columns: {missing}")

    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
    if d["timestamp"].dt.tz is not None:
        d["timestamp"] = d["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)

    notes: list[str] = []
    dups = int(d["timestamp"].duplicated().sum())
    if dups:
        notes.append(f"{dups} duplicate timestamps")

    d = d.sort_values("timestamp").reset_index(drop=True)
    step = INTERVAL_SECONDS.get(interval)
    gaps = 0
    if step and len(d) > 1:
        deltas = d["timestamp"].diff().dt.total_seconds().dropna()
        gaps = int((deltas > step * 1.5).sum())
        if gaps:
            notes.append(f"{gaps} gaps larger than {interval}")

    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    bad = int((
        (h < l) | (h < o) | (h < c) | (l > o) | (l > c)
        | (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
    ).sum())
    if bad:
        notes.append(f"{bad} bars violate OHLC invariants")

    zero_vol = int((d["volume"] <= 0).sum())
    if zero_vol:
        notes.append(f"{zero_vol} bars with zero volume")

    vol_out = 0
    if len(d) > 20 and d["volume"].std() > 0:
        z = (d["volume"] - d["volume"].mean()) / d["volume"].std()
        vol_out = int((z.abs() > 8).sum())
        if vol_out:
            notes.append(f"{vol_out} volume outliers (|z|>8)")

    now_ts = now if now is not None else time.time()
    unclosed = False
    if step:
        last_epoch = d["timestamp"].iloc[-1].timestamp()
        # 最后一根的开盘时间 + 周期 > 现在 => 还没收盘
        if last_epoch + step > now_ts:
            unclosed = True
            notes.append(f"last bar at {d['timestamp'].iloc[-1]} is not closed yet")

    return QualityReport(
        rows=len(d),
        first_ts=str(d["timestamp"].iloc[0]),
        last_ts=str(d["timestamp"].iloc[-1]),
        duplicates=dups,
        gaps=gaps,
        ohlc_violations=bad,
        zero_volume=zero_vol,
        volume_outliers=vol_out,
        unclosed_bar=unclosed,
        notes=notes,
    )


def drop_unclosed(df: pd.DataFrame, interval: str, now: float | None = None) -> pd.DataFrame:
    """把尚未收盘的最后一根剔掉（校验不过时的补救路径）。"""
    step = INTERVAL_SECONDS.get(interval)
    if not step or df.empty:
        return df
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
    if d["timestamp"].dt.tz is not None:
        d["timestamp"] = d["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)
    d = d.sort_values("timestamp").reset_index(drop=True)
    now_ts = now if now is not None else time.time()
    if d["timestamp"].iloc[-1].timestamp() + step > now_ts:
        return d.iloc[:-1].reset_index(drop=True)
    return d


class KronosService:
    """进程内单例：模型只加载一次。"""

    def __init__(self, variant: str = "mini", device: str = "cpu", threads: int | None = None):
        if variant not in VARIANTS:
            raise ValueError(f"unknown variant {variant!r}, expected one of {list(VARIANTS)}")
        self.variant = variant
        self.spec = VARIANTS[variant]
        self.device = device
        self.loaded = False
        self.load_error: str | None = None
        self.load_seconds: float = 0.0
        self.model = None
        self.tokenizer = None
        self.predictor = None

        if threads:
            import torch
            torch.set_num_threads(int(threads))

    def load(self) -> None:
        t0 = time.time()
        try:
            self.tokenizer = KronosTokenizer.from_pretrained(self.spec["tokenizer"])
            self.model = Kronos.from_pretrained(self.spec["model"])
            self.model.eval()
            self.predictor = KronosPredictor(
                self.model, self.tokenizer,
                device=self.device, max_context=self.spec["max_context"],
            )
            self.loaded = True
            self.load_error = None
        except Exception as exc:                     # noqa: BLE001
            self.loaded = False
            self.load_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.load_seconds = time.time() - t0

    def info(self) -> dict[str, Any]:
        import torch
        return {
            "variant": self.variant,
            "model": self.spec["model"],
            "tokenizer": self.spec["tokenizer"],
            "max_context": self.spec["max_context"],
            "params": self.spec["params"],
            "loaded": self.loaded,
            "load_error": self.load_error,
            "load_seconds": round(self.load_seconds, 2),
            "device": self.device,
            "torch": torch.__version__,
            "threads": torch.get_num_threads(),
            "version": VERSION,
        }

    # ------------------------------------------------------------------ 推理
    def predict_distribution(
        self,
        df: pd.DataFrame,
        interval: str,
        lookback: int,
        pred_len: int,
        sample_count: int,
        temperature: float = 1.0,
        top_p: float = 0.9,
        top_k: int = 0,
    ) -> dict[str, Any]:
        """返回**分布**而不是单点。"""
        if not self.loaded:
            raise RuntimeError(f"model not loaded: {self.load_error}")

        d = df.sort_values("timestamp").reset_index(drop=True)
        d["timestamp"] = pd.to_datetime(d["timestamp"])

        if lookback > len(d):
            raise DataQualityError(f"lookback {lookback} > available rows {len(d)}")
        ctx = d.iloc[-lookback:].reset_index(drop=True)

        cols = ["open", "high", "low", "close", "volume"]
        if "amount" in ctx.columns and ctx["amount"].notna().any():
            cols.append("amount")
        x_df = ctx[cols].astype(float)
        x_ts = ctx["timestamp"]

        step = INTERVAL_SECONDS.get(interval)
        if not step:
            raise DataQualityError(f"unsupported interval {interval}")
        last_ts = ctx["timestamp"].iloc[-1]
        y_ts = pd.Series([last_ts + pd.Timedelta(seconds=step * (i + 1)) for i in range(pred_len)])

        t0 = time.time()
        # 同一段历史复制 N 份：batch 维不被平均，得到 N 条独立采样路径
        preds = self.predictor.predict_batch(
            df_list=[x_df] * sample_count,
            x_timestamp_list=[x_ts] * sample_count,
            y_timestamp_list=[y_ts] * sample_count,
            pred_len=pred_len,
            T=temperature,
            top_k=top_k,
            top_p=top_p,
            sample_count=1,
            verbose=False,
        )
        elapsed = time.time() - t0

        # paths[s][h] = (open, high, low, close, volume[, amount])
        paths = np.stack([p[["open", "high", "low", "close", "volume"]].values for p in preds], axis=0)
        closes = paths[:, :, 3]                       # (S, H)
        last_close = float(ctx["close"].iloc[-1])

        mean_path = closes.mean(axis=0)
        final_close = closes[:, -1]
        change = (final_close - last_close) / last_close

        return {
            "samples": sample_count,
            "pred_len": pred_len,
            "lookback": lookback,
            "last_close": last_close,
            "last_ts": str(last_ts),
            "future_ts": [str(t) for t in y_ts],
            "mean_ohlcv": [
                {
                    "timestamp": str(y_ts[i]),
                    "open": float(paths[:, i, 0].mean()),
                    "high": float(paths[:, i, 1].mean()),
                    "low": float(paths[:, i, 2].mean()),
                    "close": float(mean_path[i]),
                    "volume": float(paths[:, i, 4].mean()),
                }
                for i in range(pred_len)
            ],
            "distribution": {
                "final_close": {
                    "mean": float(final_close.mean()),
                    "median": float(np.median(final_close)),
                    "std": float(final_close.std()),
                    "p10": float(np.percentile(final_close, 10)),
                    "p90": float(np.percentile(final_close, 90)),
                    "min": float(final_close.min()),
                    "max": float(final_close.max()),
                },
                "close_change_pct": {
                    "mean": float(change.mean() * 100),
                    "median": float(np.median(change) * 100),
                    "p10": float(np.percentile(change, 10) * 100),
                    "p90": float(np.percentile(change, 90) * 100),
                    "prob_up": float((change > 0).mean()),
                },
                "high_low_range": {
                    h: {
                        "high_mean": float(paths[:, h, 1].mean()),
                        "low_mean": float(paths[:, h, 2].mean()),
                        "high_p90": float(np.percentile(paths[:, h, 1], 90)),
                        "low_p10": float(np.percentile(paths[:, h, 2], 10)),
                    }
                    for h in range(pred_len)
                },
                "closes": closes.tolist(),          # 原始路径，供评估与复盘
            },
            "elapsed_ms": int(elapsed * 1000),
        }
