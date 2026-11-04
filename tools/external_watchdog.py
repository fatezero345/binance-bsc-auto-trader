"""外部看护：从 WSL 监控 Windows 侧的自动交易系统。

## 为什么需要它

Windows 侧的看护链是这个样子的：

    bsc-auto-trader (计划任务)  ->  拉起/重启  pythonw auto_trader.py
                                ->  启用/补跑  两个汇报任务

但**这条链自己没有人看**：如果 `bsc-auto-trader` 被禁用，
或者 wscript/计划任务服务本身出问题，整条链就断了，
而断了之后没有任何东西会告诉你。

本机内部的"自我监控"解决不了这个问题——它自己死了就没人叫醒它。
所以这里换一个**独立的运行时**（WSL）来盯 Windows：

    WSL 和 Windows 是两个独立的 Linux/Windows 内核实例。
    Windows 侧的调度器全挂，WSL 里的 cron 照样跑。

## 判据

不看进程，看**账本文件的修改时间**。

    账本在 <20 分钟内有更新  -> 系统活着
    超过 20 分钟没更新        -> 认为 Windows 侧停摆，尝试恢复

账本是唯一能证明"它真的在干活"的证据：每轮 tick 都会追加。

## 恢复动作

    1. schtasks /change /enable     防止任务被禁用（已踩过一次）
    2. schtasks /run                立即触发一次心跳任务
    3. 写日志到 runs/external_watchdog.log（Windows 侧也能看到）

不直接去 kill/start pythonw —— 那是 Windows 侧心跳的职责。
这里只负责"把看门狗本身踹一脚"，职责不重叠。

## 已知边界

如果 WSL 也停了（`wsl --shutdown`、机器重启后没进过 WSL），
本脚本同样不会跑。真正的兜底需要第三处（比如云端定时任务）。
这一点不隐瞒。

用法（在 WSL 里）：
    python3 tools/external_watchdog.py                  # 检查并按需恢复
    python3 tools/external_watchdog.py --check-only     # 只看，不动手
    python3 tools/external_watchdog.py --threshold 60   # 测试用

crontab：
    */10 * * * * /usr/bin/python3 <repo>/tools/external_watchdog.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "runs" / "ledger.jsonl"
LOGFILE = REPO / "runs" / "external_watchdog.log"

TASK = "bsc-auto-trader"
SCHTASKS = "/mnt/c/Windows/System32/schtasks.exe"
DEFAULT_THRESHOLD = 1200        # 20 分钟


def log(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        with LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass


def ledger_age() -> float | None:
    try:
        return time.time() - LEDGER.stat().st_mtime
    except OSError:
        return None


def schtasks(*args: str, timeout: int = 30) -> tuple[int, str]:
    """调用 Windows 的 schtasks。输出是 GBK，按替换解码。"""
    try:
        p = subprocess.run([SCHTASKS, *args], capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("cp936", "replace") + (p.stderr or b"").decode("cp936", "replace")
        return p.returncode, out.strip()
    except Exception as exc:                                 # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def task_disabled() -> bool | None:
    rc, out = schtasks("/query", "/tn", TASK, "/fo", "LIST")
    if rc != 0:
        return None
    low = out.lower()
    # 中文系统输出「状态: 已禁用」，英文是 "Status: Disabled"
    if "已禁用" in out or "disabled" in low:
        return True
    return False


def recover(check_only: bool) -> dict:
    actions: list[str] = []
    disabled = task_disabled()
    if disabled:
        if check_only:
            actions.append("would-enable")
        else:
            rc, out = schtasks("/change", "/tn", TASK, "/enable")
            actions.append(f"enable:rc={rc}")
            log(f"ACTION enable {TASK} rc={rc} {out[:120]}")
    if not check_only:
        rc, out = schtasks("/run", "/tn", TASK)
        actions.append(f"run:rc={rc}")
        log(f"ACTION run {TASK} rc={rc} {out[:120]}")
    else:
        actions.append("would-run")
    return {"disabled": disabled, "actions": actions}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args(argv)

    age = ledger_age()
    if age is None:
        log("ALERT 账本不存在或不可读")
        print("ledger missing")
        return 1

    if age <= args.threshold:
        # 健康时保持沉默，避免 cron 日志噪音
        print(f"ok age={int(age)}s")
        return 0

    log(f"ALERT 账本 {int(age)}s 未更新（阈值 {args.threshold}s），尝试恢复")
    result = recover(args.check_only)
    log(f"RESULT {result}")
    print(f"stale age={int(age)}s -> {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
