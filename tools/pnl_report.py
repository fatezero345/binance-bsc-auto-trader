#!/usr/bin/env python3
"""盈亏快照：写一份人看的报告，供定时任务每 30 分钟调用一次。

输出两个文件（都在 runs/ 下，已 gitignore）：
  pnl_report.md    最近 24 条（约 12 小时）的表格，直接打开看
  pnl_report.jsonl 全量流水，一行一次，便于以后做曲线

为什么不发通知：所有者明确要求过"不要弹窗"。
所以这里只落盘 —— 想看就打开 pnl_report.md。

用法：
    python tools/pnl_report.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from src.market import binance_public as bp  # noqa: E402
from src.risk import load_policy  # noqa: E402
from src.wallet import binance_account as wa  # noqa: E402

RUNS = ROOT / "runs"
JSONL = RUNS / "pnl_report.jsonl"
MD = RUNS / "pnl_report.md"
SYMBOLS = ("CAKEUSDT", "BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")
ASSETS = ("USDT", "CAKE", "BNB", "BTC", "ETH", "SOL", "ETHW", "WBTC", "FDUSD", "BUSD")


CRED = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".binance-sandbox" / "live.json"


def _creds() -> tuple[str, str]:
    """优先读 ~/.binance-sandbox/live.json。

    计划任务的上下文里没有交互式环境变量，所以不能只靠 environ ——
    2026-11-04 第一次跑定时汇报就是因此失败（env 里没有 key，报了 error 行）。
    """
    if CRED.exists():
        d = json.loads(CRED.read_text(encoding="utf-8"))
        return d["api_key"], d["api_secret"]
    return wa.load_credentials(os.environ)


def snapshot() -> dict:
    policy = load_policy()
    key, secret = _creds()
    acct = wa.get_account(key, secret)

    held: dict[str, Decimal] = {}
    for a in ASSETS:
        try:
            held[a] = Decimal(wa.balances(acct, assets=(a,)).get(a, "0"))
        except Exception:                                      # noqa: BLE001
            held[a] = Decimal("0")

    px: dict[str, Decimal] = {}
    for s in SYMBOLS:
        try:
            px[s] = Decimal(str(bp.last_price(s)))
        except Exception:                                      # noqa: BLE001
            px[s] = Decimal("0")

    value = held.get("USDT", Decimal("0"))
    breakdown = []
    for base, sym in (("CAKE", "CAKEUSDT"), ("BNB", "BNBUSDT"), ("BTC", "BTCUSDT")):
        q = held.get(base, Decimal("0"))
        if q > 0 and px.get(sym):
            v = q * px[sym]
            value += v
            breakdown.append({"asset": base, "qty": str(q), "price": str(px[sym]),
                              "value_usdt": str(v.quantize(Decimal("0.0001")))})

    start = Decimal(str(policy.raw["capital"]["initial_equity_usdt"]))
    # 起始以第一次实测为准；配置里的 29.30 与账户实际不同，这里用 28.24316192
    START = Decimal("28.24316192")
    pnl = value - START
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "equity_usdt": str(value.quantize(Decimal("0.00000001"))),
        "start_usdt": str(START),
        "pnl_usdt": str(pnl.quantize(Decimal("0.00000001"))),
        "pnl_pct": str((pnl / START * 100).quantize(Decimal("0.0001"))),
        "usdt_free": str(held.get("USDT", Decimal("0"))),
        "holdings": breakdown,
        "config_equity": str(start),
    }


def render(rows: list[dict]) -> str:
    last = rows[-1]
    lines = [
        "# 盈亏快照",
        "",
        f"最近更新：**{last['ts']}**（每 30 分钟自动刷新）",
        "",
        f"| 项 | 值 |",
        f"| --- | --- |",
        f"| 总权益 | **{last['equity_usdt']} USDT** |",
        f"| 起始 | {last['start_usdt']} USDT |",
        f"| **净盈亏** | **{last['pnl_usdt']} USDT（{last['pnl_pct']}%）** |",
        f"| 可用现金 | {last['usdt_free']} USDT |",
        "",
    ]
    if last["holdings"]:
        lines += ["## 持仓", "", "| 资产 | 数量 | 现价 | 折合 USDT |", "| --- | --- | --- | --- |"]
        for h in last["holdings"]:
            lines.append(f"| {h['asset']} | {h['qty']} | {h['price']} | {h['value_usdt']} |")
        lines.append("")
    else:
        lines += ["## 持仓", "", "空仓。", ""]

    lines += [
        "## 历史（最近 24 条）",
        "",
        "| 时间 | 权益 | 净盈亏 | 收益率 |",
        "| --- | --- | --- | --- |",
    ]
    for r in rows[-24:]:
        lines.append(f"| {r['ts'][5:16]} | {r['equity_usdt']} | {r['pnl_usdt']} | {r['pnl_pct']}% |")
    lines.append("")
    lines.append("> 起始 28.24316192 USDT 是 2026-11-03 第一笔实盘前的实测值；"
                 "配置里的 capital.initial_equity_usdt 另有其值，不用于计算。")
    return "\n".join(lines)


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    try:
        rec = snapshot()
    except Exception as exc:                                   # noqa: BLE001
        with JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                 "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                                ensure_ascii=False) + "\n")
        return 1

    with JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    rows = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "equity_usdt" in d:
            rows.append(d)

    MD.write_text(render(rows), encoding="utf-8")

    # 顺带刷新可视化看板（自包含 HTML，侧边栏直接开）
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import dashboard  # noqa: PLC0415

        dashboard.main([])
    except Exception as exc:                                   # noqa: BLE001
        print(f"dashboard failed: {exc}")
    print(f"{rec['ts']}  equity={rec['equity_usdt']}  pnl={rec['pnl_usdt']} ({rec['pnl_pct']}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
