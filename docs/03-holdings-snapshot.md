# 03 资产只读查询结果

**状态：✅ 已实测（2026-11-03 13:4x +0800）。结论是坏的，但它是真的。**

## 一、实测输出

```
$ python tools/account_probe.py
target = https://api.binance.com

=== credentials ===
BINANCE_API_KEY present (len=64), secret present (len=64)

=== account permissions (must-have: canWithdraw == False) ===
canTrade    = True
canWithdraw = True      <-- MUST be False
canDeposit  = True
permissions = ['TRD_GRP_236']

=== balances (read-only) ===
BNB   free = 0.00000000
USDT  free = 0.00000142

=== verdict ===
  !! canWithdraw is TRUE -- revoke withdrawal permission before doing anything else

STOP. The account does not satisfy the P0 safety precondition.
EXITCODE=3
```

密钥只在这一次进程的环境变量里存在，跑完即清除；未落盘、未回显、未进 git。

## 二、两条硬结论

### 1. 🚫 提币权是开着的 —— 安全前提不成立

`canWithdraw = True`。

这是 [ADR-0002](adr/0002-binance-spot-as-venue.md) 里整个 CEX 方案的前提，也是 [07 R1](07-risks-and-blockers.md)。开着意味着：

| | 现状 | 要求 |
| --- | --- | --- |
| Key 泄漏后的最坏后果 | 资金被**转走**，账户清零 | 只能被"乱交易"，损失上界 5.05 USDT + 熔断 1.50 |

`account_probe.py` 按设计以**退出码 3** 停下，没有继续做任何事。风控闸门在这里第一次拦住了真实世界的错误配置——这正是它存在的理由。

**必须做的事**：Binance → API 管理 → 编辑该 Key → 取消勾选 **Enable Withdrawals** → 保存。然后重跑探针，必须看到 `canWithdraw = False` 且退出码 0。

### 2. 🚫 本金不在这个现货账户里

```
USDT free = 0.00000142      （0.00000142，不是 29.30）
BNB  free = 0.00000000
```

计划投入的 29.30 USDT **在这个 Key 能看到的现货账户里不存在**。

`/api/v3/account` 只返回**现货钱包**余额。Binance 另有资金钱包（Funding Wallet），USDT 很可能停在那里——但那是**推测，不是实测**。在确认之前，本节不能声称"有 29.30 USDT"。

需要下一步确认（二选一）：

- 人工在 App 里看「资金账户」余额；
- 或用 `READ_ONLY_PATHS` 里的 `/sapi/v1/capital/config/getall` 之外再加一条资金钱包查询（当前白名单没有，需要新增并评审）。

## 三、附带修掉的一个真实 bug

`permissions = ['TRD_GRP_236']` —— 不是 `['SPOT']`。

策略原本要求 `permissions ⊆ ["SPOT"]`，这会把**自己的真实账户误杀**。这是真数据打出来的，不是想出来的。已改为「允许清单 + 禁止词」双段判定：

```toml
allow_permission_patterns  = ["^SPOT$", "^TRD_GRP_[0-9]+$"]
forbidden_permission_markers = ["MARGIN", "LEVERAGED", "FUTURES", "OPTIONS", "CONVERT", "EARN"]
```

`TRD_GRP_<n>` 是 Binance 区域性实体使用的交易组标记，仍然只映射到现货。测试 `test_trade_group_permission_is_accepted` 把它钉住，`test_unknown_permission_marker_is_rejected` 保证新出现的未知标记仍然被拒（宁可误杀不可放过）。

## 四、任务书 §4.3 的三项，逐个交代

| 要求 | 状态 | 实测值 |
| --- | --- | --- |
| 钱包地址 | **N/A**（CEX） | 无链上地址；见 ADR-0002 |
| Chain ID（应为 56） | **N/A**（CEX） | 该项在 CEX 模式下失去意义 |
| USDT 余额 | ✅ **已实测** | **0.00000142**（不是 29.30） |
| BNB 余额 | ✅ **已实测** | **0.00000000** |

上一版这一节写的是 BLOCKED 而不是 0 —— 当时的判断是对的：没有凭据时"读不到"绝不能写成"余额为零"。现在读到了，数字就是 0.00000142。

## 五、密钥卫生

- 密钥只作为**单次进程**的环境变量传入，命令结束即 `Remove-Item`；
- 未写入任何文件、未进 git（`.gitignore` 排除 `.env*`）、未出现在任何输出里；
- 探针只打印 `len=64`，从不打印内容；
- 该 Key 已出现在本次对话记录中。若这份记录会外流，**跑完诊断后应轮换一把新的**。

## 六、解除条件

1. 关掉 `Enable Withdrawals`（必须）；
2. 确认 29.30 USDT 的实际位置（现货钱包 or 资金钱包），把它划到现货钱包，或把本金规模改成本节实测值（这会连带重算 P3 的资金闸）；
3. 重跑 `python tools/account_probe.py`，必须 `canWithdraw = False` 且退出码 0。

在这三条满足之前，`docs/06` 的 P1 门不过，不得进入 P2。
