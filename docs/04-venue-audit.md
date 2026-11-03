# 04 场所审计：Binance 现货

采样时间：**2026-11-03 13:1x–13:2x +0800**。原始响应：[evidence/raw/binance_*.json](evidence/raw/)。

> 本文件取代原 `04-bsc-market-check.md`。DEX 侧数据以**附录**保留——它不再决定交易路径，但其中「WBNB 报价虚高 9.5%」的实证仍然是本项目定价纪律的来源。

## 一、连通性与时钟

```
$ GET https://api.binance.com/api/v3/time
HTTP 200  {"serverTime": 1793682991362}
```

⚠️ **时钟偏差是本次审计最严重的发现。**

| 采样 | 本机 − 服务器 |
| --- | --- |
| 13:16 | **−163 ms** |
| 13:2x | **−2,762 ms** |

策略上限 `max_clock_skew_ms = 1000`。第二次采样**已经超限 2.7 倍**。

Binance 的签名请求带 `timestamp` + `recvWindow`（默认 5000 ms）。偏差 2.7 秒意味着：网络往返 + 处理时间只要超过 2.24 秒，请求就会被 `-1021 Timestamp for this request is outside of the recvWindow` 拒绝。**在修好时钟同步之前，live 模式不可用**（记入 [07 R2](07-risks-and-blockers.md)）。

测量方法的局限：本探针用「本地时间 − serverTime」估算，未扣除网络 RTT（实测 RTT 约 1.4 s）。因此绝对值可能高估，但**两次采样之间 2.6 秒的漂移**是真实存在的，且方向指向本机时钟不稳。

## 二、三条白名单交易对的过滤器（实测）

| | BTCUSDT | BNBUSDT | CAKEUSDT |
| --- | --- | --- | --- |
| status | TRADING | TRADING | TRADING |
| 参考价 | 76,824.00 | 696.77 | 1.8450 |
| 24h 成交额 | **1,105.3 M USD** | **75.6 M USD** | **1.5 M USD** |
| stepSize | 0.00001000 | 0.00100000 | 0.01000000 |
| minQty | 0.00001000 | 0.00100000 | 0.01000000 |
| tickSize | 0.01000000 | 0.01000000 | 0.00100000 |
| **minNotional** | **5.00000000** | **5.00000000** | **5.00000000** |
| 结论 | ✅ 可下单 | ✅ 可下单 | ✅ 可下单（流动性最薄） |

观察对（已拉过滤器，但**不在白名单**）：ETHUSDT（24h 452.6 M）、SOLUSDT（24h 161.8 M）——流动性远好于 CAKEUSDT，若要扩大白名单，这两个是首选。它们在代码里被标为 `observer_symbols`，出现在意图中会以 `symbol_observer_only` 被拒。

## 三、可行性：29.30 USDT 能不能真的下出一单

这是本文件里唯一真正重要的计算。三条约束同时成立才叫"能下单"：

1. 交易所 `minNotional = 5 USDT`（下界）；
2. 本项目 `max_notional = 10 USDT`（上界）；
3. **stepSize 量化**必须落在 [5, 10] 之内。

`tools/binance_probe.py` 实算结果：

```
equity = 29.3 USDT | order window = [5.0, 10.0] USDT
  BNBUSDT    OK  qty=0.00800000 @ 696.77000000 = 5.5742 USDT (19.0% of equity)
  BTCUSDT    OK  qty=0.00007000 @ 76824.00000000 = 5.3777 USDT (18.4% of equity)
  CAKEUSDT   OK  qty=2.72000000 @ 1.84500000 = 5.0184 USDT (17.1% of equity)
  3/3 whitelisted symbols can place an order of at most 10.0 USDT
```

### 步长陷阱（这是最容易写出"看起来成功、实际被拒"的地方）

CAKEUSDT：`5 / 1.845 = 2.7100` → 向下取整到 0.01 步长 → **2.71 × 1.845 = 4.99885 < 5** ❌
最小合法量是 **2.72**（5.0184 USDT），必须**向上**对齐。

若代码用 `round()` 或 floor，会得到一个交易所必然拒掉的订单，而在本地日志里它"已经下了"。因此：

- `plan_buy_quantity()` 从 `minNotional / price` **向上**量化，并校验结果仍在预算内；
- 量化失败一律 `reject`，不四舍五入、不自动加码；
- `assess()` 与 `build_order_params()` 各校验一次步长（双保险，且两处都会出现在测试里）。

### 资金利用率的必然上限

单笔上限 10 USDT、下限 5 USDT、本金 29.30 USDT → **最多动用约 34%**，实测单笔 5.0–5.6 USDT。这不是配置没调好，是「低本金 + 交易所下限 + 严格单笔上限」三条叠加的算术结果。要改变它，只能改上限或改本金——两者都得人工签字。

## 四、P0 没做到的部分（缺口，不是"已通过"）

| 待检项 | 状态 | 原因 |
| --- | --- | --- |
| 账户权限实测（`canWithdraw`） | 🚫 **BLOCKED** | 无 API Key，`/api/v3/account` 需鉴权（[03](03-holdings-snapshot.md)） |
| 实际手续费率 | 🚫 BLOCKED | 需 `/api/v3/account` 的 `commissionRates`；`exchangeInfo` 的 maker/taker 字段已废弃返回空 |
| 下单是否真能成交 | 🚫 未验证 | 需测试网 Key + 极小金额（P3） |
| 市价单实际滑点 | 🚫 未做 | 需要成交回报；**不得用历史均价冒充** |
| IP 白名单是否已开 | 🚫 未知 | 交易所侧人工确认，脚本只记录 |
| 时钟偏差根因 | 🚫 未查明 | 漂移已实证，是 NTP 还是虚拟化时钟问题未定（R2） |

## 五、附录：DEX 侧旧证据（已归档，不决定交易路径）

保留理由：其中一条结论已成为全项目的定价纪律。

**WBNB 的 `price` 字段虚高 9.5%。** `token info` 报 $760.73，而 `gas-price` 的 `native_token_usd_price` 是 $695.005。原因是它挑中的"最大池"流动性只有 **369 USD**、USDT 侧储备 **0.118 枚**，反推价算出 0.00045 USD/WBNB——荒谬到能自证该池无意义。

同期 CAKE 用主池 reserve 反推得 $1.83976，与报告价 $1.84080 偏差仅 **0.057%**（自洽），证明方法本身有效。BTCB 同法偏差 **0.560%** 且方向扩大，成因未定。

**由此固化的规则**：任何单一 `price` 字段都不足以作为定价依据，必须交叉验证。在 CEX 语境下这条表现为——用 24h `quoteVolume` 判断深浅（CAKEUSDT 仅 1.5 M，是两个数量级之外的薄池），而不是只看 `lastPrice` 就认为"能买"。

原始 DEX 证据仍在 `docs/evidence/raw/{cake,btcb,wbnb}_token_info.json`、`bsc_gas_price.json`、`gmgn_portfolio_info.json`，随 ADR-0002 一并归档。
