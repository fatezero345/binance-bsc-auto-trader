# 02 账户权限审计（Binance 现货）

> 本文件取代原「GMGN 托管钱包」版本。任务书 §3 的八项在 CEX 语境下重新逐条回答——结论比 DEX 版更硬，因为 Binance 的权限位是**可读、可断言**的。

## 一、结论先行

| # | 任务书 §3 待确认项 | 结论 | 依据 |
| --- | --- | --- | --- |
| 1 | 是否支持只读余额查询 | ✅ **支持**（代码已实现，无 Key 故 BLOCKED） | `GET /api/v3/account`（签名只读） |
| 2 | 是否支持 BSC 网络 | **不适用** | ADR-0002：场所改为 CEX，无链概念；BSC 通路已归档 |
| 3 | 是否支持交易模拟 | ✅ **支持，且强于 DEX** | `testnet.binance.vision` 真实撮合 + `[execution] dry_run` |
| 4 | 是否支持 Swap | ✅ **支持** | `POST /api/v3/order`（P0 只开 `type=MARKET`） |
| 5 | 是否能限制目标合约 | ✅ **可限制**（本地白名单 + 交易所过滤器双约束） | `[whitelist].symbols` + `SymbolFilters` |
| 6 | 是否能限制单笔金额 | ✅ **可限制**（5.00–5.05 USDT 双向夹逼） | `min_notional=5.00` / `notional_ceiling=5.05` |
| 7 | 是否能限制授权额度 | ✅ **等价物更强**：提币权必须为 false | `[venue.binance].require_can_withdraw_false` |
| 8 | 是否能独立控制签名权限 | ✅ **可**，且可撤销 | API Key 三档权限 + IP 白名单 + 随时删除 |

**一句话**：CEX 模型下，「限制授权额度」这个问题变形为「限制 Key 的权限位」。而这一项恰恰是 Binance 比 GMGN 强的地方——**提币权可以物理关闭**。

## 二、安全前提：`can_withdraw` 必须为 false

ADR-0002 整个方案成立与否，只取决于这一条。

| 场景 | Key 泄漏后的最坏后果 |
| --- | --- |
| 提币权 **开着** | 资金被转走，29.30 USDT 全失，本地无任何代码能拦 |
| 提币权 **关着** | 只能在本账户内乱交易；损失上界 = 单笔硬上限 5.05 USDT + 熔断 1.50 USDT |

所以 `src/risk/policy.py::assess()` 里有一条**硬断言**：

```python
if policy.binance_perms.get("require_can_withdraw_false", True) and permissions.can_withdraw:
    v.append("account_can_withdraw_must_be_false")
```

`permissions` 来自 `GET /api/v3/account` 的真实返回，不是配置文件里的自述。测试 `test_account_withdraw_enabled_is_rejected` 覆盖；`tools/ci_assert_policy.py` 另有一道 CI 断言防止这条被改成 `false`。

**注意这不是"我们记得关"**：即使有人把 Key 换成带提币权的，风控也会在下一笔订单前拒绝，且 `src/execution/binance_spot.py::build_order_params()` 里还有第二道同样的检查。

## 三、权限位与代码的对应

| Binance 权限位 | 本项目要求 | 落点 |
| --- | --- | --- |
| Enable Reading | ✅ 必须开 | 否则读不到 `/api/v3/account`，「提币权是否关闭」无从验证 → 直接 BLOCKED |
| Enable Spot & Margin Trading | ✅ 必须开（P0 只用到现货部分） | `require_can_trade = true` |
| **Enable Withdrawals** | ❌ **必须关** | `require_can_withdraw_false = true` |
| IP 访问限制 | ✅ 必须开（白名单到固定出口） | `require_ip_whitelist = true`（人工确认，脚本只记录） |
| `permissions` 必须 ⊆ `["SPOT"]` | ✅ | 出现 `MARGIN` / `LEVERAGED` 即 `account_permissions_not_subset` |

## 四、模拟能力：测试网是关键增量

`https://testnet.binance.vision` 是 Binance 官方的现货测试网，账户与主网资产**完全隔离**。这让 P3 变成这样：

1. 先在测试网用假钱跑通「意图 → 风控 → 下单 → 回报 → 账本」全链路；
2. 主网还需要第四道开关 `[execution] allow_mainnet = true`，默认 false。

代码层体现（`src/execution/binance_spot.py::_base_url`）：

```python
if gate.allow_mainnet:
    return policy.base_url          # https://api.binance.com
return policy.testnet_base_url      # https://testnet.binance.vision
```

测试 `test_testnet_is_default_target` 断言"即使三道门全开，默认目标也不是主网"。

## 五、签名材料纪律（任务书 §2 三条禁令）

| 禁令 | 落实情况 |
| --- | --- |
| 禁止读取/输出/上传/提交私钥与助记词 | CEX 模型下**不存在私钥**；本地只有 `BINANCE_API_KEY` / `BINANCE_API_SECRET` 两个环境变量 |
| 禁止把钱包私钥交给 DSH Worker | 同上，无物可交；密钥只从 `os.environ` 读，不落盘、不进日志（`load_credentials()` 返回即用） |
| 禁止提币/转账/无限授权/未授权真实交易 | `src/execution/` 内不存在提币、划转的任何函数或常量；`[forbidden]` 全 false；四道开关默认全关 |

`.gitignore` 排除 `.env` / `.env.*` / `*.pem` / `*.key` / `mnemonic*` / `keystore/` / `secrets/`。

## 六、代码中的只读边界（可测）

`src/wallet/binance_account.py` 里有一张**只读路径白名单**，不在表里的路径直接抛 `WalletError`，包括 `/api/v3/order`：

```python
READ_ONLY_PATHS = {
    "/api/v3/account",
    "/api/v3/openOrders",
    "/api/v3/myTrades",
    "/sapi/v1/capital/config/getall",
}
```

测试覆盖：`test_order_path_is_not_read_only`、`test_read_rejects_non_whitelisted_path_without_network`（在不打网络的情况下验证拒绝）、`test_no_private_path_in_whitelist`。

`src/execution/binance_spot.py` 的源码里**不允许出现** `capital/withdraw` / `asset/transfer` / `universalTransfer` 字样，由 `test_no_withdraw_or_transfer_path_in_execution_source` 扫描源码断言。

## 七、解除 BLOCKED 的操作步骤（人工）

1. Binance → API 管理 → 创建 API Key；
2. 权限只勾 **Enable Reading** + **Enable Spot & Margin Trading**，**不勾** Enable Withdrawals；
3. 开启 IP 访问限制，填本机出口 IP；
4. 设置环境变量（**不要写进任何文件**）：
   ```powershell
   $env:BINANCE_API_KEY    = "<key>"
   $env:BINANCE_API_SECRET = "<secret>"
   ```
5. 验证：
   ```bash
   python tools/account_probe.py           # 读 /api/v3/account，打印权限位与 USDT/BNB 余额
   ```
6. 若 `canWithdraw=true` 出现在输出里 → **立即停手**，回第 2 步改权限。

在此之前，`docs/03` 保持 BLOCKED，且任何"当前资产 = 29.30 USDT"的说法都是编的。
