"""DSH 侧调用适配器：把 Kronos 当**预测模型**用，不让它碰钱。

明确边界（任务书 §6/§7）：
  * 本适配器只做三件事：查状态、要预测、落记录；
  * 不 import 任何下单/钱包模块，不持有私钥，不签任何交易；
  * 预测上涨 ≠ 买入，预测下跌 ≠ 卖出 —— 这里只产出信号与证据，
    真正的动作必须由独立风控模块判定（`ai_trader` 的 assess()）。
  * 预测失败一律返回 None，调用方必须按"没有预测"处理，
    **绝不允许在预测失败时生成交易信号**。

用法（在 Windows 侧的交易仓库里）：
    from kronos_adapter import KronosClient
    kc = KronosClient()                       # 默认 http://127.0.0.1:8710
    kc.health()                               # 服务活着吗
    f = kc.predict("CAKEUSDT", "4h", bars)    # bars: list[dict]
    kc.record(f, path="runs/kronos_signals.jsonl")

依赖：只用标准库（urllib/json），不引入 requests，便于在零依赖仓库里用。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_URL = os.environ.get("KRONOS_URL", "http://127.0.0.1:8710")


@dataclass
class Forecast:
    """一次预测的只读快照。字段全部来自服务端原始输出，不做二次加工。"""

    symbol: str
    interval: str
    model: str
    last_ts: str
    last_close: float
    start_ts: str
    end_ts: str
    mean_change_pct: float
    median_change_pct: float
    p10_change_pct: float
    p90_change_pct: float
    prob_up: float
    samples: int
    elapsed_ms: int
    quality: dict[str, Any]
    raw: dict[str, Any]

    @property
    def direction(self) -> str:
        """只按概率给方向，不按均值 —— 均值在分布很宽时没有意义。"""
        if self.prob_up >= 0.65:
            return "up"
        if self.prob_up <= 0.35:
            return "down"
        return "flat"

    @property
    def is_confident(self) -> bool:
        """置信判据：概率偏离 0.5 超过 0.15，**且** p10/p90 区间不跨 0。

        区间跨 0 说明模型自己都说不好方向，此时任何方向判断都是噪声。
        """
        if self.prob_up >= 0.65 and self.p10_change_pct > 0:
            return True
        if self.prob_up <= 0.35 and self.p90_change_pct < 0:
            return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "interval": self.interval, "model": self.model,
            "last_ts": self.last_ts, "last_close": self.last_close,
            "forecast_range": [self.start_ts, self.end_ts],
            "mean_change_pct": self.mean_change_pct,
            "median_change_pct": self.median_change_pct,
            "p10_change_pct": self.p10_change_pct,
            "p90_change_pct": self.p90_change_pct,
            "prob_up": self.prob_up,
            "samples": self.samples,
            "elapsed_ms": self.elapsed_ms,
            "direction": self.direction,
            "confident": self.is_confident,
            "quality_ok": bool(self.quality.get("ok")),
        }


class KronosClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 300.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------ 传输
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def _get(self, path: str, timeout: float = 10.0) -> dict:
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return json.loads(r.read())

    # ------------------------------------------------------------ 接口
    def health(self) -> dict | None:
        """服务不可用时返回 None —— 调用方据此降级，不要抛到主流程里。"""
        try:
            return self._get("/health")
        except Exception:                                    # noqa: BLE001
            return None

    def available(self) -> bool:
        h = self.health()
        return bool(h and h.get("status") == "ok" and h.get("model", {}).get("loaded"))

    def metrics(self) -> dict | None:
        try:
            return self._get("/metrics")
        except Exception:                                    # noqa: BLE001
            return None

    def predict(
        self,
        symbol: str,
        interval: str,
        bars: Iterable[dict],
        lookback: int = 400,
        pred_len: int = 6,
        sample_count: int = 16,
    ) -> Forecast | None:
        """预测。**任何失败都返回 None**，绝不返回一个编出来的方向。"""
        body = {
            "symbol": symbol, "interval": interval,
            "bars": [
                {
                    "timestamp": b["timestamp"], "open": float(b["open"]),
                    "high": float(b["high"]), "low": float(b["low"]),
                    "close": float(b["close"]), "volume": float(b["volume"]),
                }
                for b in bars
            ],
            "lookback": lookback, "pred_len": pred_len, "sample_count": sample_count,
        }
        try:
            out = self._post("/predict", body)
        except Exception:                                    # noqa: BLE001
            return None

        try:
            d = out["distribution"]
            return Forecast(
                symbol=out["symbol"], interval=out["interval"], model=out["model"],
                last_ts=out["last_ts"], last_close=float(out["last_close"]),
                start_ts=out["forecast_range"]["start"], end_ts=out["forecast_range"]["end"],
                mean_change_pct=float(d["close_change_pct"]["mean"]),
                median_change_pct=float(d["close_change_pct"]["median"]),
                p10_change_pct=float(d["close_change_pct"]["p10"]),
                p90_change_pct=float(d["close_change_pct"]["p90"]),
                prob_up=float(d["close_change_pct"]["prob_up"]),
                samples=len(d["closes"]),
                elapsed_ms=int(out["elapsed_ms"]),
                quality=out.get("data_quality", {}),
                raw=out,
            )
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------ 记录
    @staticmethod
    def record(fc: Forecast | None, path: str = "runs/kronos_signals.jsonl",
               note: str = "") -> None:
        """把预测（或"没有预测"）写进分析记录。失败也记，因为它本身就是证据。"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": "kronos_forecast" if fc else "kronos_forecast_failed",
            "note": note,
        }
        if fc:
            rec.update(fc.as_dict())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def compare_actual(self, records: list[dict], actual_bars: dict[str, list[dict]]) -> list[dict]:
        """把历史预测与真实后续行情对比，算误差。

        actual_bars: {symbol: [bars...]}，按 timestamp 升序。
        只比较那些"预测区间已经过去"的记录；没到的跳过，不猜。
        """
        out = []
        for rec in records:
            sym = rec.get("symbol")
            rng = rec.get("forecast_range")
            bars = actual_bars.get(sym)
            if not sym or not rng or not bars:
                continue
            by_ts = {b["timestamp"]: b for b in bars}
            end_bar = by_ts.get(rng[1])
            if not end_bar:
                continue
            actual_change = (end_bar["close"] - rec["last_close"]) / rec["last_close"] * 100
            err = actual_change - rec["mean_change_pct"]
            out.append({
                "symbol": sym, "interval": rec.get("interval"),
                "last_ts": rec.get("last_ts"), "end_ts": rng[1],
                "pred_change_pct": rec["mean_change_pct"],
                "actual_change_pct": actual_change,
                "abs_error_pct": abs(err),
                "direction_hit": (actual_change > 0) == (rec.get("prob_up", 0.5) > 0.5),
                "in_80_interval": rec["p10_change_pct"] <= actual_change <= rec["p90_change_pct"],
            })
        return out
