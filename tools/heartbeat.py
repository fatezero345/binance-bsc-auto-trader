"""心跳自检：交易器「活着但不干活」时把它救回来。

## 为什么需要它

现有的 run_daemon.vbs 只检查「进程在不在」。但有两种故障它看不见：

1. **进程在，但被系统挂起**（Windows S0 现代待机会节流后台程序；
   笔记本合盖、切到电池、内存压力都可能让 pythonw 停摆）。
   这时进程还在，看门狗认为一切正常，但实际上一轮 tick 都没跑。
2. **进程在，但卡在网络调用里**（没有超时的 socket）。

这两种情况对交易都是致命的：**仓位没有人管，止损止盈不会执行**。

## 判据

不看进程，看**账本**。账本是唯一能证明「它真的干了活」的东西。

  now - 最后一条账本事件 > STALE_SECONDS  ->  认为停摆

## 动作

  * 停摆且进程存在 -> 杀掉（它已经卡住，留着没用）-> 重新拉起
  * 停摆且进程不存在 -> 直接拉起
  * 没停摆 -> 什么都不做

每一步都写进账本（heartbeat_ok / heartbeat_stale / heartbeat_restart），
这样事后能查出「什么时候停摆过、停了多久」。

## 为什么用 pythonw 跑

无窗口。调度器每 5 分钟用 wscript 拉起它，不会弹黑框。

用法：
    pythonw tools/heartbeat.py
    python tools/heartbeat.py --check-only     # 只报告，不动手
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "runs" / "ledger.jsonl"
RUN_DAEMON = ROOT / "tools" / "run_daemon.vbs"

STALE_SECONDS = 900          # 15 分钟没有新事件就算停摆
CREATE_NO_WINDOW = 0x08000000


def log(kind: str, **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind}
    rec.update(fields)
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def last_event_seconds() -> float | None:
    """距最后一条账本事件多少秒。账本不存在返回 None。"""
    if not LEDGER.exists():
        return None
    try:
        with LEDGER.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    for line in reversed(lines[-200:]):
        line = line.strip()
        if not line:
            continue
        try:
            ts = json.loads(line).get("ts")
            if not ts:
                continue
            dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            return time.time() - dt.timestamp()
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def daemon_pids() -> list[int]:
    """找出正在跑 auto_trader.py 的 pythonw 进程。"""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*auto_trader.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:                                        # noqa: BLE001
        return []


def start_daemon() -> bool:
    try:
        subprocess.Popen(
            ["wscript.exe", "//nologo", str(RUN_DAEMON)],
            cwd=str(ROOT), creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:                                        # noqa: BLE001
        return False


def kill(pids: list[int]) -> None:
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:                                    # noqa: BLE001
            pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--stale", type=int, default=STALE_SECONDS)
    args = ap.parse_args(argv)

    stale = last_event_seconds()
    pids = daemon_pids()

    if stale is None:
        state = "no_ledger"
    elif stale > args.stale:
        state = "stale"
    else:
        state = "healthy"

    summary = {
        "state": state,
        "idle_s": None if stale is None else int(stale),
        "pids": pids,
    }

    if args.check_only:
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    # 判据优先级：先看进程在不在，再看它干没干活。
    # 只看账本会漏掉「进程刚死、账本还新鲜」的情况；
    # 只看进程会漏掉「进程活着但被挂起」。两个一起看才完整。
    if not pids:
        ok = start_daemon()
        log("heartbeat_restart", started=ok, process_found=False,
            idle_s=None if stale is None else int(stale),
            note="进程不存在，直接拉起")
        summary["restarted"] = ok
        summary["state"] = "no_process"
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    if state == "stale":
        log("heartbeat_stale", idle_s=int(stale), pids=pids,
            note="进程在但账本超过阈值没有新事件，判定挂起")
        kill(pids)
        log("heartbeat_kill", pids=pids, note="卡住的进程已终止")
        time.sleep(2)
        ok = start_daemon()
        log("heartbeat_restart", started=ok, process_found=True,
            idle_s=int(stale), note="已重新拉起交易器")
        summary["restarted"] = ok
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    # 进程在、账本新鲜 -> 健康，不写账本，避免心跳本身变成噪音
    print(json.dumps(summary, ensure_ascii=False))
    return 0

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
