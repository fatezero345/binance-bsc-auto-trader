"""Kronos 预测服务客户端 + 仓位系数。

这里做的是"强制使用"：**每一笔买单都会经过 Kronos 加权**，权重直接取它给出的
上涨概率。所以它在决策链里不是装饰品 —— 但它的作用范围被刻意限制在
**缩小仓位**上，不能凭空开仓、不能突破风控上限。

## 为什么用 prob_up 而不是校准区间

校准后的 p10–p90 区间有 98% 的时间跨零（实测），如果按"区间跨零就弃权"，
Kronos 几乎不会影响任何决策 —— 那就不叫强制使用了。
prob_up 是模型自己给出的方向概率，它连续、总是有值、天然落在 [0,1]，
正好可以当仓位系数用。

## 明确的边界

* 只在 `do_buy` 里被调用 —— 也就是**入场闸已经放行之后**。
  Kronos 不能把一个被闸门拒绝的机会变成买入。
* 系数被夹在 [min_factor, 1.0]：**它只能让仓位变小，不能变大**。
  这样任何情况下都不会突破 `notional_ceiling` 和风险预算。
* 服务不可用时返回中性系数（1.0）并记日志 —— 不让预测服务挂掉拖死交易。
* 完全不碰钱包、不签交易、不下单。

## 口径

* 用 4h K 线：与持仓期（4 小时超时）一致，预测视野 6 根 = 24 小时。
* 回看 200 根：Kronos-mini 的 max_context 是 2048，200 足够且推理更快。
"""

from __future__ import annotations

import json
import statistics
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8710"


class KronosDecision:
    """一次 Kronos 加权的结果。字段全部可落账，便于事后复盘它到底有没有帮上忙。"""

    __slots__ = ("ok", "prob_up", "median_change_pct", "p10", "p90",
                 "size_factor", "elapsed_ms", "error")

    def __init__(self, ok: bool, prob_up: float | None = None,
                 median_change_pct: float | None = None,
                 p10: float | None = None, p90: float | None = None,
                 size_factor: float = 1.0, elapsed_ms: int | None = None,
                 error: str | None = None):
        self.ok = ok
        self.prob_up = prob_up
        self.median_change_pct = median_change_pct
        self.p10 = p10
        self.p90 = p90
        self.size_factor = size_factor
        self.elapsed_ms = elapsed_ms
        self.error = error

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "prob_up": None if self.prob_up is None else round(self.prob_up, 4),
            "median_change_pct": None if self.median_change_pct is None else round(self.median_change_pct, 4),
            "p10": None if self.p10 is None else round(self.p10, 4),
            "p90": None if self.p90 is None else round(self.p90, 4),
            "size_factor": round(self.size_factor, 4),
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


class KronosClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 30.0,
                 lookback: int = 200, pred_len: int = 6, samples: int = 16,
                 min_factor: float = 0.25, repeats: int = 5):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.lookback = lookback
        self.pred_len = pred_len
        self.samples = samples
        self.min_factor = min_factor
        # 去噪：单次采样的 prob_up 标准差实测约 0.10（同一段不变的 K 线连问 6 次，
        # 极差能到 0.28）。这样的系数等于让随机数决定仓位。
        # 取 repeats 次的中位数，把抖动压下去。
        self.repeats = max(1, int(repeats))

    # ------------------------------------------------------------------ 传输
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def health(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=5) as r:
                return json.loads(r.read())
        except Exception:                                    # noqa: BLE001
            return None

    def available(self) -> bool:
        h = self.health()
        return bool(h and h.get("status") == "ok" and h.get("model", {}).get("loaded"))

    # ------------------------------------------------------------------ 决策
    def decision(self, symbol: str, interval: str, bars: list[dict]) -> KronosDecision:
        """返回仓位系数。**任何失败都退化成中性 1.0**，绝不阻断交易。"""
        body = {
            "symbol": symbol,
            "interval": interval,
            "bars": [
                {
                    "timestamp": b["timestamp"],
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b["volume"]),
                }
                # 多带 8 根：服务会剔除未收盘的最后一根，
                # 只发正好 lookback 根会在剔除后不足，被服务拒绝（实测踩过）
                for b in bars[-(self.lookback + 8):]
            ],
            "lookback": min(self.lookback, len(bars)),
            "pred_len": self.pred_len,
            "sample_count": self.samples,
        }
        probs, meds, p10s, p90s, elapsed = [], [], [], [], 0
        last_err = None
        for _ in range(self.repeats):
            try:
                out = self._post("/predict", body)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = (exc.read() or b"").decode("utf-8", "replace")[:160]
                except Exception:                            # noqa: BLE001
                    pass
                last_err = f"HTTP {exc.code}: {detail}"
                break
            except Exception as exc:                         # noqa: BLE001
                last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
                break
            try:
                d = out["distribution"]["close_change_pct"]
                probs.append(float(d["prob_up"]))
                meds.append(float(d["median"]))
                p10s.append(float(d["p10"]))
                p90s.append(float(d["p90"]))
                elapsed += int(out.get("elapsed_ms") or 0)
            except (KeyError, TypeError, ValueError) as exc:
                last_err = f"bad payload: {exc}"
                break

        if not probs:
            return KronosDecision(False, error=last_err or "no sample")

        # 中位数：对采样噪声稳健，比均值更抗离群
        prob_up = statistics.median(probs)
        factor = max(self.min_factor, min(1.0, prob_up))
        return KronosDecision(
            True,
            prob_up=prob_up,
            median_change_pct=statistics.median(meds),
            p10=statistics.median(p10s),
            p90=statistics.median(p90s),
            size_factor=factor,
            elapsed_ms=elapsed,
        )
