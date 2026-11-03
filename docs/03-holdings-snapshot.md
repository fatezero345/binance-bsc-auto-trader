# 03 账户与 Key 权限实测

**状态：✅ 已实测（2026-11-03 13:4x 两次采样）。提币权已关闭；但还有三项没过，本金也不在现货账户。**

## 一、先更正上一版的一个错误

上一版我写「`canWithdraw = True`，提币权开着」。**这个判断读错了字段。**

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `canWithdraw` | `/api/v3/account` | **账户**能不能提币 |
| `enableWithdrawals` | `/sapi/v1/account/apiRestrictions` | **这把 Key** 能不能提币 |

所有者确实关掉了 Key 的提币权（`enableWithdrawals=False`），但我读的是账户级字段，于是误报成"没关"。已修：`assess()`、`build_order_params()`、`account_probe.py` 全部改为以 **Key 级**权限位为准，并且 Key 级读不到时 **fail_closed**（`key_permissions_unavailable`），不当成"没危险"。

这条被测试钉住：`test_account_level_can_withdraw_alone_is_not_a_violation` / `test_key_withdraw_flag_open_is_rejected` / `test_missing_key_flags_fail_closed`。

## 二、实测输出（2026-11-03 13:4x）

```
=== account (GET /api/v3/account) ===
canTrade    = True
canWithdraw = True   <-- ACCOUNT-level, not the key's flag
canDeposit  = True
permissions = ['TRD_GRP_236']

=== key flags (GET /sapi/v1/account/apiRestrictions)  <-- authoritative ===
enableReading                = True
enableSpotAndMarginTrading   = True
ipRestrict                   = False   <-- must be True
--- these must ALL be False ---
enableWithdrawals              = False   ✅
enableInternalTransfer         = True    ❌ OPEN
enableFutures                  = True    ❌ OPEN
enableMargin                   = False
enableVanillaOptions           = False
permitsUniversalTransfer       = False
enablePortfolioMarginTrading   = False

=== balances (spot wallet only) ===
BNB   free = 0.00000000
USDT  free = 0.00000142

EXITCODE = 3
```

## 三、还差三件事（探针退出码仍为 3）

### 1. 🔴 `enableFutures = True` —— 这条比提币权更危险

合约权限开着，意味着这个 Key **可以在合约市场开杠杆仓**。而本项目整个风控模型只覆盖现货市价单：

| 风控假设 | 合约开仓后的现实 |
| --- | --- |
| 单笔 ≤ 5.05 USDT | 合约可以开 10x / 20x，名义敞口 = 保证金的 10–20 倍 |
| 累计亏损 1.50 USDT 熔断 | 强平可以一次亏掉全部保证金，绕过熔断 |
| 只做现货，无杠杆 | 直接违反 `[forbidden].leverage / derivatives = false` |

**本地代码管不到这条路**——它不经过 `src/execution/`，是 Key 自身的能力。所以必须在 Key 上关掉。

### 2. 🟠 `enableInternalTransfer = True` —— 内部划转

能在自己的钱包 / 子账户之间搬钱。搬不出平台，但也超出了"只做一笔 5 USDT 现货"的授权范围。

### 3. 🟠 `ipRestrict = False` —— 没开 IP 白名单

Key 现在从任何 IP 都可用。这把 Key 已经出现在对话记录里，没有 IP 约束等于没有第二道地理门槛。

## 四、操作清单

Binance → API 管理 → 编辑该 Key：

- [x] **Enable Withdrawals** → 已取消（`enableWithdrawals=False` 已确认）
- [ ] **Enable Futures** → **取消勾选**（必须）
- [ ] **Enable Internal Transfer** → 取消勾选
- [ ] **Restrict access to trusted IPs only** → 勾上，填本机出口 IP
- [x] Enable Reading → 保持
- [x] Enable Spot & Margin Trading → 保持

改完重跑：

```
python tools/account_probe.py     # 目标：EXITCODE=0
```

## 五、本金仍然不在现货账户

```
USDT free = 0.00000142      （不是 29.30）
BNB  free = 0.00000000
```

`/api/v3/account` 只返回**现货钱包**。USDT 可能停在资金钱包（Funding Wallet）——**推测，未实测**。当前 `READ_ONLY_PATHS` 里没有资金钱包查询接口，所以这一点现在查不了。

两条路：

1. 人工在 App 里看「资金账户」，有就划转到现货账户；
2. 或把 `capital.initial_equity_usdt` 改成本节实测值（会连带重算 P3 资金闸——0.00000142 USDT 连 5 USDT 的交易所下限都够不着，等于不能交易）。

在确认之前，任何"当前资产 29.30 USDT"的说法都不成立。

## 六、密钥卫生

- 密钥只作为**单次进程**的环境变量传入，命令结束即 `Remove-Item`；
- 未写入任何文件、未进 git、未出现在任何输出里（探针只打印 `len=64`）；
- 该 Key 已出现在本次对话记录中。**跑完全部诊断后应轮换一把新的**——尤其 `ipRestrict` 还是 false 的现在。

## 七、这一轮真正的收获

不是"又发现三个开关"，而是**风控第一次被真实数据证伪并当场修好**：

1. 账户级 vs Key 级权限字段混用 → 误报，已改成 Key 级 + fail_closed；
2. `permissions` 白名单过严（只认 `SPOT`）→ 误杀真实账户（`TRD_GRP_236`），已改成允许清单 + 禁止词；
3. 危险开关的清单从"提币一项"扩到**七项**（提币 / 内部划转 / 合约 / 杠杆 / 期权 / 万能划转 / 组合保证金），每一项都有独立测试。

测试数从 56 → **65**，全部离线可跑。
