"""离场规则：止损 / 止盈 / 超时。纯函数，可离线测。

为什么离场必须先于入场写好：
一笔只有开仓没有平仓逻辑的自动交易，本质是把风险敞口交给"下次我再看一眼"。
全自动模式下没有"下次" —— 所以离场规则必须是代码，且必须在第一次买入之前存在。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: str                 # 字符串保存，避免浮点误差写进状态文件
    entry_price: float
    stop_price: float
    tp_price: float
    opened_ts: int           # unix 秒

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "tp_price": self.tp_price,
            "opened_ts": self.opened_ts,
        }

    @staticmethod
    def from_dict(d: dict) -> "Position":
        return Position(
            symbol=d["symbol"],
            qty=str(d["qty"]),
            entry_price=float(d["entry_price"]),
            stop_price=float(d["stop_price"]),
            tp_price=float(d["tp_price"]),
            opened_ts=int(d["opened_ts"]),
        )


def exit_decision(pos: Position, price: float, now_ts: int, max_hold_s: int) -> str | None:
    """返回 'stop' / 'take_profit' / 'max_hold' / None。

    顺序有讲究：**先判止损**。如果同一 tick 里价格既破止损又破止盈（跳空或数据异常），
    按最坏情况处理。
    """
    if price <= pos.stop_price:
        return "stop"
    if price >= pos.tp_price:
        return "take_profit"
    if max_hold_s > 0 and (now_ts - pos.opened_ts) >= max_hold_s:
        return "max_hold"
    return None
