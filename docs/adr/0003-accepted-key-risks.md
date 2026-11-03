# ADR-0003：实验账户接受合约/划转权限与无 IP 白名单

- 状态：**已采纳**
- 日期：2026-11-03
- 决策者：项目所有者（账户持有人本人）
- 相关：[02 权限审计](../02-wallet-audit.md)、[03 实测](../03-holdings-snapshot.md)、[07 R16](../07-risks-and-blockers.md)

## 背景

P0 的 Key 权限审计要求「七项危险开关全关 + 开 IP 白名单」。实测后所有者明确答复：

> api权限就开这么大，没有问题，在可接受范围内，这个账户就是给你玩的

即：`enableFutures=True`、`enableInternalTransfer=True`、`ipRestrict=False` 三项**保持现状**。

可选做法有两条：

| 做法 | 后果 |
| --- | --- |
| A. 坚持原门槛，拒绝交易 | 系统永远停在 P0。所有者已明确接受风险，坚持只会让代码变成障碍 |
| B. **把「拒绝」改成「署名接受 + 记账」** | 风险仍在，但谁签的字、为什么、影响了哪几行代码，全部可查 |

## 决定

采用 B：**危险开关分两张表**。

```toml
forbidden_key_flags   = ["enable_withdrawals", "permits_universal_transfer"]   # 硬红线，出现即拒
[venue.binance.accepted_risk_flags]                                            # 署名接受，放行但记账
enable_futures = "ADR-0003 / 2026-11-03 所有者明确接受；本仓库无任何合约代码路径"
enable_internal_transfer = "..."
...
```

规则：**任何危险开关必须落在这两张表之一。** 既不禁止、也没有署名记录的开关会被 `key_flag_unaccounted` 拒绝——不允许"既不禁、也没人签"这种静默状态存在。

放行的开关不静默：`RiskDecision.warnings` 会带上 `key_flag_accepted:<flag>`，测试 `test_owner_accepted_flags_pass_but_are_recorded` 同时断言"通过"和"留痕"两件事。

## 为什么红线仍然保留两项

**提币（`enableWithdrawals`）与万能划转（`permitsUniversalTransfer`）** 是唯一两条「钱能被搬出这个账户」的路径：

- 其余开关（合约/杠杆/期权/内部划转）的最坏后果都被**账户余额**封顶 —— 账户里有多少，最多亏多少；
- 提币与万能划转不是：Key 泄漏时它们能把「余额上限」变成「任何上限」。

而且这两项**与盈利无关**——本项目 `src/execution/` 里没有提币、没有划转、没有合约，一行都没有。关掉它们不影响任何能力，只影响别人拿走这笔钱的可能性。所有者实测的 `enableWithdrawals` 也已经是 `False`，不存在需要改动的地方。

## 后果

### 收益

1. 代码不再因为所有者已经接受的风险而空转。P0 的 P1 门可以过。
2. 风险从"被拒绝的黑箱"变成"有署名的记录"——`grep ADR-0003` 就能找回全部上下文。
3. `key_flag_unaccounted` 这条兜底，让**未来**新出现的权限位默认走拒绝，而不是默认走放行。

### 代价（必须写清楚）

1. **`enableFutures=True` 意味着这把 Key 能在合约市场开杠杆仓，而本地代码完全管不到。** 只要有人拿到这把 Key，就可以在 `src/execution/` 之外把账户打穿。`ipRestrict=False` 进一步放大了这个面（可从任意 IP 使用）。
2. 本项目自己的风控上限（单笔 5.05 USDT、熔断 1.50）**只约束本项目发出的订单**，不约束 Key 的其他用途。这一点在 [07 R16](../07-risks-and-blockers.md) 里保留，不因本 ADR 消失。
3. 当前的化解手段只有一条：**账户余额极小**。实测 0.00000142 USDT，等于没有可亏的钱。**一旦余额变大（例如 29.30 真的到位），本 ADR 的前提就变了，必须重新评审。**

### 重估触发条件

- 账户余额超过 29.30 USDT 的 2 倍；
- 这把 Key 出现在对话记录或任何日志里，且 `ipRestrict` 仍为 false；
- 出现任何非本项目发起的订单或划转记录；
- P3 进入主网实盘之前（届时至少应开启 IP 白名单）。
