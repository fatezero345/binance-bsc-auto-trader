"""把插件源码同步到 DSH 的运行时副本。

## 为什么需要它

DSH 的插件加载路径是 `~/.dsh/profiles/web/node_modules/<name>`，
而开发副本在 `~/.dsh/plugins/<name>`。Windows 上 `file:` 依赖是**复制**而非软链，
所以改了开发副本之后必须再复制一次运行时副本才会生效。

问题是：**DSH 运行期间这两个文件是被占用的，复制会失败**
（`IOException: 文件正由另一进程使用`）。这个坑已经踩过三次，
每次都要人工在「关掉 DSH → 复制 → 重开」之间走一遍，很容易漏。

## 做法

每 5 分钟比对两边的哈希：

   相同   -> 什么都不做，退出
   不同   -> 尝试复制
             成功 -> 记日志（DSH 下次重启即生效，或者热重载直接生效）
             失败 -> 记「被占用」，下轮再试

DSH 关掉的那一刻锁就释放了，下一轮 5 分钟内会自动补上。
用户不需要记住任何手工步骤。

用法：
    pythonw tools/sync_plugin.py
    python tools/sync_plugin.py --check    # 只报告是否需要同步
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from pathlib import Path

SRC = Path.home() / ".dsh" / "plugins" / "dsh-pnl-panel"
DST = Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / "dsh-pnl-panel"
FILES = ("index.js", "client.js", "package.json", "cordis.patch.yml")


def digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    if not SRC.exists() or not DST.exists():
        print(f"skip: src={SRC.exists()} dst={DST.exists()}")
        return 0

    stale = [f for f in FILES
             if (SRC / f).exists() and digest(SRC / f) != digest(DST / f)]

    if not stale:
        print("in sync")
        return 0

    if args.check:
        print("stale:", ", ".join(stale))
        return 0

    done, failed = [], []
    for f in stale:
        try:
            shutil.copy2(SRC / f, DST / f)
            done.append(f)
        except OSError as exc:
            failed.append(f"{f}({type(exc).__name__})")

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    if done:
        # 只有真的写成功才记，避免刷屏
        try:
            log = Path(__file__).resolve().parents[1] / "runs" / "plugin_sync.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} synced={done} pending={failed}\n")
        except OSError:
            pass

    print(f"synced={done} pending={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
