#!/usr/bin/env python3
"""生成自包含的盈亏看板 runs/dashboard.html。

零依赖：不引 CDN、不引字体，SVG 手绘曲线，离线也能开。
数据全部来自本地文件，不联网：
    runs/pnl_report.jsonl   权益序列
    runs/ledger.jsonl       成交与事件流水
    runs/state.json         当前持仓

用法：
    python tools/dashboard.py            # 写盘
    python tools/dashboard.py --print    # 顺带打印路径
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
OUT = RUNS / "dashboard.html"

START = 28.24316192


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def kronos_stats(ledger: list[dict]) -> dict:
    """把建仓时的 Kronos 系数与平仓盈亏配对。
    口径与 tools/kronos_scorecard.py、插件 index.js 完全一致：FIFO 配对。"""
    last: dict[str, float] = {}
    q: dict[str, list] = {}
    pairs: list[dict] = []
    orphan = 0
    with_factor = 0
    for r in ledger:
        sym = r.get("symbol")
        if not sym:
            continue
        k = r.get("kind")
        if k == "kronos" and r.get("ok") and isinstance(r.get("size_factor"), (int, float)):
            last[sym] = float(r["size_factor"])
        elif k == "bought":
            q.setdefault(sym, []).append({"ts": r.get("ts"), "factor": last.get(sym)})
            if last.get(sym) is not None:
                with_factor += 1
        elif k == "sold" and isinstance(r.get("pnl_usdt"), (int, float)):
            bucket = q.get(sym) or []
            e = bucket.pop(0) if bucket else None
            if not e or e["factor"] is None:
                orphan += 1
                continue
            pairs.append({"symbol": sym, "factor": e["factor"],
                          "pnl": float(r["pnl_usdt"]), "why": r.get("why")})

    def agg(g):
        if not g:
            return None
        v = [x["pnl"] for x in g]
        return {"n": len(v), "mean": sum(v) / len(v), "sum": sum(v),
                "win": sum(1 for x in v if x > 0) / len(v) * 100}

    high = [x for x in pairs if x["factor"] >= 0.60]
    low = [x for x in pairs if x["factor"] < 0.40]
    mid = [x for x in pairs if 0.40 <= x["factor"] < 0.60]

    tv = None
    if len(high) >= 2 and len(low) >= 2:
        import statistics as st
        a = [x["pnl"] for x in high]
        b = [x["pnl"] for x in low]
        se = (st.variance(a) / len(a) + st.variance(b) / len(b)) ** 0.5
        if se:
            tv = (st.mean(a) - st.mean(b)) / se

    return {"pairs": len(pairs), "with_factor": with_factor, "orphan": orphan,
            "high": agg(high), "mid": agg(mid), "low": agg(low), "t": tv,
            "recent": pairs[-8:][::-1]}


def _sparkline(values: list[float], w: int = 900, h: int = 220, pad: int = 8) -> str:
    if len(values) < 2:
        return '<div class="empty">数据点不足，等下一次快照</div>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or abs(hi) * 0.001 or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (w - 2 * pad) * i / (n - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / span
        pts.append((round(x, 1), round(y, 1)))
    line = " ".join(f"{x},{y}" for x, y in pts)
    area = f"{pad},{h - pad} " + line + f" {pts[-1][0]},{h - pad}"
    up = values[-1] >= values[0]
    color = "#3ddc97" if up else "#ff6b6b"
    zero_y = None
    if lo <= START <= hi:
        zero_y = round(h - pad - (h - 2 * pad) * (START - lo) / span, 1)
    base_line = (
        f'<line x1="{pad}" y1="{zero_y}" x2="{w - pad}" y2="{zero_y}" '
        f'stroke="#5a6b7d" stroke-width="1" stroke-dasharray="4 4"/>'
        if zero_y is not None else ""
    )
    dots = "".join(
        f'<circle cx="{x}" cy="{y}" r="2.5" fill="{color}" opacity="0.9"/>' for x, y in pts[-12:]
    )
    return f'''<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="spark">
  {base_line}
  <polygon points="{area}" fill="{color}" opacity="0.12"/>
  <polyline points="{line}" fill="none" stroke="{color}" stroke-width="2.2"
            stroke-linejoin="round" stroke-linecap="round"/>
  {dots}
</svg>'''


def _kronos_html(k: dict) -> str:
    verdict = ("样本不足", "var(--muted)")
    if k["t"] is not None:
        if abs(k["t"]) < 1.5:
            verdict = ("看不出差别", "var(--muted)")
        elif k["t"] > 0:
            verdict = ("高分组更好", "var(--pos)")
        else:
            verdict = ("高分组更差", "var(--neg)")

    rows = []
    for name, rng, s in (("高分组", ">=0.60", k["high"]),
                         ("中分组", "0.40-0.60", k["mid"]),
                         ("低分组", "<0.40", k["low"])):
        if not s:
            rows.append('<tr><td>' + name + '</td><td class="muted" colspan="3">-</td></tr>')
        else:
            color = "var(--pos)" if s["mean"] >= 0 else "var(--neg)"
            rows.append(
                '<tr><td>' + name + ' <span class="muted" style="font-size:10px">' + rng + '</span></td>'
                + '<td>' + str(s["n"]) + '</td>'
                + '<td style="color:' + color + '">' + f'{s["mean"]:+.4f}' + '</td>'
                + '<td>' + f'{s["win"]:.0f}%' + '</td></tr>')

    if k["t"] is not None:
        t_line = ('高分组 - 低分组 Welch t = ' + f'{k["t"]:+.2f}'
                  + '　（|t|<1.5 视为无差别；每组需 >=20 笔）')
    else:
        t_line = "每组需 >=20 笔才有统计意义；当前样本不足，不下结论。"

    orphan = ('<div class="foot">' + str(k["orphan"])
              + ' 笔平仓发生在 Kronos 接入之前，无系数可配对</div>') if k["orphan"] else ""

    return (
        '<div class="sub" style="margin-bottom:8px">已配对 <b>' + str(k["pairs"])
        + '</b> 笔 · 待平仓 <b>' + str(max(0, k["with_factor"] - k["pairs"])) + '</b> 笔'
        + '　<span style="color:' + verdict[1] + ';font-weight:600">● ' + verdict[0] + '</span></div>'
        + '<table><thead><tr><th>分组</th><th>笔数</th><th>平均盈亏</th><th>胜率</th></tr></thead>'
        + '<tbody>' + "".join(rows) + '</tbody></table>'
        + '<div class="foot" style="margin-top:8px">' + t_line + '</div>'
        + orphan
    )


def build() -> str:
    snaps = [r for r in _read_jsonl(RUNS / "pnl_report.jsonl") if "equity_usdt" in r]
    ledger = _read_jsonl(RUNS / "ledger.jsonl")
    state = json.loads((RUNS / "state.json").read_text(encoding="utf-8")) if (RUNS / "state.json").exists() else {}

    equity = [float(s["equity_usdt"]) for s in snaps]
    last = snaps[-1] if snaps else {"equity_usdt": "n/a", "pnl_usdt": "n/a", "pnl_pct": "n/a",
                                    "ts": "还没有快照", "usdt_free": "n/a", "holdings": []}
    pnl = float(last.get("pnl_usdt") or 0) if snaps else 0.0
    cls = "pos" if pnl >= 0 else "neg"

    trades = [r for r in ledger if r["kind"] in ("bought", "sold", "dust_unsellable",
                                                 "dust_recovered", "order_failed",
                                                 "order_state_unknown", "order_not_found")]
    counts: dict[str, int] = {}
    for r in ledger:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1

    kronos = kronos_stats(ledger)
    positions = state.get("positions", {})
    dust = state.get("dust", {})

    pos_rows = "".join(
        f"<tr><td>{html.escape(s)}</td><td>{html.escape(str(p['qty']))}</td>"
        f"<td>{p['entry_price']}</td><td>{p['stop_price']:.6f}</td>"
        f"<td>{p['tp_price']:.6f}</td></tr>"
        for s, p in positions.items()
    ) or '<tr><td colspan="5" class="muted">空仓</td></tr>'

    dust_rows = "".join(
        f"<tr><td>{html.escape(s)}</td><td>{html.escape(str(d['qty']))}</td>"
        f"<td>{html.escape(str(d['value_usdt']))}</td></tr>"
        for s, d in dust.items()
    )

    def trade_row(r: dict) -> str:
        k = r["kind"]
        label = {"bought": "买入", "sold": "卖出", "dust_unsellable": "灰尘锁死",
                 "dust_recovered": "灰尘回收", "order_failed": "下单失败",
                 "order_state_unknown": "状态未知", "order_not_found": "回查未成交"}.get(k, k)
        sym = r.get("symbol", "")
        detail = ""
        if k == "bought":
            detail = f"{r.get('qty')} @ {r.get('entry')}"
        elif k == "sold":
            detail = f"{r.get('qty')} @ {r.get('exit')} · {r.get('why')} · pnl {r.get('pnl_usdt')}"
        elif k == "dust_unsellable":
            detail = f"{r.get('qty')} · {r.get('value_usdt')} 低于下限"
        elif k == "dust_recovered":
            detail = f"{r.get('qty')} @ {r.get('exit')} · 回收 {r.get('proceeds_usdt')}"
        else:
            detail = str(r.get("error", ""))[:70]
        tone = {"bought": "buy", "sold": "sell", "order_failed": "warn",
                "order_state_unknown": "warn", "order_not_found": "warn"}.get(k, "")
        return (f'<tr class="{tone}"><td class="t">{html.escape(r["ts"][5:16])}</td>'
                f"<td>{label}</td><td>{html.escape(sym)}</td>"
                f"<td class='d'>{html.escape(detail)}</td></tr>")

    trade_rows = "".join(trade_row(r) for r in trades[-14:][::-1]) or \
        '<tr><td colspan="4" class="muted">暂无成交</td></tr>'

    stats = " · ".join(f"{html.escape(k)} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])[:8])

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<!-- 每 60 秒自刷新一次：数据由 bsc-pnl-report 每 5 分钟重建 -->
<title>自动交易 · 盈亏看板</title>
<style>
  :root {{
    --bg:#0e1418; --panel:#151e24; --line:#22303a; --fg:#dbe6ee;
    --muted:#7b8fa0; --pos:#3ddc97; --neg:#ff6b6b; --accent:#4ea8de;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; padding:18px; }}
  h1 {{ font-size:15px; font-weight:600; margin:0 0 2px; letter-spacing:.02em; }}
  .sub {{ color:var(--muted); font-size:12px; margin-bottom:16px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:16px; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
  .card .k {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; }}
  .card .v {{ font-size:20px; font-weight:650; margin-top:4px; font-variant-numeric:tabular-nums; }}
  .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
  .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
            padding:14px; margin-bottom:14px; }}
  .panel h2 {{ font-size:12px; font-weight:600; color:var(--muted); margin:0 0 10px;
               text-transform:uppercase; letter-spacing:.06em; }}
  .spark {{ width:100%; height:180px; display:block; }}
  table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
  th {{ text-align:left; font-size:11px; color:var(--muted); font-weight:500;
        padding:6px 8px; border-bottom:1px solid var(--line); }}
  td {{ padding:7px 8px; border-bottom:1px solid rgba(34,48,58,.55); font-size:13px; }}
  tr:last-child td {{ border-bottom:none; }}
  td.t {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
  td.d {{ color:var(--muted); font-size:12px; }}
  tr.buy td:nth-child(2) {{ color:var(--pos); }}
  tr.sell td:nth-child(2) {{ color:var(--accent); }}
  tr.warn td:nth-child(2) {{ color:#e8b44a; }}
  .muted {{ color:var(--muted); }}
  .empty {{ color:var(--muted); padding:26px; text-align:center; font-size:13px; }}
  .stats {{ color:var(--muted); font-size:11px; line-height:1.7; word-break:break-all; }}
  .foot {{ color:var(--muted); font-size:11px; margin-top:6px; }}
</style></head>
<body>
  <h1>自动交易 · 盈亏看板</h1>
  <div class="sub">最后更新 {html.escape(str(last.get('ts','-')))} · 数据来自本地文件，不联网</div>

  <div class="cards">
    <div class="card"><div class="k">总权益</div>
      <div class="v">{html.escape(str(last.get('equity_usdt','-')))}<span class="muted" style="font-size:12px"> USDT</span></div></div>
    <div class="card"><div class="k">净盈亏</div>
      <div class="v {cls}">{html.escape(str(last.get('pnl_usdt','-')))}</div></div>
    <div class="card"><div class="k">收益率</div>
      <div class="v {cls}">{html.escape(str(last.get('pnl_pct','-')))}%</div></div>
    <div class="card"><div class="k">可用现金</div>
      <div class="v">{html.escape(str(last.get('usdt_free','-')))}</div></div>
  </div>

  <div class="panel">
    <h2>权益曲线（每次快照一个点，虚线为起始 {START}）</h2>
    {_sparkline(equity)}
  </div>

  <div class="panel">
    <h2>当前持仓</h2>
    <table><thead><tr><th>标的</th><th>数量</th><th>成本</th><th>止损</th><th>止盈</th></tr></thead>
    <tbody>{pos_rows}</tbody></table>
    {f'<div class="foot">灰尘：{" · ".join(f"{k} {v["qty"]}（{v["value_usdt"]}）" for k,v in dust.items())}</div>' if dust else ''}
  </div>

  <div class="panel">
    <h2>Kronos 仓位系数成绩单</h2>
    {_kronos_html(kronos)}
  </div>

  <div class="panel">
    <h2>最近的成交与异常</h2>
    <table><thead><tr><th>时间</th><th>类型</th><th>标的</th><th>明细</th></tr></thead>
    <tbody>{trade_rows}</tbody></table>
  </div>

  <div class="panel">
    <h2>运行统计（全部历史）</h2>
    <div class="stats">{stats or '暂无'}</div>
  </div>
</body></html>
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args(argv)
    RUNS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), encoding="utf-8")
    if args.print:
        print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
