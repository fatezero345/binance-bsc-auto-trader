"""Kronos 推理服务（FastAPI）。

只监听回环 / 内网；不对外。模型加载失败不影响进程存活 —— /health 会如实报告，
DSH 侧据此判断"模型不可用"，而不是让整个调用链崩掉。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import core
from .core import DataQualityError, KronosService, drop_unclosed, validate_ohlcv

DB_PATH = os.environ.get("KRONOS_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "predictions.db"))
VARIANT = os.environ.get("KRONOS_VARIANT", "mini")
DEVICE = os.environ.get("KRONOS_DEVICE", "cpu")
THREADS = int(os.environ.get("KRONOS_THREADS", "8"))
TIMEOUT_S = float(os.environ.get("KRONOS_TIMEOUT_S", "120"))

METRICS = {
    "predict_ok": 0,
    "predict_fail": 0,
    "total_ms": 0,
    "started_at": time.time(),
    "last_error": None,
}

svc = KronosService(variant=VARIANT, device=DEVICE, threads=THREADS)


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            variant TEXT NOT NULL,
            lookback INTEGER,
            pred_len INTEGER,
            samples INTEGER,
            last_ts TEXT,
            last_close REAL,
            mean_change_pct REAL,
            prob_up REAL,
            elapsed_ms INTEGER,
            quality TEXT,
            payload TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sym ON predictions(symbol, interval, ts)")
    conn.commit()
    return conn


def _persist(rec: dict[str, Any]) -> None:
    try:
        conn = _db()
        with conn:
            conn.execute(
                "INSERT INTO predictions (ts,symbol,interval,variant,lookback,pred_len,samples,"
                "last_ts,last_close,mean_change_pct,prob_up,elapsed_ms,quality,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec["ts"], rec["symbol"], rec["interval"], rec["variant"],
                    rec["lookback"], rec["pred_len"], rec["samples"],
                    rec["last_ts"], rec["last_close"],
                    rec["mean_change_pct"], rec["prob_up"], rec["elapsed_ms"],
                    json.dumps(rec["quality"], ensure_ascii=False),
                    json.dumps(rec["payload"], ensure_ascii=False),
                ),
            )
        conn.close()
    except Exception as exc:                      # noqa: BLE001
        METRICS["last_error"] = f"persist: {type(exc).__name__}: {exc}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        svc.load()
    except Exception:                              # noqa: BLE001
        pass                                       # 加载失败也照常起服务，由 /health 暴露
    yield


app = FastAPI(title="Kronos Inference Service", version=core.VERSION, lifespan=lifespan)


# ----------------------------------------------------------------- 模型
class Bar(BaseModel):
    timestamp: str | int | float
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None


class PredictRequest(BaseModel):
    symbol: str
    interval: str = Field(default="4h", pattern="^(1m|5m|15m|30m|1h|2h|4h|1d|1w)$")
    bars: list[Bar] = Field(min_length=32)
    lookback: int = Field(default=400, ge=32, le=2048)
    pred_len: int = Field(default=6, ge=1, le=120)
    sample_count: int = Field(default=16, ge=1, le=64)
    temperature: float = Field(default=1.0, gt=0, le=3)
    top_p: float = Field(default=0.9, gt=0, le=1)
    drop_unclosed: bool = True


@app.get("/health")
def health() -> dict[str, Any]:
    info = svc.info()
    return {
        "status": "ok" if info["loaded"] else "degraded",
        "model": {
            "name": info["model"],
            "variant": info["variant"],
            "tokenizer": info["tokenizer"],
            "params": info["params"],
            "max_context": info["max_context"],
            "loaded": info["loaded"],
            "load_error": info["load_error"],
            "load_seconds": info["load_seconds"],
        },
        "device": info["device"],
        "torch": info["torch"],
        "threads": info["threads"],
        "version": core.VERSION,
        "uptime_s": round(time.time() - METRICS["started_at"], 1),
    }


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    rss_mb = None
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_mb = round(int(line.split()[1]) / 1024, 1)
                    break
    except OSError:
        pass
    ok, fail = METRICS["predict_ok"], METRICS["predict_fail"]
    return {
        "predict_ok": ok,
        "predict_fail": fail,
        "success_rate": round(ok / (ok + fail) * 100, 2) if (ok + fail) else None,
        "avg_inference_ms": round(METRICS["total_ms"] / ok, 1) if ok else None,
        "avg_success_inference_ms": round(METRICS["total_ms"] / ok, 1) if ok else None,
        "rss_mb": rss_mb,
        "loadavg": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "model_loaded": svc.loaded,
        "uptime_s": round(time.time() - METRICS["started_at"], 1),
        "last_error": METRICS["last_error"],
    }


@app.post("/predict")
async def predict(req: PredictRequest) -> dict[str, Any]:
    rows = [b.model_dump() for b in req.bars]
    df = pd.DataFrame(rows)
    try:
        quality = validate_ohlcv(df, req.interval)
    except DataQualityError as exc:
        METRICS["predict_fail"] += 1
        METRICS["last_error"] = str(exc)
        raise HTTPException(status_code=422, detail=f"data quality: {exc}") from exc

    # 未收盘的 K 线：剔除后重算质量报告
    if quality.unclosed_bar:
        if req.drop_unclosed:
            df = drop_unclosed(df, req.interval)
            quality = validate_ohlcv(df, req.interval, now=time.time() + 10**9)  # 已剔除，不再判
        else:
            METRICS["predict_fail"] += 1
            METRICS["last_error"] = "unclosed bar in input"
            raise HTTPException(status_code=422, detail="last bar is not closed; refusing to predict on partial data")

    if quality.ohlc_violations or quality.duplicates:
        METRICS["predict_fail"] += 1
        METRICS["last_error"] = "invalid ohlcv"
        raise HTTPException(status_code=422, detail=quality.as_dict())

    if not svc.loaded:
        METRICS["predict_fail"] += 1
        METRICS["last_error"] = f"model not loaded: {svc.load_error}"
        raise HTTPException(status_code=503, detail=f"model not loaded: {svc.load_error}")

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: svc.predict_distribution(
                    df=df, interval=req.interval, lookback=req.lookback,
                    pred_len=req.pred_len, sample_count=req.sample_count,
                    temperature=req.temperature, top_p=req.top_p,
                )
            ),
            timeout=TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        METRICS["predict_fail"] += 1
        METRICS["last_error"] = f"timeout after {TIMEOUT_S}s"
        raise HTTPException(status_code=504, detail=f"inference timeout after {TIMEOUT_S}s") from None
    except DataQualityError as exc:
        METRICS["predict_fail"] += 1
        METRICS["last_error"] = str(exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:                       # noqa: BLE001
        METRICS["predict_fail"] += 1
        METRICS["last_error"] = f"{type(exc).__name__}: {exc}"
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}") from exc

    METRICS["predict_ok"] += 1
    METRICS["total_ms"] += result["elapsed_ms"]

    payload = {
        "model": svc.spec["model"],
        "variant": svc.variant,
        "symbol": req.symbol,
        "interval": req.interval,
        "last_ts": result["last_ts"],
        "forecast_range": {"start": result["future_ts"][0], "end": result["future_ts"][-1]},
        "last_close": result["last_close"],
        "mean_ohlcv": result["mean_ohlcv"],
        "distribution": result["distribution"],
        "elapsed_ms": result["elapsed_ms"],
        "data_quality": quality.as_dict(),
    }
    _persist({
        "ts": time.time(), "symbol": req.symbol, "interval": req.interval,
        "variant": svc.variant, "lookback": req.lookback, "pred_len": req.pred_len,
        "samples": req.sample_count, "last_ts": result["last_ts"],
        "last_close": result["last_close"],
        "mean_change_pct": result["distribution"]["close_change_pct"]["mean"],
        "prob_up": result["distribution"]["close_change_pct"]["prob_up"],
        "elapsed_ms": result["elapsed_ms"], "quality": quality.as_dict(),
        "payload": payload,
    })
    return payload
