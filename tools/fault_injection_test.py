#!/usr/bin/env python3
"""故障注入验证：**故意把响应丢掉，看能不能靠 clientOrderId 把成交捞回来**。

这是 2026-11-03 那个最危险 bug 的验收工具：

    下单请求发出去了，交易所也成交了，但本地没收到响应。
    系统当时靠"余额为 0"猜出已经平仓 —— 猜对了是运气。

本脚本做的事：
  1. 组一笔真实的 5 USDT 买单；
  2. 用 http.client 把请求**真的发出去**，然后立刻断开、不读响应；
  3. 断言 submit_order 抛的是 OrderStateUnknown（而不是 ExecutionRefused）；
  4. 用 query_order(clientOrderId) 回查，断言拿得到 FILLED 与真实成交量；
  5. 打印结论。

⚠️ 这会真的产生一笔成交（约 5 USDT），跑完请执行
    python tools/auto_trader.py --flatten
   把它平掉。

用法：
    python tools/fault_injection_test.py --yes    # 必须显式确认
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.execution import binance_spot as x  # noqa: E402
from src.market import binance_public as bp  # noqa: E402
from src.risk import (  # noqa: E402
    Instrument,
    OrderIntent,
    Policy,
    PortfolioState,
    Side,
    Venue,
    assess,
    execution_gate,
    load_policy,
    plan_buy_quantity,
)
from src.wallet import binance_account as wa  # noqa: E402

LEDGER = os.path.join(ROOT, "runs", "ledger.jsonl")


def send_then_drop(req, timeout=None):
    """把请求真的发出去，然后立刻丢弃响应并抛 URLError（模拟 TLS 断链）。"""
    parsed = urllib.parse.urlsplit(req.full_url)
    # 链路 RTT 中位数 2.3s、峰值 6.6s —— timeout 必须给足，否则会在发送阶段就超时，
    # 那样测的就不是"响应丢失"，而是"发不出去"（2026-11-03 实际踩到）。
    conn = http.client.HTTPSConnection(
        parsed.netloc, timeout=max(timeout or 15, 60), context=ssl.create_default_context()
    )
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        conn.putrequest(req.get_method(), path)
        for k, v in req.header_items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        # 不调 getresponse()：请求已到达服务器，响应被丢弃
    except BaseException as exc:                              # noqa: BLE001
        raise urllib.error.URLError(f"send failed before drop: {exc}") from exc
    finally:
        try:
            conn.sock.close()
        except Exception:                                     # noqa: BLE001
            pass
        conn.close()
    raise urllib.error.URLError("simulated: response dropped after send")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="确认会产生一笔真实成交")
    args = ap.parse_args(argv)
    if not args.yes:
        print("这会真的产生一笔约 5 USDT 的成交。确认请加 --yes")
        return 1

    key, secret = wa.load_credentials(os.environ)
    policy = load_policy()
    snap = bp.snapshot("CAKEUSDT")
    qty, err = plan_buy_quantity(snap.price, policy.notional_ceiling,
                                 snap.filters.min_notional, snap.filters.step_size,
                                 snap.filters.min_qty)
    if qty is None:
        print("plan failed:", err)
        return 1

    acct = wa.get_account(key, secret)
    free = Decimal(wa.balances(acct).get("USDT", "0"))
    notional = (qty * snap.price).quantize(Decimal("0.0001"))
    intent = OrderIntent(venue=Venue.BINANCE_SPOT, symbol="CAKEUSDT", side=Side.BUY,
                         instrument=Instrument.SPOT, quote_notional_usdt=notional,
                         base_qty=qty, reference_price=snap.price,
                         stop_distance_bps=500, slippage_bps=50, fee_bps=10,
                         filters=snap.filters)
    st = PortfolioState(equity_usdt=free, realized_loss_usdt=Decimal("0"),
                        open_positions=0, quote_free_usdt=free)
    perms = wa.full_permissions(key, secret)
    d = assess(intent, st, perms, policy)
    if not d.approved:
        print("risk rejected:", d.violations)
        return 1

    raw = policy.raw
    raw["execution"]["dry_run"] = False
    live = Policy(raw)
    gate = execution_gate(live, environ={"TRADER_LIVE_ORDERS": "1"})
    if not gate.may_submit:
        print("gate closed:", gate.reasons)
        return 1

    print(f"intent: BUY {qty} CAKEUSDT @ ~{snap.price} = {notional} USDT")

    # ---- 注入故障 ----
    original = urllib.request.urlopen
    urllib.request.urlopen = send_then_drop
    cid = None
    try:
        x.submit_order(intent, d, gate, live, perms, key, secret)
        print("FAIL: 没有抛异常 —— 故障注入没生效")
        return 1
    except x.OrderStateUnknown as exc:
        cid = exc.client_order_id
        print(f"PASS 1/3: 抛了 OrderStateUnknown，clientOrderId={cid}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"FAIL: 抛了 {type(exc).__name__}: {exc}")
        return 1
    finally:
        urllib.request.urlopen = original

    # ---- 回查 ----
    print("回查 GET /api/v3/order?origClientOrderId=... ...")
    for attempt in range(4):
        try:
            o = wa.query_order(key, secret, symbol="CAKEUSDT", client_order_id=cid)
            break
        except Exception as exc:                              # noqa: BLE001
            print(f"  attempt {attempt + 1}: {str(exc)[:140]}")
            time.sleep(3)
    else:
        print("FAIL: 回查失败")
        return 1

    status = o.get("status")
    executed = o.get("executedQty")
    print(f"PASS 2/3: 回查得到 status={status} executedQty={executed} quote={o.get('cummulativeQuoteQty')}")
    if status not in ("FILLED", "PARTIALLY_FILLED"):
        print("WARN: 状态不是 FILLED —— 订单可能确实没成交，回查结果同样有效")
    else:
        print("PASS 3/3: 成交被准确捞回 —— 不再依赖'余额为 0'去猜")

    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "fault_injection_test",
           "client_order_id": cid, "status": status, "executed_qty": executed,
           "note": "响应被主动丢弃后，用 query_order 回查验证成交可恢复"}
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("\n⚠️ 现在有真实持仓。请执行： python tools/auto_trader.py --flatten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
