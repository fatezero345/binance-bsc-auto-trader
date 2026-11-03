# P0 总报告

- 任务书：[issue #1](https://github.com/fatezero345/binance-bsc-auto-trader/issues/1)
- 执行时间：2026-11-03 11:1x–13:3x（Asia/Shanghai）
- 场所：**Binance 现货**（[ADR-0002](adr/0002-binance-spot-as-venue.md) 取代 ADR-0001 的 GMGN/BSC 方案）
- 结论：**八项交付 6 项完成、1 项部分、1 项 BLOCKED；真实交易按设计保持关闭。**

## 一、验收项对照（issue §4）

| # | 交付物 | 状态 | 落点 |
| --- | --- | --- | --- |
| 1 | 环境审计报告 | ✅ 完成 | [01](01-environment-audit.md) |
| 2 | 钱包/账户权限审计报告 | ⚠️ **部分**：八项已逐条回答，权限位未实测 | [02](02-wallet-audit.md) |
| 3 | 当前资产只读查询结果 | 🚫 **BLOCKED**：无 API Key | [03](03-holdings-snapshot.md) |
| 4 | 场所审计（原「BSC 网络及交易池」） | ✅ 完成，三条标的三条可行 | [04](04-venue-audit.md) |
| 5 | 独立交易系统架构图 | ✅ 完成 | [05](05-architecture.md) |
| 6 | 项目目录结构 | ✅ 完成（本仓库） | [README](../README.md) |
| 7 | P1–P5 后续实施计划 | ✅ 完成 | [06](06-roadmap-p1-p5.md) |
| 8 | 风险和未解决问题清单 | ✅ 完成 | [07](07-risks-and-blockers.md) |

#3 如实标 BLOCKED，未用任何估算值填充；#4 因场所变更而重写（原「BSC 交易池」项在 CEX 模式下不适用，改为交易所过滤器 + 可行性审计）。

## 二、六条关键结论

### 1. 场所改为 Binance 现货，这是所有权衡里更好的一档

原方案（GMGN 托管钱包）的致命弱点是「一把 Key 全权」——Key 泄漏等于资金全失，本地没有第二道门槛。

Binance 的 API Key 有独立权限位，**提币权可以物理关闭**：

| | Key 泄漏后的最坏后果 |
| --- | --- |
| 提币权开着 | 资金被转走，全失，本地无法拦 |
| **提币权关着** | 只能在本账户内乱交易；损失上界 = 单笔硬上限 5.05 USDT + 熔断 1.50 USDT |

所以 `assess()` 与 `build_order_params()` 各有一条硬断言 `can_withdraw == False`，`tools/account_probe.py` 在检测到提币权开着时**退出码 3 要求停手**，`ci_assert_policy.py` 另外断言策略不允许放弃这条检查。

### 2. 时钟漂移是本轮最不该被忽略的发现

```
13:16  本机 − 服务器 = −163 ms
13:2x  本机 − 服务器 = −2,762 ms
```

策略上限 1000 ms，第二次采样**超限 2.7 倍**。Binance 签名请求带 `timestamp` + `recvWindow`（默认 5000 ms），偏差 2.7 秒意味着往返稍慢就会被 `-1021 Timestamp outside of the recvWindow` 拒掉。

**不修时钟同步，live 模式不可用。** 这不是理论风险，是一小时内实测的漂移。记入 [07 R2](07-risks-and-blockers.md)。

### 3. 资产查询 BLOCKED，没有编数字

`BINANCE_API_KEY` 未设置。代码路径已就绪并已测试：`load_credentials({})` 抛 `CredentialsMissing`，**不会静默返回空余额**——「读不到」和「余额为零」必须是两种不同状态。

钱包地址、Chain ID、USDT/BNB 余额全部如实标 BLOCKED。Chain ID 一项在 CEX 模式下**失去意义**（不是"没测"），报告里保留了原编号以便对上任务书。

### 4. 单笔压到 5.00 之后：只剩 CAKEUSDT 能下单

四条约束同时成立才叫"能下单"：交易所 `minNotional=5.00`、本项目目标 `max_notional=5.00`、量化上浮容许度 100 bps（硬上限 5.05）、stepSize 量化后必须落在 `[5.00, 5.05]` 之内。

```
equity = 29.3 USDT | target 5.0, ceiling 5.0500, exchange floor 5.0
  BNBUSDT    NO  nominal_step=0.6958  quantized 5.5662 > 5.05
  BTCUSDT    NO  nominal_step=0.7664  quantized 5.3648 > 5.05
  CAKEUSDT   OK  nominal_step=0.0184  qty=2.72 @ 1.844 = 5.0048 USDT (17.1% of equity)
```

**资金利用率上限 ~17% 是算术必然。** 更严重的副作用：单笔压到 5.00 后，**BTCUSDT / BNBUSDT 实质退出可交易集合**——它们的名义步长（0.6958 / 0.7664 USDT）比整个允许窗口（0.05 USDT）还宽。判据是「名义步长 = stepSize × price ≤ 窗口宽度」。当前只剩 CAKEUSDT 恒可成交，而它是三个标的中最薄的（24h 1.5 M）。这是 R14 / D7，需要在 P3 之前决定。

### 5. 步长陷阱：会写出"看起来成功、实际被拒"的订单

CAKEUSDT：`5 / 1.845 = 2.7100` → 向下取整 2.71 → **2.71 × 1.845 = 4.99885 < 5** ❌
最小合法量是 **2.72**（5.0184 USDT），必须**向上**对齐。

若代码用 `round()` 会得到一个交易所必然拒掉的订单，而在本地日志里它"已经下了"。因此 `plan_buy_quantity()` 从 `minNotional/price` 向上量化并校验仍在预算内，量化失败一律 reject，不四舍五入、不自动加码。测试 `test_the_cake_trap_floor_is_below_min_notional` 把这条坑钉死在用例里。

### 6. 风控是代码，56 条测试全绿

```
$ python -m unittest discover -s tests
Ran 56 tests in 0.017s
OK
```

覆盖：提币权必须关、权限超集拒绝、账户不可交易拒绝、交易对白名单 / 观察名单、归档场所拒绝、非现货品种（杠杆/合约/借贷/提币/划转）拒绝、步长对齐、minNotional 双向夹逼、风险预算、熔断、持仓数、滑点与手续费上限、名义本金自洽、四道开关缺一不可、以及只读边界（`/api/v3/order` 不在只读白名单、execution 源码不得出现提币路径）。

## 三、四道开关（默认全关）

1. `[execution] enabled = false`
2. `[execution] dry_run = true`
3. 环境变量 `TRADER_LIVE_ORDERS` 未设置
4. **`[execution] allow_mainnet = false`** —— 即使前三条全开，默认也只打测试网

`tools/ci_assert_policy.py` 在 CI 里断言这四条没被悄悄打开——因为**代码写对了但开关被打开时，单测仍然全绿**。

## 四、P0 明确没做的事

- 没有签署或提交任何订单，没有授权任何资产，没有读取或提交任何密钥。
- 没有实测滑点与成交可行性（需测试网 Key，P3）。
- 没有查明时钟漂移根因（B5）。
- 没有把 ETHUSDT / SOLUSDT 转正（它们流动性更好，但需补审计证据，D2）。

## 五、下一步只需要一个动作

按 [02 §七](02-wallet-audit.md) 建 API Key（读 + 现货交易，**关提币**，开 IP 白名单），然后：

```powershell
$env:BINANCE_API_KEY    = "<key>"
$env:BINANCE_API_SECRET = "<secret>"
python tools/account_probe.py
```

`canWithdraw=False` 且余额可读，#2 #3 即从 BLOCKED 转完成。在此之前，任何声称"当前资产 = X"的数字都是编的。
