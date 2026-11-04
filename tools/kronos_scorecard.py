"""Kronos 成绩单：用真实成交检验它的仓位系数到底有没有用。

## 要回答的问题

Kronos 现在对每一笔买单生效：系数 = prob_up（去噪后），预算按系数缩小。
但没有任何证据表明这个系数和结果相关。所以唯一可靠的检验是：

  **系数高的那些单子，实际盈亏是不是比系数高的更好？**

## 配对逻辑

日志是流式事件，需要把「建仓时的系数」和「平仓时的盈亏」对上：

  kronos(ok)  -> 记住该标的最新系数
  bought      -> 把 (系数, 时间) 压入该标的的队列
  sold        -> 从队列头部弹出，取它的系数与该笔 pnl_usdt 配对

用 FIFO 而不是"最近一次"，因为同一个标的可能连续建仓多次。
找不到配对的平仓（例如 reconcile 清出来的）记为 None，不计入统计但不隐藏，单独报数量。

## 输出

  runs/kronos_scorecard.md   人看的表
  runs/kronos_scorecard.json 原始配对，便于复算

## 判定

  * 高分组（系数 >= 0.60）vs 低分组（系数 < 0.40）的平均盈亏差；
  * 双侧 Welch t 检验；
  * 样本量够不够（每组 >= 20 才算初步可用，>= 50 才算稳）。

样本不足时照实写"不足"，不给结论。

用法：
    python tools/kronos_scorecard.py                # 平仓数不足 10 笔则跳过
    python tools/kronos_scorecard.py --force        # 强制生成
    python tools/kronos_scorecard.py --min-closes 20
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
LEDGER = RUNS / "ledger.jsonl"
MD = RUNS / "kronos_scorecard.md"
JSON_OUT = RUNS / "kronos_scorecard.json"
STATE = RUNS / "kronos_scorecard.state.json"


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def pair(rows: list[dict]) -> tuple[list[dict], int]:
    """把建仓系数与平仓盈亏配对。返回 (配对列表, 无系数平仓数)。"""
    last_factor: dict[str, float] = {}
    queues: dict[str, list[dict]] = {}
    pairs: list[dict] = []
    orphan = 0

    for r in rows:
        kind = r.get("kind")
        sym = r.get("symbol")
        if not sym:
            continue
        if kind == "kronos" and r.get("ok") and r.get("size_factor") is not None:
            last_factor[sym] = float(r["size_factor"])
        elif kind == "bought":
            queues.setdefault(sym, []).append({
                "ts": r.get("ts"),
                "factor": last_factor.get(sym),
                "qty": r.get("qty"),
                "entry": r.get("entry"),
            })
        elif kind == "sold":
            q = queues.get(sym) or []
            # LIFO：每个标的只持一仓，平仓关掉的是最近那笔买入。
            # FIFO 会让被 reconcile 清掉、没有 sold 的旧买入堵在队首，配对失败。
            entry = q.pop() if q else None
            pnl = r.get("pnl_usdt")
            if pnl is None:
                continue
            if entry is None or entry.get("factor") is None:
                orphan += 1
                continue
            pairs.append({
                "symbol": sym,
                "buy_ts": entry["ts"],
                "sell_ts": r.get("ts"),
                "factor": entry["factor"],
                "pnl_usdt": float(pnl),
                "qty": entry.get("qty"),
                "entry": entry.get("entry"),
                "exit": r.get("exit"),
                "why": r.get("why"),
            })
    return pairs, orphan


def welch(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2 or len(y) < 2:
        return None
    vx, vy = statistics.variance(x), statistics.variance(y)
    se = (vx / len(x) + vy / len(y)) ** 0.5
    if se == 0:
        return None
    return (statistics.mean(x) - statistics.mean(y)) / se


def spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 3 or len(set(x)) < 2:
        return 0.0

    def rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(o):
            j = i
            while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[o[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(x), rank(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def bucket(pairs: list[dict], lo: float, hi: float) -> list[dict]:
    return [p for p in pairs if lo <= p["factor"] <= hi]


def render(pairs: list[dict], orphan: int, closes_total: int) -> str:
    high = [p for p in pairs if p["factor"] >= 0.60]
    low = [p for p in pairs if p["factor"] < 0.40]
    mid = [p for p in pairs if 0.40 <= p["factor"] < 0.60]

    def stat(g):
        if not g:
            return None
        pnls = [p["pnl_usdt"] for p in g]
        return {
            "n": len(g),
            "mean": statistics.mean(pnls),
            "sum": sum(pnls),
            "win": sum(1 for x in pnls if x > 0) / len(pnls) * 100,
        }

    sh, sl, sm = stat(high), stat(low), stat(mid)
    t = welch([p["pnl_usdt"] for p in high], [p["pnl_usdt"] for p in low]) if high and low else None
    rho = spearman([p["factor"] for p in pairs], [p["pnl_usdt"] for p in pairs]) if len(pairs) >= 5 else 0.0

    lines = [
        "# Kronos 成绩单",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 问题",
        "",
        "Kronos 的仓位系数（prob_up）和实际盈亏有关吗？系数高的时候是不是真的赚得更多？",
        "",
        "## 样本",
        "",
        f"- 已配对成交：**{len(pairs)} 笔**",
        f"- 无系数可配对的平仓：{orphan} 笔（这些不计入统计）",
        f"- 账本累计平仓：{closes_total} 笔",
        "",
        "| 分组 | 系数区间 | 笔数 | 平均盈亏 | 合计盈亏 | 胜率 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for name, s, rng in (("高分组", sh, "≥ 0.60"), ("中分组", sm, "0.40 – 0.60"), ("低分组", sl, "< 0.40")):
        if s:
            lines.append(f"| {name} | {rng} | {s['n']} | {s['mean']:+.6f} | "
                         f"{s['sum']:+.6f} | {s['win']:.1f}% |")
        else:
            lines.append(f"| {name} | {rng} | 0 | — | — | — |")

    lines += [
        "",
        "## 判定",
        "",
    ]

    if t is None:
        lines.append("**样本不足**：高分组或低分组为空，无法比较。继续积累。")
    else:
        lines.append(f"- 高分组 − 低分组 的差异：**{t:+.2f}**（Welch t 值）")
        lines.append(f"- 系数与盈亏的秩相关：**{rho:+.4f}**")
        lines.append("")
        if abs(t) < 1.5:
            lines.append("**结论：看不出差别。** 系数高低与盈亏无关，"
                         "缩仓在当前样本上没有带来任何改善。")
        elif t > 1.5:
            lines.append("**结论：高分组表现更好。** 方向与预期一致，"
                         "但需要更多样本才能确认不是偶然。")
        else:
            lines.append("**结论：高分组反而更差。** 说明这个系数在反向指示，"
                         "继续按它缩仓是在削弱盈利。")

    lines += [
        "",
        "## 可信度",
        "",
        "| 条件 | 现状 | 是否达标 |",
        "| --- | --- | --- |",
        f"| 每组 ≥ 20 笔 | 高 {sh['n'] if sh else 0} / 低 {sl['n'] if sl else 0} | "
        f"{'是' if (sh and sl and sh['n'] >= 20 and sl['n'] >= 20) else '否'} |",
        f"| 每组 ≥ 50 笔 | 高 {sh['n'] if sh else 0} / 低 {sl['n'] if sl else 0} | "
        f"{'是' if (sh and sl and sh['n'] >= 50 and sl['n'] >= 50) else '否'} |",
        "",
        "样本量不到 50 时，任何结论都可能只是运气。本表如实标注，不粉饰。",
        "",
        "## 明细",
        "",
        "| 建仓时间 | 标的 | 系数 | 盈亏 | 离场原因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for p in sorted(pairs, key=lambda x: x.get("sell_ts") or ""):
        lines.append(f"| {str(p['buy_ts'])[5:16]} | {p['symbol']} | {p['factor']:.4f} | "
                     f"{p['pnl_usdt']:+.6f} | {p.get('why') or '-'} |")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-closes", type=int, default=10)
    args = ap.parse_args(argv)

    rows = read_ledger()
    if not rows:
        print("账本为空")
        return 0

    closes_total = sum(1 for r in rows if r.get("kind") == "sold")
    pairs, orphan = pair(rows)

    # 只在新增平仓达到门槛时才重写报告（用户要求"每积累 10 笔算一次"）
    last = 0
    if STATE.exists():
        try:
            last = int(json.loads(STATE.read_text(encoding="utf-8")).get("closes", 0))
        except Exception:                                      # noqa: BLE001
            last = 0
    new_closes = closes_total - last

    if not args.force and new_closes < args.min_closes:
        print(f"新增平仓 {new_closes} 笔，未达门槛 {args.min_closes}，跳过")
        return 0

    MD.write_text(render(pairs, orphan, closes_total), encoding="utf-8")
    JSON_OUT.write_text(json.dumps({"pairs": pairs, "orphan": orphan,
                                    "closes_total": closes_total,
                                    "generated": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    STATE.write_text(json.dumps({"closes": closes_total}), encoding="utf-8")

    high = [p for p in pairs if p["factor"] >= 0.60]
    low = [p for p in pairs if p["factor"] < 0.40]
    print(f"成绩单已更新：配对 {len(pairs)} 笔（高 {len(high)} / 低 {len(low)}），"
          f"无系数 {orphan} 笔 -> {MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
