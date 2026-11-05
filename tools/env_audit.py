#!/usr/bin/env python3
"""P0 environment audit, re-runnable.

Read-only by construction: this script only calls the read side of ``gmgn-cli``
(``portfolio info`` / ``token info`` / ``gas-price``) plus the local toolchain.
It never calls ``swap``, never signs, never reads a private key or the API key
value.

Usage
-----
    python tools/env_audit.py              # print report
    python tools/env_audit.py --write      # also refresh docs/evidence/raw/*

Exit code is always 0 unless the script itself crashes: an audit that finds
problems is a successful audit.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "docs" / "evidence" / "raw"

# P0 审计过的三条标的；address -> (label, quote token for the implied-price check)
TARGETS = {
    "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82": "CAKE",
    "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c": "BTCB",
    "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c": "WBNB",
}
QUOTE_IS_WBNB = {
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
}
USDT_BSC = "0x55d398326f99059ff775485246999027b3197955"
DEVIATION_WARN_PCT = Decimal("0.5")


def _resolve(tool: str) -> list[str] | None:
    """Resolve an executable to an argv prefix.

    On Windows ``npm`` / ``gmgn-cli`` are ``.cmd`` shims, and CreateProcess does
    not resolve PATHEXT for those -- ``subprocess.run(["gmgn-cli", ...])`` raises
    FileNotFoundError. Route them through ``cmd /c`` explicitly.
    """
    path = shutil.which(tool)
    if not path:
        return None
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", path]
    return [path]


def run(cmd: list[str], timeout: int = 45) -> tuple[int, str]:
    """Run a command, return (rc, output). Never raises on non-zero."""
    resolved = _resolve(cmd[0])
    if resolved is None:
        return 127, f"{cmd[0]}: not found"
    argv = [*resolved, *cmd[1:]]
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timeout after {timeout}s"


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 68 - len(title)))


def audit_toolchain() -> str:
    lines = [f"host_os = {platform.system()} {platform.release()} ({platform.version()})"]
    for tool in ("node", "npm", "python", "git", "gh", "gmgn-cli"):
        rc, out = run([tool, "--version"] if tool != "python" else ["python", "--version"])
        if rc == 0:
            lines.append(f"{tool:10s} = {out.strip().splitlines()[0]}")
        else:
            lines.append(f"{tool:10s} = (missing)")
    if shutil.which("wsl"):
        rc, out = run(["wsl", "-e", "bash", "-lc", "uname -srm; python3 --version; node -v"])
        if rc == 0:
            for ln in out.strip().splitlines():
                # WSL 会把每条 shell 警告回显成 "wsl: ..."；那不是审计事实
                if ln.startswith("wsl:"):
                    continue
                lines.append(f"wsl        : {ln}")
    return "\n".join(lines)


def gmgn_json(args: list[str]) -> tuple[bool, object]:
    rc, out = run(["gmgn-cli", *args])
    if rc != 0:
        return False, out.strip()
    try:
        return True, json.loads(out)
    except json.JSONDecodeError:
        return False, out.strip()[:400]


def audit_wallet() -> tuple[str, object]:
    ok, data = gmgn_json(["portfolio", "info", "--raw"])
    if not ok:
        return f"portfolio info FAILED: {data}", None
    wallets = data.get("wallets", []) if isinstance(data, dict) else None
    if wallets is None:
        return f"unexpected shape: {data}", data
    if not wallets:
        return "BLOCKED: no wallet bound to this API key ({\"wallets\":[]})", data
    return f"{len(wallets)} wallet(s) bound", data


def audit_gas() -> tuple[str, object]:
    ok, data = gmgn_json(["gas-price", "--chain", "bsc"])
    if not ok:
        return f"gas-price FAILED: {data}", None
    if not isinstance(data, dict):
        return f"unexpected shape: {data}", data

    def gwei(v) -> str:
        try:
            return f"{Decimal(str(v)) / Decimal(10**9):.3f} gwei"
        except Exception:
            return f"unparsable({v!r})"

    bnb = data.get("native_token_usd_price")
    out = [
        f"high    = {data.get('high')} wei = {gwei(data.get('high'))}",
        f"average = {data.get('average')} wei = {gwei(data.get('average'))}",
        f"low     = {data.get('low')} wei = {gwei(data.get('low'))}",
        f"BNB/USD = {bnb}",
    ]
    if data.get("last_block") in (0, 1, None):
        out.append(
            f"WARNING: last_block={data.get('last_block')!r} is implausible for BSC "
            "mainnet -> do not use this field"
        )
    return "\n".join(out), data


def audit_token(address: str, label: str, bnb_usd: Decimal | None) -> tuple[str, object]:
    ok, data = gmgn_json(["token", "info", "--chain", "bsc", "--address", address])
    if not ok:
        return f"{label}: token info FAILED: {data}", None
    if not isinstance(data, dict):
        return f"{label}: unexpected shape", data

    price = data.get("price") or {}
    pool = data.get("pool") or {}
    reported = price.get("price")
    quote_sym = pool.get("quote_symbol")
    base_r = pool.get("base_reserve")
    quote_r = pool.get("quote_reserve")

    lines = [
        f"{label:5s} reported_price = {reported}",
        f"{'':5s} liquidity      = {data.get('liquidity')}",
        f"{'':5s} pool           = {pool.get('exchange')} ({quote_sym}) "
        f"base={base_r} quote={quote_r}",
        f"{'':5s} holders        = {data.get('holder_count')}",
        f"{'':5s} swaps_24h      = {price.get('swaps_24h')} "
        f"(buys {price.get('buys_24h')} / sells {price.get('sells_24h')})",
    ]

    # 交叉验算：用主池 reserve 反推价，与 reported price 对比。
    # 这是 P0 发现 WBNB 报价虚高 9% 的方法，固化在这里。
    try:
        base = Decimal(str(base_r))
        quote = Decimal(str(quote_r))
        if base <= 0 or quote <= 0:
            raise ValueError("empty reserves")
        if quote_sym == "WBNB" and bnb_usd:
            # quote 是 WBNB -> 反推 base 的 USD 价
            implied = (quote / base) * bnb_usd
            basis = f"(quote=WBNB, BNB={bnb_usd})"
        elif quote_sym == "USDT" or (pool.get("quote_address") or "").lower() == USDT_BSC:
            implied = quote / base
            basis = "(quote=USDT)"
        else:
            implied = None
            basis = f"(unsupported quote {quote_sym})"

        if implied is not None and reported:
            rep = Decimal(str(reported))
            if rep > 0:
                dev = abs(implied - rep) / rep * 100
                flag = "OK" if dev <= DEVIATION_WARN_PCT else "!! DEVIATION"
                lines.append(
                    f"{'':5s} implied_price  = {implied:.8f} {basis}  "
                    f"dev={dev:.3f}%  {flag}"
                )
                if dev > DEVIATION_WARN_PCT:
                    lines.append(
                        f"{'':5s}   -> reported price is NOT trustworthy; "
                        "use pool reserves / an independent source"
                    )
    except Exception as exc:  # noqa: BLE001 - audit must not crash on dirty data
        lines.append(f"{'':5s} implied_price  = (could not compute: {exc})")

    return "\n".join(lines), data


def main(argv: list[str]) -> int:
    write = "--write" in argv
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"P0 environment audit @ {ts}")

    section("toolchain")
    tool_text = audit_toolchain()
    print(tool_text)

    section("wallet (read-only)")
    wallet_text, wallet_raw = audit_wallet()
    print(wallet_text)

    section("BSC gas")
    gas_text, gas_raw = audit_gas()
    print(gas_text)

    bnb_usd = None
    if isinstance(gas_raw, dict) and gas_raw.get("native_token_usd_price"):
        bnb_usd = Decimal(str(gas_raw["native_token_usd_price"]))

    section("targets (live, cross-checked)")
    token_texts: dict[str, str] = {}
    token_raws: dict[str, object] = {}
    for addr, label in TARGETS.items():
        text, raw = audit_token(addr, label, bnb_usd)
        token_texts[label] = text
        token_raws[label] = raw
        print(text)
        print()

    if write:
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / "env.txt").write_text(f"# env-audit @ {ts}\n{tool_text}\n", encoding="utf-8")

        # 审计卫生：一次失败的调用绝不能覆盖一份成功的证据。
        # 已经存在的证据默认也不覆盖——覆盖会让历史快照消失，
        # 而"当时的市场长什么样"正是本目录存在的理由。要归档请换文件名。
        force = "--force" in argv
        wrote, skipped, kept = [], [], []
        for name, payload in [
            ("gmgn_portfolio_info.json", wallet_raw),
            ("bsc_gas_price.json", gas_raw),
            *((f"{label.lower()}_token_info.json", raw) for label, raw in token_raws.items()),
        ]:
            dest = RAW / name
            if payload is None or isinstance(payload, str):
                skipped.append(name)          # 调用失败，保留旧文件
                continue
            if dest.exists() and not force:
                kept.append(name)
                continue
            dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            wrote.append(name)
        print(f"wrote evidence to {RAW}: {', '.join(wrote) or '(nothing)'}")
        if kept:
            print(f"left untouched (already exists, use --force to replace): {', '.join(kept)}")
        if skipped:
            print(f"skipped (call failed, existing files left untouched): {', '.join(skipped)}")

    section("verdict")
    if wallet_raw in (None, {"wallets": []}):
        print("wallet  : BLOCKED (B2) - nothing bound to the API key")
    else:
        print("wallet  : bound")
    print("trading : DISABLED (config/risk_policy.toml [execution])")
    print("note    : this audit performs read-only calls only; no swap was attempted")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
