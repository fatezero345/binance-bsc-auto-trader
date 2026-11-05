# 证据索引

本目录存放结论的**原始响应**。规则：

1. 每份文件都是某条命令/请求的**未加工**输出，不做手工整理；
2. 采集时间与命令写在下表；
3. **不含任何密钥、私钥、助记词、完整 API Key**（`.gitignore` 也排除 `.env*` / `*.key` / `*.pem` / `secrets/`）；
4. 报告里出现但这里找不到的结论 = 没做。

## raw/ — 当前通路（Binance 现货，ADR-0002）

采集于 **2026-11-03 13:1x–13:2x +0800**，由 `tools/binance_probe.py` 与直接 `GET` 生成。

| 文件 | 采集方式 | 支撑的结论 |
| --- | --- | --- |
| `binance_server_time.json` | `GET /api/v3/time` | [04 §一](../04-venue-audit.md)：场所可达 |
| `binance_exchange_info.json` | `GET /api/v3/exchangeInfo?symbols=[…]` | [04 §二](../04-venue-audit.md)：三条白名单的 stepSize / minNotional / tickSize |
| `binance_ticker_24hr.json` | `GET /api/v3/ticker/24hr?symbols=[…]` | [04 §二](../04-venue-audit.md)：参考价与 24h 成交额（CAKEUSDT 仅 1.5 M，判深浅的依据） |

> **时钟偏差不在这三份文件里**，它来自 `tools/binance_probe.py` 的两次实跑（−163 ms / −2,762 ms），记录在 [04 §一](../04-venue-audit.md) 与 [07 R2](../07-risks-and-blockers.md)。探针每次运行都会重新打印，可随时复核。

## raw/ — 归档通路（GMGN/BSC，ADR-0001 时期）

采集于 **2026-11-03 11:2x–11:30 +0800**。**这些数据不再决定交易路径**，保留原因见下。

| 文件 | 采集方式 | 保留理由 |
| --- | --- | --- |
| `gmgn_portfolio_info.json` | `gmgn-cli portfolio info --raw` | `{"wallets":[]}`——证明"通路通、缺绑定"，是当时 BLOCKED 判定的一手证据 |
| `bsc_gas_price.json` | `gmgn-cli gas-price --chain bsc` | 单位坑（wei vs gwei）与 `last_block: 1` 脏字段的现场 |
| `cake_token_info.json` / `cake_security.json` | `gmgn-cli token info|security` | honeypot/税率/池深的判定方法示范 |
| `btcb_token_info.json` | `gmgn-cli token info` | BTCB 0.560% 系统偏差的现场 |
| `wbnb_token_info.json` | `gmgn-cli token info` | **WBNB 报价虚高 9.5%** 的现场——「单一 price 不可信」这条纪律的来源 |
| `gmgn_cli_version.txt` | `gmgn-cli --version` | 1.6.2 |
| `env.txt` / `wsl.txt` | 工具链版本命令 | [01](../01-environment-audit.md) |
| `superagent_guard_check.txt` | `pwsh -File D:\ops\tools\superagent-guard.ps1 -Mode Check` | `OrchestratorCode=404`、`Healthy=False`（[01 §四](../01-environment-audit.md)、[07 R13](../07-risks-and-blockers.md)） |

## 复现

```bash
python tools/binance_probe.py --write     # 刷新 binance_*.json（默认不覆盖已存在文件）
python tools/env_audit.py --write         # 刷新工具链与 GMGN 侧（归档通路）
python tools/account_probe.py             # 鉴权只读：权限位 + 余额（无 Key 时退出码 2）
```

三者默认都**不覆盖**已存在的证据文件，要覆盖必须显式加 `--force`。
链上部分需要已配置的 GMGN API Key；账户部分需要 Binance API Key。所有探针都只读，**不会**下单。

## 关于"不可复现"

以下证据是**一次性**的，重跑不会得到同样结果，因此必须留原始文件：

- 价格、24h 成交额、过滤器数值 —— 每秒都在变，[04](../04-venue-audit.md) 的数字只对 **2026-11-03 13:1x–13:2x** 那一刻成立；
- 时钟偏差 —— 两次采样差 2.6 秒，本身就是结论，无法复现只能重测；
- `gmgn_portfolio_info.json` 的空结果 —— 绑定钱包后会变化，届时应**追加**新文件（如 `gmgn_portfolio_info.after-bind.json`）而不是覆盖。

## 本次的一处流程失误（留档）

P0 首轮（11:2x）采集后，`tools/env_audit.py --write` 的第一次运行把 `raw/*.json` **覆盖**成了 11:30:08 的第二次采样，11:2x 那批原始响应因此没有保留。

后果与处置：

- 所有文档只引用落盘的那一份快照，没有出现"引用了一个已不存在的文件"；
- 未落盘的观测（BTCB 偏差 0.33%/0.66%、时钟 −163 ms）只能作为**观察**陈述，不能作为证据引用——[04](../04-venue-audit.md) 与 [07](../07-risks-and-blockers.md) 已按此口径写；
- `env_audit.py` 与 `binance_probe.py` 均已加保护：**默认不覆盖已存在的证据文件**，要覆盖必须显式 `--force`。

这类失误在审计里是实质性的，所以写进文档而不是悄悄修掉。
