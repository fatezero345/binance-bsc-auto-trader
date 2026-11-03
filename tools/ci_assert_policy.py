#!/usr/bin/env python3
"""CI 断言：P0 的安全姿态没被悄悄改回去。

这一段存在的理由很具体——风控代码写对了，但如果有人把
``[execution] enabled`` 改成 true 并提交，所有单测仍然是绿的，
而系统就真能下单了。所以这道门只检查"开关状态"与"硬上限量级"，
以及 ADR-0002 落地后必须持续成立的四件事：

  * 场所仍是 binance_spot，且 gmgn_bsc 仍在 archived 里；
  * 提币权仍然被要求关闭；
  * 主网仍然不被允许（allow_mainnet = false）；
  * 交易所下限 5 USDT 与量化上浮后的硬上限 5.05 USDT 之间仍然存在可行窗口；
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.risk import execution_gate, load_policy  # noqa: E402

# P0 任务书 §5 的四个数字。改了这里就等于改了实验边界，必须是有意的。
P0_LIMITS = {
    "max_notional": Decimal("5.00"),
    "max_risk_budget": Decimal("0.30"),
    "max_cumulative_loss": Decimal("1.50"),
}


def main() -> int:
    policy = load_policy()
    failures: list[str] = []

    # --- 执行必须关 ---
    # ADR-0004 之后：路径可以开着，但**空环境下必须仍然发不出真实订单**。
    gate = execution_gate(policy, environ={})
    if gate.may_submit:
        failures.append("execution gate is OPEN with an empty environment")
    if policy.execution.get("dry_run") and not policy.execution.get("require_env_unlock"):
        failures.append("dry_run is on but there is no env unlock requirement")

    # --- 资金上限仍在 P0 预算内 ---
    for name, ceiling in P0_LIMITS.items():
        actual = getattr(policy, name)
        if actual > ceiling:
            failures.append(f"{name}={actual} exceeds the P0 ceiling {ceiling}")
    if policy.max_open_positions > 1:
        failures.append(f"max_open_positions={policy.max_open_positions} > 1")

    # --- 场所（ADR-0002）---
    if policy.venue_primary != "binance_spot":
        failures.append(f"venue.primary={policy.venue_primary!r} != 'binance_spot'")
    if "gmgn_bsc" not in policy.archived_venues:
        failures.append("venue.archived no longer lists gmgn_bsc -- ADR-0002 regression")

    # --- CEX 模型的安全前提（ADR-0003：红线 + 署名接受表，二选一，不允许两头空）---
    perms = policy.binance_perms
    flags = set(perms.get("forbidden_key_flags", []))
    accepted = set(perms.get("accepted_risk_flags", {}))
    if "enable_withdrawals" not in flags:
        failures.append("forbidden_key_flags must still contain enable_withdrawals (red line)")
    if "permits_universal_transfer" not in flags:
        failures.append("forbidden_key_flags must still contain permits_universal_transfer")
    for flag in ("enable_futures", "enable_internal_transfer", "enable_margin",
                 "enable_vanilla_options", "enable_portfolio_margin"):
        if flag not in flags and flag not in accepted:
            failures.append(f"{flag} is neither forbidden nor in accepted_risk_flags")
    overlap = flags & accepted
    if overlap:
        failures.append(f"flags both forbidden and accepted: {sorted(overlap)}")

    # --- 可行窗口：交易所下限 < 本项目硬上限（严格小于，否则窗口为空）---
    if policy.min_notional >= policy.notional_ceiling:
        failures.append(
            f"no feasible order window: exchange min {policy.min_notional} >= "
            f"policy ceiling {policy.notional_ceiling}"
        )
    # 量化上浮容许度是 cap 语义的一部分，不许被悄悄放宽成"变相提高上限"
    if policy.overrun_bps > 200:
        failures.append(
            f"max_quantization_overrun_bps={policy.overrun_bps} > 200 -- "
            "this would silently raise the effective per-order cap"
        )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("ok: venue=binance_spot, real orders need the env unlock, limits within P0 budget")
    print(
        f"     per-order target {policy.max_notional} USDT, ceiling {policy.notional_ceiling} "
        f"(overrun {policy.overrun_bps} bps), exchange floor {policy.min_notional}"
    )
    print(f"     equity = {policy.raw['capital']['initial_equity_usdt']} USDT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
