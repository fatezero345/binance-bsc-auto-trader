# ADR-0004：dry_run 改为「打交易所的订单测试端点」

- 状态：**已采纳**
- 日期：2026-11-03
- 决策者：项目所有者（授权「代码阻挡的话就把代码改了」）
- 相关：[ADR-0003](0003-accepted-key-risks.md)、[03 实测](../03-holdings-snapshot.md)、[05 架构](../05-architecture.md)

## 背景

ADR-0003 之后，权限不再是拒绝理由。但真实判决仍然是 `approved=False`，因为：

```
insufficient_free_balance:5.0130 > 0.00000142
risk_exceeds_equity
```

所有者授权「代码阻挡就改代码」。于是要问一个更根本的问题：**这些代码到底在阻挡什么？**

- `insufficient_free_balance` 挡的不是"我的规矩"，是**交易所自己会拒**。删掉它只会把清晰的本地报错换成远程的 `-2010`。
- 真正值得改的是另一件：`dry_run=true` 原本意味着"什么都不发"。这让 dry_run **永远证明不了接线是通的**——签名、时钟、参数、过滤器一次都没被真实验证过。

## 决定

1. **`dry_run` 的语义从「不发」改为「发到 `/api/v3/order/test`」。** 交易所的这个端点校验签名、`recvWindow`、参数与过滤器，**不进入撮合**。每跑一次 dry_run 就是一次真实接线验证，而不是本地自我安慰。
2. **`for_preflight=True` 时不要求环境解锁变量**——预检不动钱，不需要"我真的要下单"的确认。真实下单（`submit_order`）仍然强制要求它。
3. **打开执行路径**：`enabled=true`、`allow_mainnet=true`。真实下单还差两步：`dry_run=false` + 环境变量 `TRADER_LIVE_ORDERS`。

## 为什么保留余额检查

删掉它不会让任何订单成交，只会让错误信息变差。**「代码阻挡」和「交易所不允许」是两件事**：

| 检查 | 它挡的是 | 删掉的后果 |
| --- | --- | --- |
| `insufficient_free_balance` | 交易所必然拒的订单 | 本地报错变成远程 `-2010`，排查成本上升 |
| `notional_exceeds_ceiling` | 自己定的 5.05 上限 | 真的会超发，违反所有者设的资金边界 |
| `circuit_breaker_tripped` | 累计亏损已达 1.50 | 熔断失效 |
| `key_flag_dangerous:{withdrawals}` | 钱能被搬走 | 提币权红线失效 |

所以改的是「dry_run 什么都不做」这条**假闸门**，不是把真闸门拆了。

## 实测结果（2026-11-03 14:1x）

```
symbol      : CAKEUSDT
price       : 1.84000000
qty         : 2.72000000          (step 0.01 ✔)
notional    : 5.0048 USDT         (≥ minNotional 5.00 ✔, ≤ ceiling 5.05 ✔)
gate        : enabled=True dry_run=True mainnet=True env=False

=== POST /api/v3/order/test ===
RESULT: 200 OK -> {}
```

**交易所接受了这个订单。** 签名有效、时钟在 `recvWindow` 内、参数合法、过滤器通过。

也就是说：**这是第一笔"只要账户里有钱就会成交"的订单。** 唯一还挡着它的不是代码，是账户可用余额 `0.00000142 USDT`。

## 后果

- dry_run 从"零信息"变成"每次都验证接线"——这是净收益，与余额无关。
- 真实下单仍然需要两道人工动作（改 `dry_run`、设环境变量）。这不是我留的后门，是给"手滑"留的余地。
- 余额一旦 ≥ 5.05，`assess()` 的所有检查都会通过，`submit_order()` 只需清掉那两道即可落单。

## 重估触发条件

- 首次真实成交之后（应回看这道门是否仍需要人工确认）；
- 出现任何 `-1021`（时钟）或 `-2010`（余额）以外的拒绝码。
