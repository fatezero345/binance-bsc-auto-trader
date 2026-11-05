# 自动交易系统 · 运维手册

> 这份文档回答一个问题：**出事了，我该看哪里、点哪里。**
> 设计细节见 `docs/00-p0-report.md` 起的那一组；这里只写操作。

---

## 一、系统由什么组成

```
┌─ Windows ─────────────────────────────────────────────────────┐
│                                                               │
│  ① 交易器  pythonw tools/auto_trader.py --loop --interval 300 │
│      每 5 分钟一轮：先平仓，再建仓                             │
│      └ 写入 runs/ledger.jsonl（所有事件的唯一真源）             │
│      └ 维护 runs/state.json（持仓、冷却、灰尘）                 │
│                                                               │
│  ② 心跳   计划任务 bsc-auto-trader  每 5 分钟                  │
│      tools/heartbeat.py                                       │
│      ├ 交易器进程不在        → 拉起                           │
│      ├ 有进程但账本 15 分钟没更新 → 杀掉重来                   │
│      └ 汇报任务被禁用/超期   → 启用 / 补跑                     │
│                                                               │
│  ③ 快照   计划任务 bsc-pnl-report  每 5 分钟                   │
│      tools/pnl_report.py → runs/pnl_report.jsonl + dashboard   │
│                                                               │
│  ④ 成绩单 计划任务 bsc-kronos-scorecard  每 30 分钟            │
│      tools/kronos_scorecard.py（新增平仓 ≥10 笔才重写）        │
│                                                               │
└───────────────────────────────────────────────────────────────┘
                              ▲
                              │ schtasks /run /change
                              │
┌─ WSL ─────────────────────────────────────────────────────────┐
│  ⑤ 外部看护  cron 每 10 分钟                                   │
│      tools/external_watchdog.py                               │
│      ├ 账本 20 分钟没更新 → 踹 Windows 的任务（enable + run）  │
│      └ Kronos 服务不可达  → setsid 重新拉起                    │
│                                                               │
│  ⑥ Kronos 服务  127.0.0.1:8710  仅本机                         │
│      ~/kronos-service/run.sh                                  │
│      POST /predict 给仓位系数（prob_up）                       │
└───────────────────────────────────────────────────────────────┘
```

**为什么⑤在 WSL**：Windows 侧的①~④互相看护，但**没人看护②本身**。
如果②被禁用或计划任务服务出问题，整条链断掉且无人知晓。
WSL 和 Windows 是两个独立内核，Windows 调度器全挂时 WSL 的 cron 照样跑。

---

## 二、出事了看哪里

**第一站永远是账本**：`runs/ledger.jsonl`。它是唯一的真源，
`state.json` 可以被 reconcile 重建，账本不会。

```powershell
# 最近 20 条
Get-Content runs\ledger.jsonl -Tail 20

# 只看成交
Get-Content runs\ledger.jsonl | Select-String '"kind": "(bought|sold)"' | Select-Object -Last 10

# 只看异常
Get-Content runs\ledger.jsonl | Select-String 'error|failed|blocked|unknown'
```

---

## 三、故障决策树

### 症状：面板数字不动了

```powershell
Get-ScheduledTask bsc-pnl-report
(Get-ScheduledTaskInfo bsc-pnl-report).LastRunTime
```

- 状态 `Disabled` → 心跳会在 5 分钟内启用它；急的话手动：
  `Enable-ScheduledTask -TaskName bsc-pnl-report; Start-ScheduledTask -TaskName bsc-pnl-report`
- 状态 `Ready` 但上次运行很久以前 → 同上，直接 `Start-ScheduledTask`
- 面板顶部出现「数据已过期 N 分钟」横幅 = 这套检测生效了，按上面处理

### 症状：不再交易（长时间没有 bought）

按顺序查：

```powershell
# 1. 进程在吗
Get-Process pythonw

# 2. 账本还在更新吗（超过 15 分钟没新行 = 卡住）
(Get-Item runs\ledger.jsonl).LastWriteTime

# 3. 是不是被闸门/冷却/日内上限挡住了（看 ledger 的 skip/cooldown/daily_loss_halt）
Get-Content runs\ledger.jsonl -Tail 30

# 4. 熔断了吗
Get-Content runs\ledger.jsonl | Select-String 'circuit_breaker|daily_loss_halt'
```

### 症状：卖出失败

```powershell
Get-Content runs\ledger.jsonl | Select-String 'order_failed|sell_blocked|order_state_unknown'
```

- `-1013 Filter failure: NOTIONAL` → 残量低于交易所最小名义，会被 `dust_unsellable` 一次性写掉（不会重试循环）
- `-2010` → 下单量超过余额；卖出走的是**真实可用余额**，不该出现，若出现查 `reconcile`
- `order_state_unknown` → 网络中断导致状态未知，下一轮会用 `query_order` 回查

### 症状：Kronos 服务挂了

```bash
# WSL 里
curl -s http://127.0.0.1:8710/health
pgrep -af "[u]vicorn app.main"
```

外部看护会在 10 分钟内自动拉起。急的话：

```bash
cd ~/kronos-service && setsid nohup ./run.sh > service.log 2>&1 < /dev/null &
```

**必须用 `setsid`**：普通后台进程会随启动它的会话一起死（踩过两次）。

### 症状：整台机器没人管了

WSL 也停了（`wsl --shutdown`、重启后没进过 WSL）。这时所有自愈都失效，
只能人工介入：

```powershell
Start-ScheduledTask -TaskName bsc-auto-trader
```

---

## 四、紧急停止

```powershell
# 立刻停止交易（最常用）
Get-Process pythonw | Stop-Process -Force

# 防止心跳把它拉起来
Disable-ScheduledTask -TaskName bsc-auto-trader

# 彻底停（含汇报任务）
foreach ($n in 'bsc-auto-trader','bsc-pnl-report','bsc-kronos-scorecard') {
  Disable-ScheduledTask -TaskName $n
}
```

恢复：把上面的 `Disable` 换成 `Enable-ScheduledTask`，再
`Start-ScheduledTask -TaskName bsc-auto-trader`。

**停止不会平仓**。要清空持仓用 `python tools/auto_trader.py --flatten`。

---

## 五、风控参数在哪里

`config/risk_policy.toml`：

| 参数 | 现值 | 含义 |
| --- | --- | --- |
| `max_notional_per_order_usdt` | 7.92 | 单笔硬上限（加量化余量后 8.00） |
| `max_risk_budget_per_order_usdt` | 0.40 | 单笔最大亏损（= 上限 × 5% 止损） |
| `max_open_positions` | 3 | 同时持仓数 |
| `circuit_breaker.max_cumulative_realized_loss_usdt` | 1.50 | **累计**净亏损到这数就停止开新仓 |
| `circuit_breaker.max_daily_realized_loss_usdt` | 0.80 | **当日**净亏损到这数就停止开新仓 |

两个熔断都是**净额**口径（盈利抵扣亏损），且只停新仓，不影响平仓。

> 2026-11-05 之前这两个值都是摆设：`PortfolioState.realized_loss_usdt`
> 在调用点被硬编码成 `Decimal("0")`，熔断条件永远不成立。现在从账本实算。

---

## 六、Kronos 的定位与边界

它**只决定仓位大小**，不决定买卖：

```
仓位系数 = prob_up，夹在 [0.25, 1.0]
预算 = min(单笔上限, 剩余现金/剩余仓位) × 系数
       ↑ 缩小后若低于交易所最小可成交额，抬到最小值并记 kronos_floor
```

它不能：开一个被闸门拒绝的机会、放大仓位上破上限、碰钱包、签交易。
服务不可用时退化为系数 1.0（满额）并记 `kronos` + `error`，不阻断交易。

**它目前没有可验证的预测力**（IC 不显著、微调后收敛到零、98% 时间区间跨零）。
`runs/kronos_scorecard.md` 用真实成交在检验它：系数高的单子是否真的赚更多。
样本不足 50 笔/组之前，任何结论都只是运气。

---

## 七、已知边界（不要指望这些）

| 边界 | 说明 |
| --- | --- |
| WSL 停了外部看护也停 | 真正的第三处兜底需要云服务/另一台机器 |
| 心跳自己无人看护 | 依赖 WSL 侧⑤ |
| 无推送告警 | 只有日志与面板；异常需主动看 |
| 策略无正期望证据 | 回测 479 笔 −42%，劣于买入持有；实盘盈利主要来自 beta |
| GPU 未启用 | Kronos 跑 CPU，2.2s/16 采样 |

---

## 八、日常检查清单（30 秒）

```powershell
Get-Process pythonw                                   # ①在跑吗
(Get-Item runs\ledger.jsonl).LastWriteTime            # 2 分钟内有更新吗
foreach ($n in 'bsc-auto-trader','bsc-pnl-report','bsc-kronos-scorecard') {
  "$n = $((Get-ScheduledTask $n).State)"
}
```

```bash
# WSL
curl -s http://127.0.0.1:8710/health | head -c 80
crontab -l | grep external_watchdog
```

四行命令覆盖全部六个组件。全绿就没问题。
