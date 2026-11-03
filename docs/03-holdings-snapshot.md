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

---

## 八、追加实测：资金钱包也是空的（2026-11-03 13:5x）

所有者告知本金在资金钱包。据此新增两条**语义只读、但交易所要求 POST** 的查询接口，
并实测：

```
=== funding wallet (POST /sapi/v1/asset/get-funding-asset) ===
assets returned: 0 | non-zero: 0

=== all wallets (POST /sapi/v3/asset/getUserAsset) ===
assets returned: 1 | non-zero: 1
{"asset":"USDT","free":"0.00000142","locked":"0","freeze":"0","withdrawing":"0",
 "ipoable":"0","btcValuation":"0"}
```

### 结论：这把 Key 能看到的**全部资产**是 0.00000142 USDT

不是"现货没有、资金钱包有"。资金钱包返回 **0 条资产**，全钱包查询也只有一条
0.00000142 USDT。

所以 29.30 USDT **不在这把 Key 对应的账户里**。可能性按概率排序：

1. **Key 属于子账户**——`permissions=['TRD_GRP_236']` 与近乎为零的余额都指向这个；
   子账户的余额与主账户完全独立，`/sapi/v3/asset/getUserAsset` 只看得到子账户自己。
2. Key 属于不同实体账户（区域性实体），与 App 里看到的那个账户不是同一个。
3. 本金尚未入金。

**需要人工确认**：在 App 里看「资金账户」时，当前选中的是主账户还是子账户；
API Key 是在哪个账户下创建的。这一点无法由 API 自身判定。

在余额与 Key 归属对齐之前，交易无从谈起——不是风控不同意，是**账户里没有可交易的余额**。
---

## 九、ADR-0003 之后的真实判决（2026-11-03 14:0x）

所有者明确接受 `enableFutures` / `enableInternalTransfer` / 无 IP 白名单三项风险（[ADR-0003](adr/0003-accepted-key-risks.md)）。据此把「拒绝」改为「署名接受 + 记账」，然后跑一次完整判决——**只判不下单**：

```
intent      : BUY 2.72000000 CAKEUSDT @ 1.84300000 = 5.0130 USDT
free USDT   : 0.00000142
approved    : False
violations  : ['risk_exceeds_equity', 'insufficient_free_balance:5.0130>0.00000142']
warnings    : ['key_flag_accepted:enable_internal_transfer',
               'key_flag_accepted:enable_futures',
               'key_ip_restrict_disabled_accepted']
gate        : CLOSED ['policy.execution.enabled=false', 'policy.execution.dry_run=true',
                      'env TRADER_LIVE_ORDERS not set']
```

三件事同时成立了：

1. **权限不再是障碍** —— 三项都变成 `warnings`，不再进 `violations`；
2. **真正的障碍是余额** —— `insufficient_free_balance:5.0130 > 0.00000142`。这一条是 2026-11-03 才补上的检查（原本风控会放行一单买不起的订单，然后在交易所被 `-2010` 拒掉）；
3. **执行仍关闭** —— 三道开关全关，与余额无关。

顺带补的 `risk_exceeds_equity`：`equity_usdt` 现在取实际可用余额（0.00000142），不是配置里的 29.30。**配置里的本金与账户里的钱已经脱钩，这是事实，不是笔误。**
---

## 十、dry_run 改造后的第一次真实预检（2026-11-03 14:1x）

```
symbol      : CAKEUSDT
price       : 1.84000000
qty         : 2.72000000          (step 0.01 ✔)
notional    : 5.0048 USDT         (≥ minNotional 5.00 ✔, ≤ ceiling 5.05 ✔)
gate        : enabled=True dry_run=True mainnet=True env=False

=== POST /api/v3/order/test ===
RESULT: 200 OK -> {}
```

**交易所接受了这个订单。** 签名、recvWindow、参数、过滤器全部通过。

这是本项目第一次让交易所而不是本地代码来判定订单合法性。结论是：

> **订单是合法的，签名是有效的，时钟在窗口内。唯一拦住它的不是代码，是账户余额 0.00000142 USDT。**

改造内容见 [ADR-0004](adr/0004-dry-run-hits-order-test.md)：`dry_run` 从"什么都不发"
改成"发到 `/order/test`"——每跑一次 dry_run 就是一次真实接线验证，而不是本地自我安慰。