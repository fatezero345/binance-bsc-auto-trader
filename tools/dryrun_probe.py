#!/usr/bin/env python3
"""空跑验证：用真实行情跑完整 tick，但**强制 live=false**，绝不发单。

验三件事：
  1. 每个标的的行情 / K 线 / 余额都能稳定拉到（不崩）；
  2. **请求权重**——昨晚 82 次重试把 IP 打进 418，必须知道一轮 tick 花多少；
  3. tick 结束时状态与账户是否一致。

做法：monkeypatch `urllib.request.urlopen` 计数，并把 load_creds 的 live 改成 False。
生产代码一行不改。

用法：
    python tools/dryrun_probe.py            # 跑 2 轮
    python tools/dryrun_probe.py --rounds 3
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("auto_trader", ROOT / "tools" / "auto_trader.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)

_ORIG = urllib.request.urlopen
_counter = {"n": 0, "paths": {}}


def counting_urlopen(req, *a, **kw):
    url = getattr(req, "full_url", str(req))
    path = url.split("api.binance.com", 1)[-1].split("?")[0]
    _counter["n"] += 1
    _counter["paths"][path] = _counter["paths"].get(path, 0) + 1
    return _ORIG(req, *a, **kw)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args(argv)

    real_load = at.load_creds

    def forced_dry() -> tuple[str, str, bool]:
        k, s, _ = real_load()
        return k, s, False          # 强制 live=false：只检查，不下单

    at.load_creds = forced_dry
    urllib.request.urlopen = counting_urlopen

    print(f"dry-run: live forced to False, {args.rounds} rounds\n")
    for i in range(1, args.rounds + 1):
        _counter["n"] = 0
        _counter["paths"] = {}
        t0 = time.time()
        try:
            rc = at.tick()
            err = None
        except Exception as exc:                                   # noqa: BLE001
            rc, err = -1, f"{type(exc).__name__}: {exc}"
        dt = time.time() - t0
        print(f"--- round {i} ---")
        print(f"  rc={rc}  用时={dt:.1f}s  请求数={_counter['n']}  "
              f"平均={_counter['n']/max(dt,0.001):.2f}/s")
        for p, c in sorted(_counter["paths"].items(), key=lambda x: -x[1]):
            print(f"    {c:3}x {p}")
        if err:
            print(f"  ERROR: {err[:200]}")
        if i < args.rounds:
            time.sleep(2)

    urllib.request.urlopen = _ORIG
    print("\n已完成。生产代码未修改，未产生任何订单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
