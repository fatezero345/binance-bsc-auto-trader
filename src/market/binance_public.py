"""Binance 现货公开行情客户端（无需鉴权）。

只做三件事：服务器时间、交易对过滤器、24h 行情。
不碰账户、不碰订单——那是 wallet / execution 的职责。

实现约束：std/库 only（urllib + json），与仓库其余部分保持零第三方依赖。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.risk.models import SymbolFilters

DEFAULT_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"
USER_AGENT = "binance-bsc-auto-trader/p0"


class MarketError(RuntimeError):
    pass


def _request(
    path: str, params: dict[str, Any] | None = None, *, base: str = DEFAULT_BASE, timeout: int = 15
) -> Any:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise MarketError(f"{path} -> HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MarketError(f"{path} -> unreachable: {exc}") from exc


def server_time(*, base: str = DEFAULT_BASE) -> int:
    return int(_request("/api/v3/time", base=base)["serverTime"])


def clock_skew_ms(local_ms: int, *, base: str = DEFAULT_BASE) -> int:
    """正数表示本机时钟快于服务器。Binance 的 recvWindow 对偏差敏感。"""
    return local_ms - server_time(base=base)


def _decimal(value: str | None, default: str = "0") -> Decimal:
    return Decimal(value if value not in (None, "") else default)


def parse_filters(payload: dict) -> dict[str, SymbolFilters]:
    out: dict[str, SymbolFilters] = {}
    for sym in payload.get("symbols", []):
        filters = {f["filterType"]: f for f in sym.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        tick = filters.get("PRICE_FILTER", {})
        # NOTIONAL 是现行名字；MIN_NOTIONAL 是旧名，两者都兼容
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        out[sym["symbol"]] = SymbolFilters(
            symbol=sym["symbol"],
            base_asset=sym["baseAsset"],
            quote_asset=sym["quoteAsset"],
            step_size=_decimal(lot.get("stepSize"), "0.00000001"),
            min_qty=_decimal(lot.get("minQty"), "0.00000001"),
            tick_size=_decimal(tick.get("tickSize"), "0.00000001"),
            min_notional=_decimal(notional.get("minNotional") or notional.get("notional"), "5"),
        )
    return out


def symbol_filters(symbols: list[str], *, base: str = DEFAULT_BASE) -> dict[str, SymbolFilters]:
    payload = _request(
        "/api/v3/exchangeInfo",
        {"symbols": json.dumps(symbols, separators=(",", ":"))},
        base=base,
    )
    return parse_filters(payload)


def ticker_24h(symbols: list[str], *, base: str = DEFAULT_BASE) -> dict[str, dict]:
    payload = _request(
        "/api/v3/ticker/24hr",
        {"symbols": json.dumps(symbols, separators=(",", ":"))},
        base=base,
    )
    return {row["symbol"]: row for row in payload}


def last_price(symbol: str, *, base: str = DEFAULT_BASE) -> Decimal:
    payload = _request("/api/v3/ticker/price", {"symbol": symbol}, base=base)
    return Decimal(payload["price"])


@dataclass(frozen=True)
class MarketSnapshot:
    """P1 快照层的形状（本阶段只定义，不调度）。"""

    symbol: str
    price: Decimal
    filters: SymbolFilters
    quote_volume_24h: Decimal


def snapshot(symbol: str, *, base: str = DEFAULT_BASE) -> MarketSnapshot:
    f = symbol_filters([symbol], base=base)[symbol]
    t = ticker_24h([symbol], base=base)[symbol]
    return MarketSnapshot(
        symbol=symbol,
        price=Decimal(t["lastPrice"]),
        filters=f,
        quote_volume_24h=Decimal(t["quoteVolume"]),
    )
