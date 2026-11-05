#!/usr/bin/env python3
"""相对链接检查。

审计文档最隐蔽的失效方式不是写错数字，而是**引用了一个不存在的文件**——
读者点进去发现 404，整份报告的可信度就没了。CI 里跑这个。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SKIP = {".git", "node_modules", "__pycache__"}


def iter_markdown() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*.md"):
        if any(part in SKIP for part in p.parts):
            continue
        out.append(p)
    return sorted(out)


def main() -> int:
    bad: list[str] = []
    checked = 0
    for md in iter_markdown():
        text = md.read_text(encoding="utf-8")
        for raw in LINK.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target:
                continue                      # 纯锚点
            if "://" in target or target.startswith(("mailto:", "tel:")):
                continue                      # 外链不在此检查范围
            checked += 1
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                bad.append(f"{md.relative_to(ROOT)} -> {target}")

    if bad:
        print("broken relative links:")
        for b in bad:
            print(f"  - {b}")
        return 1

    print(f"ok: {checked} relative links resolve across {len(iter_markdown())} markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
