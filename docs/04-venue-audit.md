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

这是本文件里唯一真正重要的计算。四条约束同时成立才叫"能下单"：

1. 交易所 `minNotional = 5 USDT`（**下界**）；
2. 本项目 `max_notional = 5 USDT`（**目标上界**，2026-11-03 由所有者主动从 10 下调）；
3. **量化上浮容许度** `max_quantization_overrun_bps = 100` → 硬上限 **5.05 USDT**；
4. **stepSize 量化**后的名义本金必须落在 `[5.00, 5.05]` 之内。

### 为什么必须有第 3 条

步长是离散的，取整后的金额不可能恰好等于目标值。CAKEUSDT 在 5.00 目标下的最小合法单是 **5.0184 USDT**——比目标高 0.37%。如果 `max_notional` 是硬墙，这条策略在任何价格下都会被自己的 cap 拒死。

所以 cap 的语义定为：**目标 5.00，硬上限 5.05，超出硬上限一律拒绝**（不四舍五入、不自动缩量）。这 0.05 USDT 不是"偷偷放宽"，而是把"取整不可避免的溢出"显式化并封顶。`tools/ci_assert_policy.py` 断言 `overrun_bps ≤ 200`，防止它被悄悄改成变相提高上限。

### 单笔 = 5.00 之后的实测结果（2026-11-03 13:4x）

```
equity = 29.3 USDT | per-order target 5.0 USDT, ceiling 5.0500, exchange floor 5.0
quantisation window width = 0.0500 USDT
nominal step = stepSize x price ; a symbol is RELIABLY tradeable only if nominal step <= window

  BNBUSDT    NO  nominal_step=0.6958  price-alignment-only
             quantized_above_budget:5.56616>5.05
  BTCUSDT    NO  nominal_step=0.7664  price-alignment-only
             quantized_above_budget:5.36479>5.05
  CAKEUSDT   OK  nominal_step=0.0184  reliable
             qty=2.72000000 @ 1.84 = 5.0048 USDT (17.1% of equity)

1/3 whitelisted symbols can place an order inside the 5.0-5.05 USDT window
```

### 这一节的结论：一个通用判据

> **名义步长 = `stepSize × price`。只有当「名义步长 ≤ 量化窗口宽度（= 硬上限 − 交易所下限 = 0.05 USDT）」时，该标的才恒可成交。**

| 标的 | stepSize | price | 名义步长 | 与 0.05 窗口 |
| --- | --- | --- | --- | --- |
| CAKEUSDT | 0.01 | 1.840 | **0.0184** | ≤ 0.05 → ✅ 恒可成交 |
| BNBUSDT | 0.001 | 695.86 | **0.6958** | ≫ 0.05 → ❌ 只能靠价格恰好落在格点上 |
| BTCUSDT | 0.00001 | 76,675 | **0.7664** | ≫ 0.05 → ❌ 同上 |

**含义**：把单笔压到 5.00 之后，BTCUSDT 与 BNBUSDT **实质上退出可交易集合**——不是被代码禁用，而是它们的步长粒度（0.70 / 0.77 USDT）比整个允许窗口（0.05 USDT）还宽。理论上当价格落到某个 1% 宽的带内（例如 BNB 在 ~625–631 USDT 时，8 步 = 5.00–5.05）仍然可以成交，但这属于运气而不是设计。

两个因此成立的判断：

1. **当前的 5.00 单笔上限等于「只做 CAKEUSDT」。** 而 CAKEUSDT 的 24h 成交额只有 1.5 M USD，是三个标的中最薄的。
2. 想让 BTC/BNB 回到可交易，只有三条路：(a) 放宽单笔上限到 ≥5.6；(b) 把容许度放大到 ~12%（就不叫 5 了）；(c) 换 `stepSize` 与价格之比更小的标的。这是一个需要人工签字的选择（[07 D6](07-risks-and-blockers.md)）。

### 步长陷阱（这是最容易写出"看起来成功、实际被拒"的地方）

CAKEUSDT：`5 / 1.84 = 2.7174` → 向下取整到 0.01 步长 → **2.71 × 1.84 = 4.9864 < 5** ❌
最小合法量是 **2.72**（5.0048 USDT），必须**向上**对齐。

若代码用 `round()` 或 floor，会得到一个交易所必然拒掉的订单，而在本地日志里它"已经下了"。因此：

- `plan_buy_quantity()` 从 `minNotional / price` **向上**量化，并校验结果仍在硬上限内；
- 量化失败一律 `reject`，不四舍五入、不自动加码；
- `assess()` 与 `build_order_params()` 各校验一次步长（双保险，且两处都会出现在测试里）。

### 资金利用率的必然上限

单笔目标 5.00、硬上限 5.05、本金 29.30 USDT → 单笔最多动用约 **17%**（实测 CAKE 5.0048 USDT）。这不是配置没调好，是「低本金 + 交易所下限 + 严格单笔上限」三条叠加的算术结果。要改变它，只能改上限或改本金——两者都得人工签字。

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
