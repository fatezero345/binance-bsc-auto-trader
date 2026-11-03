# 05 系统架构

> ADR-0002：场所为 Binance 现货。本文件取代原 GMGN/BSC 版本。

## 一、一句话

九个模块、单向依赖、四道闸；**风控是唯一能说"不行"的组件，执行是唯一能触达交易所的组件，两者之间没有旁路。**

## 二、架构图

```
                    ┌──────────────────────────────────────────────┐
                    │  AI 策略（不信任输入）                        │
                    │  src/strategy/   snapshot → Signal            │
                    │  权限：读快照；写 Signal。不得批准订单         │
                    └───────────────┬──────────────────────────────┘
                                    │ Signal（意图，没有金额决定权）
                                    ▼
 ┌──────────────────────┐   ┌───────────────────────────────┐
 │ src/market/          │   │  OrderIntent                  │
 │ Binance 公开只读：    │   │  symbol / side / base_qty /    │
 │ time, exchangeInfo,  │   │  notional / filters / 止损距离 │
 │ ticker/24hr          │   └──────────┬────────────────────┘
 │ 不得 import execution│              │
 └──────────┬───────────┘              ▼
            │        ┌────────────────────────────────────────────────┐
            │        │ ★ src/risk/  独立风控闸门（唯一否决方）          │
            │        │ assess(intent, state, permissions, policy)      │
            │        │  ← config/risk_policy.toml                     │
            │        │  提币权 / 白名单 / 步长 / minNotional / 熔断 …  │
            │        └──────────┬─────────────────────────────────────┘
            │                   │ RiskDecision(approved=True)
            │                   ▼
            │        ┌────────────────────────────────────────────────┐
            │        │ ★ execution_gate()  其余三道闸                  │
            │        │ enabled ∧ ¬dry_run ∧ env-unlock ∧ (主网需第4道)  │
            │        └──────────┬─────────────────────────────────────┘
            │                   │ may_submit
            │                   ▼
            │        ┌────────────────────────────────────────────────┐
            └───────►│ src/execution/  唯一可下单的模块                 │
                     │ build_order_params() 纯函数（可离线测）         │
                     │ submit_order()       唯一触网，P0 恒不调用      │
                     │ 默认目标 = testnet.binance.vision              │
                     └──────────┬─────────────────────────────────────┘
                                │ 成交回报（P3 起才有）
                                ▼
                     ┌────────────────────────────────────────────────┐
                     │ src/ledger/  append-only JSONL                  │
                     └──────────┬─────────────────────────────────────┘
                ┌───────────────┴───────────────┐
                ▼                               ▼
    ┌────────────────────────┐      ┌────────────────────────┐
    │ src/reporting/ 日报    │      │ src/paper/ 影子账本     │
    │ 审计轨迹               │      │ 不碰交易所的真实撮合模拟 │
    └────────────────────────┘      └────────────────────────┘

    ┌────────────────────────┐      ┌────────────────────────┐
    │ src/wallet/  鉴权只读  │      │ src/backtest/ 历史回放  │
    │ READ_ONLY_PATHS 白名单 │      │ kline，纯计算           │
    │ 无下单 / 无提币 / 无划转│     └────────────────────────┘
    └────────────────────────┘
```

## 三、模块边界（可执行的规则）

| 模块 | 允许 | **禁止** | 状态 |
| --- | --- | --- | --- |
| `market` | 读 `/api/v3/time`、`exchangeInfo`、`ticker/*` | import `execution`；任何写请求 | ✅ P0 已实现 |
| `strategy` | 读 snapshot，输出 `Signal` | import `execution`；读 `risk_policy.toml` | P4 |
| **`risk`** | 读 policy / `PortfolioState` / `AccountPermissions`，否决 | 任何 I/O、任何网络 | ✅ **P0 已实现** |
| `wallet` | `READ_ONLY_PATHS` 内的签名 GET | 下单、提币、划转、读私钥 | ✅ P0 已实现 |
| **`execution`** | `POST /api/v3/order`（四道门全开时） | 被 strategy 直接 import；绕过 `assess` + `execution_gate` | ✅ P0 骨架（恒不触网） |
| `backtest` | 读历史 kline | 写交易所、写账本 | P2 |
| `paper` | 影子成交、写 `state/` | 任何真实下单 | P2 |
| `ledger` | append-only 写 JSONL | 修改或删除历史行 | P1 |
| `reporting` | 读 ledger | 写 ledger、触发交易 | P5 |

## 四、量化链路（CEX 特有，也是最容易出错的一段）

```
price, minNotional, stepSize, 预算上限
        │
        ▼
plan_buy_quantity(price, budget, minNotional, stepSize, minQty)
        │  ① qty = minNotional / price
        │  ② 向上对齐 stepSize        ← 不是向下！CAKE 的坑就在这
        │  ③ 校验 qty ≥ minQty
        │  ④ 校验 qty×price ≥ minNotional
        │  ⑤ 校验 qty×price ≤ budget
        ▼
   (qty, None)  或  (None, 拒绝原因)
```

`assess()` 再校验一次步长与 minNotional，`build_order_params()` 第三次校验。三处重复是刻意的：
任何一处被改动，另两处仍会拦住，且三处都有对应测试。

## 五、为什么风控必须独立

任务书原文：「所有限制必须由独立代码执行，不能只依赖 AI 提示词」。

落地为四条硬约束：

1. **`OrderIntent` 没有 `is_approved` 字段。** 策略层构造不出"已批准"的意图；批准只能来自 `assess()` 的返回值。
2. **`strategy` 不允许读 `risk_policy.toml`。** 它连 10 USDT / 0.30 USDT / 5 USDT 这三个数字都拿不到，无法针对边界做特化（既不能卡着 9.99 反复试探，也不会有"我以为上限是 20"的幻觉）。
3. **`AccountPermissions` 是 `assess()` 的必填参数。** 想绕过提币权检查就得显式构造一个假对象——那在 code review 里一眼可见。
4. **fail_closed。** `load_policy()` 启动期全字段校验，缺字段直接崩；`assess()` 里"算不出来"一律计入 violations。

## 六、威胁模型（DEX → CEX 的重心转移）

| 威胁 | ADR-0001（GMGN 托管钱包） | **ADR-0002（Binance 现货）** |
| --- | --- | --- |
| 私钥泄漏 | 不存在（无本地私钥） | 不存在（CEX 无链上私钥） |
| **API Key 泄漏** | 🔴 致命：全权，可转走资金 | 🟠 **可交易损失**：提币权关闭 → 转不走 |
| 交易对手作恶 / 跑路 | 托管方，本地无法缓解 | 交易所，本地无法缓解（R3） |
| **权限位配错** | 无此面（一把 Key 全权） | 🔴 **新的头号风险**（R1）→ 三处硬断言 |
| **时钟漂移** | 无此面 | 🔴 **实证超限 2.7 倍**（R2）→ live 前必修 |
| 白名单被绕过 | 本地可拦 | 本地可拦（symbol 白名单 + 过滤器双约束） |
| 上游字段脏数据 | 已实证（WBNB 虚高 9.5%） | 未实测，P1 用契约测试盯住（R6） |
| 资金利用率 | 低（DEX 无下限） | 结构性上限 ~34%（R4，算术必然） |

**结论**：换场所不是"更安全"或"更危险"，而是**风险重心从「谁能拿到私钥」转到「权限位配得对不对 + 时钟准不准」**。所以 [07](07-risks-and-blockers.md) 里 R1（权限位）与 R2（时钟）排前两位，而不是滑点。

## 七、尚未落地的架构决定（P1 前必须定）

| 待定 | 选项 | 影响 |
| --- | --- | --- |
| 账本存储 | JSONL（默认）vs SQLite | 29.30 USDT 规模 JSONL 足够；可换但要保 append-only |
| 快照频率 | 轮询 vs 事件驱动 | 交易所限流是硬约束，间隔决定成本 |
| 状态机归属 | ledger 自带 vs 独立 | 决定"持仓数"这个熔断输入的可信度 |
| 时钟同步方案 | w32time / 第三方 NTP / 每次请求前读 serverTime | 直接决定 live 是否可用（R2） |
| Python 版本锁定 | 宿主 3.13 vs WSL 3.12 | 见 [01 §二](01-environment-audit.md) |
