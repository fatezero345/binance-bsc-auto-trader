# 01 环境审计

采集时间：2026-11-03 11:2x（Asia/Shanghai）。原始输出：[evidence/raw/env.txt](evidence/raw/env.txt)、[evidence/raw/wsl.txt](evidence/raw/wsl.txt)、[evidence/raw/superagent_guard_check.txt](evidence/raw/superagent_guard_check.txt)。

## 一、宿主机

| 组件 | 版本 / 值 | 采集命令 |
| --- | --- | --- |
| OS | Windows（`Win32_OperatingSystem.Caption`） | `Get-CimInstance Win32_OperatingSystem` |
| Node.js | **v24.19.0** | `node -v` |
| npm | 11.17.0 | `npm -v` |
| Python | **3.13.15** | `python --version` |
| Git | 2.55.0.windows.3 | `git --version` |
| GitHub CLI | 2.97.0 | `gh --version` |

## 二、WSL2

| 项 | 值 |
| --- | --- |
| 发行版 | **Ubuntu-24.04**，STATE = Running，VERSION = **2** |
| 内核 | `6.18.54.1-microsoft-standard-WSL2` x86_64 |
| Python | **3.12.3** |
| Node.js | v24.21.0 |

> 任务书要求「Windows + WSL2 Ubuntu」，实测成立。宿主与 WSL 两套工具链都在，但版本不同（Python 3.13.15 vs 3.12.3，Node 24.19.0 vs 24.21.0）。P1 起必须锁定只用一套，否则 "在我机器上能跑" 会成为常态。**建议锁定宿主 Python 3.13**（`requires-python = ">=3.11"` 兼容），WSL 只用于需要 Linux 原生的工具。

## 三、GitHub 身份（双轨，本次踩到了）

| 身份 | 凭据 | 对目标仓库权限 |
| --- | --- | --- |
| `tanglangbc` | `gh`（keyring，scope: gist/read:org/repo/workflow） | `push: false` |
| `fatezero345` | `gh-agent.exe auth git-credential`（keyring） | `admin: true, push: true` |

**含义**：默认 `gh` 登录的是 `tanglangbc`，无权推本仓库；superagent 的 worker 用 `~/.dsh/state/superagent/worker-gitconfig` 里的 credential helper 指向 `gh-agent.exe`，那才是 `fatezero345`。本次 P0 的 clone 用无认证 HTTPS（公开仓库只读），push 走 `gh-agent.exe`。

**风险**：任何人（或任何 agent）在本目录里直接 `git push` 会以 `tanglangbc` 身份失败或推到错误的地方。已记入 [07 风险 R4](07-risks-and-blockers.md)。

## 四、DSH / SuperAgent 运行状态

`pwsh -File D:\ops\tools\superagent-guard.ps1 -Mode Check`（11:21:59）原始结果：

```
At               : 2026-11-03T11:21:59.2683140+08:00
Port             : 64931
OrchestratorCode : 404
RuntimeState     :
RuntimeDetail    :
GithubState      :
GithubDetail     :
Healthy          : False
LockExists       : True
LockEntries      : 0
LockMtime        : 2026/11/3 11:04:20
LockRemovable    : True
DshRunning       : True
MainCount        : 1
MainPid          : 21728
ServicePid       : 18420
OtherServers     : {19504, 25040, 26492}
```

**判定口径**：`OrchestratorCode = 404` + `Healthy = False`，而 `DshRunning = True`。这正是交接文档描述的形态——DSH 进程活着，但 superagent 路由失活，且存在残留锁（`LockExists=True`、`LockEntries=0`、`LockRemovable=True`）。

**后果**：issue #1 里的 `@fatezero345 请执行此Issue` 不会被任何自动化接管。必须人工介入：

```powershell
pwsh -File D:\ops\tools\superagent-guard.ps1 -Mode Heal -Yes -Force
```

（`-Force` 会先摘锁再杀主进程。**这会重启当前 DSH 会话**，因此 P0 期间没有执行，避免自断。）

## 五、交易场所能力面

ADR-0002 之后，主通路是 Binance 现货 REST；DEX 侧工具保留但已归档。

### Binance（当前主通路）

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `api.binance.com` 连通 | ✅ HTTP 200，`/api/v3/time` | [evidence/raw/binance_server_time.json](evidence/raw/binance_server_time.json) |
| `/api/v3/exchangeInfo` | ✅ 可读三条白名单的过滤器 | [evidence/raw/binance_exchange_info.json](evidence/raw/binance_exchange_info.json) |
| `/api/v3/ticker/24hr` | ✅ 可读价格与 24h 成交额 | [evidence/raw/binance_ticker_24hr.json](evidence/raw/binance_ticker_24hr.json) |
| 时钟偏差 | ⚠️ **−163 ms → −2,762 ms（超策略上限 1000 ms）** | [04 §一](04-venue-audit.md) |
| `BINANCE_API_KEY` / `SECRET` | ❌ **未设置** → 账户级全部 BLOCKED | [03](03-holdings-snapshot.md) |
| 测试网 `testnet.binance.vision` | ✅ 可达（同一套 API 形状） | `tools/binance_probe.py` |

### DEX 侧（已归档，ADR-0002）

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `gmgn-cli` | ✅ 1.6.2，`C:\Users\fixadmin\.local\bin\gmgn-cli.cmd` | [evidence/raw/gmgn_cli_version.txt](evidence/raw/gmgn_cli_version.txt) |
| GMGN API Key | ✅ 已配置 `~/.config/gmgn/.env`（390 B，2026-09-12） | `gmgn-cli config --check` 退出 0 |
| 已绑定钱包 | ❌ `{"wallets":[]}` | [evidence/raw/gmgn_portfolio_info.json](evidence/raw/gmgn_portfolio_info.json) |
| Foundry（`cast`/`forge`） | ❌ 未安装 | `Get-Command cast` 无结果 |
| Binance Agentic Wallet 技能 | ❌ 不存在（技能目录 0 匹配） | 已由 ADR-0002 绕开，不再需要 |

## 六、结论

1. 宿主与 WSL2 环境满足运行前提；工具链齐备，零第三方 Python 依赖即可跑通全部审计与测试。
2. **任务书假设的「Binance Agentic Wallet 工作台」不存在**；所有者随后要求改为币安自动交易模式，据此走了 CEX 现货通路（ADR-0002），该前提不再需要成立。
3. **Binance 公开接口全部可用**，但账户级接口因缺 API Key 而 BLOCKED（[03](03-holdings-snapshot.md)）。
4. **本机时钟不稳**：一小时内偏差从 −163 ms 漂到 −2,762 ms，已超策略上限。这是接入 live 之前必须解决的环境问题（[07 R2](07-risks-and-blockers.md)）。
5. superagent 编排器当前 404，P0 全程人工驱动，未依赖自动化。
