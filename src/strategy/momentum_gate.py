"""入场闸：用标的自身的波动率判断「这次下跌算不算数」。

存在的理由：2026-11-03 第一次真实入场决策时，我先用了一个拍脑袋的阈值
（"价格必须在 MA20 上方"），结果 CAKE 比 MA20 低 0.30% 就被拒 —— 而 0.30%
对这条标的（15m 收益标准差 0.258%）根本就是噪音。

于是阈值改为 **由标的自身 σ 推导**，而不是由我想让它通过多少来决定。

这不是一个能赚钱的策略。它只回答一个问题：**现在这个位置，是不是在逆着短期
趋势往下接刀。** 它不提供任何正期望，也不假装提供。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal


MOMENTUM_BARS = 4          # chg_1h 跨 4 根 15m K 线


@dataclass(frozen=True)
class GateVerdict:
    allow: bool
    reason: str
    sigma_15m: float
    ma20: float
    price: float
    dev_ma: float
    chg_1h: float
    tolerance: float

    def summary(self) -> str:
        return (
            f"sigma_15m={self.sigma_15m*100:.3f}% ma20={self.ma20:.6f} "
            f"price={self.price:.6f} dev={self.dev_ma*100:+.3f}% "
            f"chg_1h={self.chg_1h*100:+.3f}% tolerance={self.tolerance*100:.3f}% "
            f"-> {'ALLOW' if self.allow else 'SKIP'} ({self.reason})"
        )


def entry_gate(
    closes: list[float],
    *,
    ma_window: int = 20,
    dev_sigmas: float = 1.0,
    momentum_sigmas: float = 2.0,
) -> GateVerdict:
    """closes 为等间隔收盘价（建议 15m，至少 60 根）。

    拒绝条件（任一成立即 SKIP）：
      1. 偏离 MA 的幅度超过 ``dev_sigmas`` 个 σ（跌得太狠，不接刀）；
         ``dev_sigmas <= 0`` 表示**关闭**这一条（只留急跌保护）；
      2. 近 5 根的总变化跌幅超过 ``momentum_sigmas`` 个 σ（正在快速下坠）。

    参数默认值写死在这里，不随某次决策调整 —— 要改必须改代码并说明理由。
    """
    if len(closes) < max(ma_window + 1, 60):
        raise ValueError(f"need >= {max(ma_window + 1, 60)} closes, got {len(closes)}")

    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    sigma = statistics.pstdev(rets)
    ma = sum(closes[-ma_window:]) / ma_window
    price = closes[-1]
    dev = (price - ma) / ma
    chg_1h = (price - closes[-5]) / closes[-5]

    if sigma == 0:
        return GateVerdict(False, "zero volatility: no basis to judge", sigma, ma, price, dev, chg_1h, 0.0)
    sigma_mom = sigma * math.sqrt(MOMENTUM_BARS)
    # dev_sigmas <= 0 -> 关闭「均线偏离」这一条件，只保留「急跌不接刀」
    # 2026-11-04 所有者决定放宽：慢跌行情下 1σ 会让系统连续 2.5 小时一单不下。
    if dev_sigmas > 0 and dev <= -dev_sigmas * sigma:
        reason = f"deviation {dev*100:.3f}% below -{dev_sigmas}sigma"
        return GateVerdict(False, reason, sigma, ma, price, dev, chg_1h, momentum_sigmas * sigma_mom)
    # ★ 尺度修正（2026-11-04）：sigma 是**单根**K线收益的标准差，
    #   而 chg_1h 跨了 4 根，其标准差约为 sigma*sqrt(4)。
    #   拿 1 小时的变动直接比单根 sigma 会系统性地过度拦截：
    #   实测 BNB 的 1h 跌幅 -0.395%，被当成 "超过 2sigma(0.382%)" 拒掉，
    #   实际 1h 的 2sigma 应为 0.764%。这不是调参，是量纲错误。
    if chg_1h <= -momentum_sigmas * sigma_mom:
        reason = (f"1h drop {chg_1h*100:.3f}% beyond "
                  f"-{momentum_sigmas}sigma_1h({momentum_sigmas*sigma_mom*100:.3f}%)")
        return GateVerdict(False, reason, sigma, ma, price, dev, chg_1h, momentum_sigmas * sigma_mom)
    return GateVerdict(True, "within tolerance", sigma, ma, price, dev, chg_1h,
                       dev_sigmas * sigma if dev_sigmas > 0 else momentum_sigmas * sigma_mom)


def closes_from_klines(klines: list[list]) -> list[float]:
    """Binance /api/v3/klines 的响应 -> 收盘价序列。"""
    return [float(k[4]) for k in klines]
