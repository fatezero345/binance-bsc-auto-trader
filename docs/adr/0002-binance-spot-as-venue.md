# ADR-0002：交易场所改为 Binance 现货，GMGN/BSC 归档

- 状态：**已采纳**（supersede [ADR-0001](0001-gmgn-as-wallet-layer.md)）
- 日期：2026-11-03
- 决策者：项目所有者
- 依据：所有者指令「改成币安自动交易模式」（2026-11-03 13:16 +08）
- 相关：[02 权限审计](../02-wallet-audit.md)、[04 场所审计](../04-venue-audit.md)、[05 架构](../05-architecture.md)

## 背景

ADR-0001 把交易场所定在 BSC 上的 GMGN 托管钱包，理由是任务书假设的 Binance Agentic Wallet 不存在。该决定解决的是「今天能跑」，不是「长期最优」。

所有者随后要求改为币安。这个要求与仓库名（`binance-bsc-auto-trader`）也更一致。

本次不做的是「换个 API 调用」。真正需要重判的是**威胁模型**：托管钱包和交易所账户的风险面完全不同。

## 选项

| 选项 | 提币权 | 权限粒度 | 失败模式 | 结论 |
| --- | --- | --- | --- | --- |
| A. 自持私钥 + DEX | 自己控制 | 无（私钥即全权） | 私钥泄漏 = 全失 | 与任务书 §2 冲突 |
| B. GMGN 托管钱包（ADR-0001） | 托管方 | 一把 API Key 全权 | Key 泄漏 = 全失，本地无第二道门槛 | 已弃用 |
| C. **Binance 现货 API Key** | **可关闭** | 读 / 现货交易 / 提币 三档独立 + IP 白名单 | Key 泄漏 = 只能乱交易，转不走 | **采纳** |
| D. 只做 paper | — | — | 不满足自动交易目标 | 保留为 P2 前置 |

## 决定

**交易场所 = Binance 现货（CEX）。** `config/risk_policy.toml [venue].primary = "binance_spot"`，`gmgn_bsc` 移入 `archived`，且由 CI 断言它留在归档名单里（防止回退）。

## 依据（本次实测，非引用文档）

1. **`https://api.binance.com/api/v3/time` 可达**，HTTP 200（[evidence/raw/binance_server_time.json](../evidence/raw/binance_server_time.json)）。
2. **`/api/v3/exchangeInfo` 可读到三条白名单交易对的真实过滤器**：BTCUSDT / BNBUSDT / CAKEUSDT 均 `status=TRADING`，`minNotional=5.00000000`（[evidence/raw/binance_exchange_info.json](../evidence/raw/binance_exchange_info.json)）。
3. **权限位可读**：`/api/v3/account` 返回 `canTrade` / `canWithdraw` / `canDeposit` / `permissions`。这可直接回答任务书 §3 的八项，而 GMGN 侧只能靠文档推断。
4. **可行窗口存在**：29.30 USDT 本金、单笔上限 10 USDT、交易所下限 5 USDT → 每单 5–10 USDT 可落。实测三对都能下出合法单（[04 §三](../04-venue-audit.md)）。

## 后果

### 收益（相对 ADR-0001 的实质改进）

1. **提币权可以物理关闭。** 这是本次改变里唯一真正提升安全等级的一点：Key 泄漏的最坏后果从「资金被转走」降级为「资金被乱交易」，而后者的损失有上限（单笔 10 USDT、累计亏损 1.50 熔断）。
2. **权限粒度可审计。** 不再是「一把 Key 全权」，而是三档独立权限，且 `permissions` 必须是 `["SPOT"]` 的子集——出现 `MARGIN` 直接被风控拒绝。
3. **有测试网。** `testnet.binance.vision` 让 P3 可以在零真实资金下跑通全链路；GMGN 没有对应能力。
4. **过滤器是一等公民。** stepSize / minNotional 是可读、可测、可断言的数字，而不是「大概能行」。

### 代价（必须写清楚）

1. **交易所是交易对手。** 冻结、风控封号、地域限制、维护窗口——本地代码一行都缓解不了。且 29.30 USDT 的账户本身就是高风险画像。
2. **时钟偏差成为硬依赖。** 签名请求依赖 `timestamp` 与 `recvWindow`。本次实测 1 小时内偏差从 **−163 ms 漂到 −2762 ms**，已超过策略上限 1000 ms（[04 §一](../04-venue-audit.md)）。**不修时钟，live 模式不可用。**
3. **KYC / 地域 / 出入金**全部转移到交易所侧，本地不可控，也不再是本仓库能审计的部分。
4. **资金利用率被双边挤压。** 上限 10 USDT、下限 5 USDT，29.30 USDT 本金最多动用约 34%——单笔实际 5.0–5.6 USDT（实测）。这是规则的必然结果，不是 bug。
5. **限价单暂不开放。** P0 只允许 `type=MARKET`，因为限价单引入挂单状态机与撤单路径，那是 P3 的工作量。

### 触发重估的条件

- 账户被交易所风控限制或冻结；
- 时钟偏差无法稳定在 1000 ms 以内；
- 本金规模显著超过 29.30 USDT（单笔上限需重新推导）；
- 需要在同一套代码里同时支持 DEX（那就得回到双场所设计，不是改 `primary` 一行的事）。

## 对 ADR-0001 的处置

ADR-0001 **被本 ADR 取代**，但其两条结论仍然有效并保留：

1. 「本地不持有私钥」——在 CEX 下更强，本地只有 API Key；
2. 「任何单一 price 字段都不足以作为定价依据」——DEX 侧 WBNB 虚高 9.5% 的实证仍留在 [04 附录](../04-venue-audit.md) 作为方法学依据。

GMGN 相关代码保留在历史提交里，不删除证据；在代码层，`Venue.GMGN_BSC` 仍存在枚举值但会触发 `venue_archived` 拒绝——**保留它正是为了让"不小心回退"变成一条测试失败的红色断言**。
