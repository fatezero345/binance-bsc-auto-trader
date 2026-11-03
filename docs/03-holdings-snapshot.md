# 03 资产只读查询结果

**状态：🚫 BLOCKED。** 本节不含任何资产数字——因为查不到，不编。

## 一、查询与结果

采集时间：2026-11-03 13:2x（Asia/Shanghai）。

```bash
$ python -c "import os; print(bool(os.environ.get('BINANCE_API_KEY')))"
False
```

| 项 | 值 |
| --- | --- |
| `BINANCE_API_KEY` | **未设置** |
| `BINANCE_API_SECRET` | **未设置** |
| `GET /api/v3/account` | 未发起（无凭据，发了也只会拿到 401） |
| 结论 | **BLOCKED** |

代码路径已就绪并已测试：`src/wallet/binance_account.py::load_credentials({})` 抛 `CredentialsMissing`（测试 `test_credentials_missing_is_explicit`），而不是静默返回空余额。这一点很重要——**"读不到"和"余额为零"必须是两种不同的状态**。

## 二、为什么这是 BLOCKED 而不是"余额为 0"

- **余额为 0** 意味着：有 Key、鉴权通过、查询成功、数字是 0。
- **BLOCKED** 意味着：连鉴权都没过，问题问不出来。

此时若写出「当前资产 ≈ 29.30 USDT」，就是把任务书里的**计划投入**当成**实际持仓**——两个不同的事实。任务书 §4 明确要求「遇到无法确认的项目，明确标注 BLOCKED，不得编造结果」，故此处只标注。

## 三、任务书 §4.3 要求的三项，逐个交代

| 要求 | 状态 | 原因 |
| --- | --- | --- |
| 钱包地址 | **N/A**（ADR-0002） | CEX 无钱包地址；等价物是 Binance 账户，无公开地址 |
| Chain ID（应为 56） | **N/A**（ADR-0002） | 场所改为 CEX 后不存在链 ID；BSC 通路已归档 |
| USDT 余额 | 🚫 BLOCKED | 无 API Key |
| BNB 余额 | 🚫 BLOCKED | 同上 |

> 关于 Chain ID：不是"没测"，是这项在 CEX 模式下**失去意义**。保留原编号是为了让读报告的人能对上任务书，而不是假装它还是 BSC。若未来恢复 DEX 通路，这一项会随 `Venue.GMGN_BSC` 一起被风控以 `venue_archived` 拒绝——除非有新的 ADR。

## 四、解除条件（唯一路径）

按 [02 §七](02-wallet-audit.md) 建好只读+现货交易、**关闭提币权**、开 IP 白名单的 API Key，然后：

```powershell
$env:BINANCE_API_KEY    = "<key>"
$env:BINANCE_API_SECRET = "<secret>"
python tools/account_probe.py
```

`tools/account_probe.py` 会打印（而不是落盘）：

1. `canTrade` / `canWithdraw` / `canDeposit` / `permissions` —— **`canWithdraw` 必须为 `False`**；
2. USDT 与 BNB 的可用余额；
3. 一行结论：权限是否满足本项目安全前提。

把这些结果**不含密钥**地贴进本文件，并附上时间戳，本项才算交付。

## 五、证据卫生

本节当前只有一条证据：环境里没有密钥。这本身是有价值的证据（它说明通路是"待解锁"而不是"不存在"），但**不能**用来推断余额。评估这份报告的人应当把本节当作"问不出来"，而不是"资产为零"。

密钥永不进入本仓库：`.gitignore` 排除 `.env` / `.env.*` / `*.key` / `*.pem` / `secrets/`，`load_credentials()` 只读环境变量不落盘，且没有任何日志会打印密钥内容。
