# binance-bsc-auto-trader

Binance 现货上的 AI 辅助自动投资系统。当前阶段：**P0 —— 只审计与设计，不接真实交易**。

初始实验资金：**29.30 USDT**。这不是收益承诺，是实验上限。

> 仓库名里的 `bsc` 是历史。2026-11-03 起交易场所改为 **Binance 现货**，原 GMGN/BSC 通路已归档，见 [ADR-0002](docs/adr/0002-binance-spot-as-venue.md)。

## 当前状态

| 项 | 状态 |
| --- | --- |
| P0 环境审计 | ✅ [docs/01](docs/01-environment-audit.md) |
| P0 账户权限审计 | ⚠️ 八项已答，权限位未实测 [docs/02](docs/02-wallet-audit.md) |
| P0 资产只读查询 | 🚫 BLOCKED：无 API Key [docs/03](docs/03-holdings-snapshot.md) |
| P0 场所审计（过滤器 + 可行性） | ✅ [docs/04](docs/04-venue-audit.md) |
| P0 系统架构 | ✅ [docs/05](docs/05-architecture.md) |
| P0 目录结构 | ✅ 本仓库 |
| P0 P1–P5 计划 | ✅ [docs/06](docs/06-roadmap-p1-p5.md) |
| P0 风险与未决 | ✅ [docs/07](docs/07-risks-and-blockers.md) |
| **真实交易** | **关闭**（四道开关，见下） |

汇总入口：[docs/00-p0-report.md](docs/00-p0-report.md)。

## 关键设计决定

**场所 = Binance 现货；本地只有 API Key，没有私钥。**

Binance 的 API Key 可以**关闭提币权限**。因此 Key 泄漏的最坏后果从「资金被转走」降级为「资金被乱交易」，而后者的损失上界是单笔 10 USDT + 熔断 1.50 USDT。

这条前提由三处代码硬断言，不是文档承诺：`assess()`、`build_order_params()`、`tools/account_probe.py`（退出码 3）。

## 四道开关（缺一不可，默认全关）

1. `config/risk_policy.toml` → `[execution] enabled = false`
2. `config/risk_policy.toml` → `[execution] dry_run = true`
3. 环境变量 `TRADER_LIVE_ORDERS` 未设置
4. `config/risk_policy.toml` → `[execution] allow_mainnet = false`（即使前三条全开，也只打测试网）

硬资金限制（单笔 5–10 USDT、单笔风险 ≤ 0.30 USDT、累计亏损 1.50 USDT 熔断、最大持仓 1）由 `src/risk/` 独立执行，策略层无权读写。

## 目录

```
config/risk_policy.toml   硬风控参数（TOML，stdlib tomllib 可读，零依赖）
src/risk/                 ★ 独立风控闸门：assess() / execution_gate() / 量化
src/market/               Binance 公开行情（只读，无需鉴权）
src/wallet/               Binance 账户只读（只读路径白名单，无下单/提币）
src/execution/            唯一可下单的模块（当前恒不触网）
src/strategy/             纯决策层（不得批准订单）
src/backtest/ src/paper/ src/ledger/ src/reporting/
tests/                    56 条验收测试（stdlib unittest，零第三方依赖）
docs/                     P0 交付物 + ADR + 原始证据
tools/                    可复现的审计与探针脚本
```

依赖方向单向：`strategy → risk → execution`。`strategy` 不得 import `execution`；`market` 与 `wallet` 只读。

## 跑起来

```bash
python -m unittest discover -s tests -v     # 56 条验收测试
python tools/ci_assert_policy.py            # 四道开关仍关 + 场所未回退
python tools/check_links.py                 # 文档相对链接
python tools/binance_probe.py               # 只读：连通性 / 过滤器 / 可行性
python tools/account_probe.py               # 鉴权只读：权限位 + 余额（无 Key 时 BLOCKED）
```

零第三方依赖。Python ≥ 3.11（`tomllib`）。

## 不做什么（写进代码，不只是承诺）

- 不持有私钥；API 密钥只从环境变量读，不落盘、不进日志
- 不做杠杆、合约、借贷（`forbidden_modes` 显式列出，`src/execution/` 里没有这些路径）
- 不做提币与划转（`src/execution/` 内不存在相关函数或常量，源码级测试断言）
- 不在 P0 阶段提交任何订单

## 许可

未定。P0 阶段暂不添加许可证文件。
