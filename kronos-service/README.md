# Kronos 推理服务（P1）

把清华 Kronos 金融 K 线基础模型接成一个**独立的本地推理服务**，通过 HTTP 供 DSH 调用。
模型只做预测，**不接触钱包、不签交易、不下单**。

---

## 1. 环境实测（2026-11-04）

| 项 | 实测值 | 影响 |
| --- | --- | --- |
| OS | Ubuntu 24.04.5 LTS, WSL2, kernel 6.18 | — |
| CPU | i7-12700H, 20 线程 | 够用，torch 限 8 线程 |
| **内存** | **WSL 上限 4 GB，可用 2.8 GB** | **硬约束**，见下 |
| GPU | NVIDIA MX550, **2 GB VRAM** | 太小，未启用 |
| CUDA | 无 nvcc（WSL 未装 toolkit） | 同上 |
| Python | 3.12.3 | 与 Kronos 的 pandas==2.2.2 声明不符，见"已知偏差" |
| 磁盘 | 939 GB 可用 | 充裕 |

**结论：走 CPU。** 模型峰值内存 423 MB，服务常驻 327 MB，在 2.8 GB 预算内。

GPU 不启用是刻意的：2 GB 显存装不下 cuda 版 torch 的运行时开销，且本机没有
CUDA toolkit；CPU 上 16 条采样 2.2 秒，对 4H/1D 周期完全够用。

---

## 2. 模型

| 变体 | 模型 | Tokenizer | max_context | 参数 |
| --- | --- | --- | --- | --- |
| **mini（已部署）** | `NeoQuasar/Kronos-mini` | `Kronos-Tokenizer-2k` | **2048** | 4.1 M |
| small（可切换） | `NeoQuasar/Kronos-small` | `Kronos-Tokenizer-base` | 512 | 24.7 M |

模型缓存在 `data/hf`（`HF_HOME`），**只在首次加载时下载**，之后不联网。

切换 small：

```bash
KRONOS_VARIANT=small ./run.sh
```

---

## 3. 启动

```bash
cd ~/kronos-service
./run.sh                      # 默认 127.0.0.1:8710
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `KRONOS_VARIANT` | `mini` | `mini` / `small` |
| `KRONOS_DEVICE` | `cpu` | `cpu` / `cuda` |
| `KRONOS_PORT` | `8710` | 端口 |
| `KRONOS_THREADS` | `8` | torch 线程（本机 20 核，但内存吃紧） |
| `KRONOS_TIMEOUT_S` | `120` | 单次推理超时 |
| `KRONOS_DB` | `data/predictions.db` | SQLite 落库路径 |

> **注意**：`pkill -f "uvicorn app.main:app"` 会**杀掉执行它的 shell 自己**
> （命令行里含同样字符串）。用 `pkill -f "[u]vicorn app.main"`。
> 这个坑在部署时实际踩过一次，退出码 15。

---

## 4. 接口

### GET /health

```json
{"status":"ok","model":{"name":"NeoQuasar/Kronos-mini","loaded":true,"load_seconds":2.05},
 "device":"cpu","torch":"2.15.0+cpu","version":"1.0.0","uptime_s":2.5}
```

模型加载失败时 `status` 为 `degraded`，`load_error` 带原因，**进程不倒**。

### POST /predict

输入至少 32 根、最多 2048 根 OHLCV；`pred_len` 1–120；`sample_count` 1–64。

```json
{
  "symbol": "CAKEUSDT", "interval": "4h",
  "bars": [{"timestamp":"2026-11-03T00:00:00","open":1.9,"high":1.95,"low":1.85,"close":1.92,"volume":12000}],
  "lookback": 400, "pred_len": 6, "sample_count": 16,
  "temperature": 1.0, "top_p": 0.9, "drop_unclosed": true
}
```

返回（节选）：

```json
{
  "model": "NeoQuasar/Kronos-mini",
  "last_ts": "2026-11-04 04:00:00",
  "forecast_range": {"start":"2026-11-04 08:00:00","end":"2026-11-05 04:00:00"},
  "last_close": 1.919,
  "mean_ohlcv": [ ... 6 根 ... ],
  "distribution": {
    "final_close": {"mean":1.905,"median":1.905,"p10":1.834,"p90":1.955,"std":0.048},
    "close_change_pct": {"mean":-0.66,"p10":-4.45,"p90":1.86,"prob_up":0.375},
    "high_low_range": {...},
    "closes": [[...6 个 close...], ... 共 16 条路径]
  },
  "elapsed_ms": 2239,
  "data_quality": {"ok": true, "rows": 400, "unclosed_bar": false, ...}
}
```

**`distribution.closes` 是逐条采样路径的原始数值，服务端不做任何加工或替换。**

### GET /metrics

推理次数、失败次数、平均耗时、RSS、loadavg、最后一条错误。

---

## 5. 为什么能拿到"分布"

官方 `KronosPredictor.generate()` 内部有一行：

```python
preds = np.mean(z.reshape(-1, sample_count, H, F), axis=1)   # sample_count 被平均掉
```

所以公共 `predict()` 永远只返回一条均值路径，**拿不到单条采样**。

本服务没有改模型，只是换了入口：把同一段历史复制 N 份走 `predict_batch`，
每份 `sample_count=1` —— batch 维不被平均，于是得到 N 条**互相独立**的采样路径。

```python
preds = predictor.predict_batch(df_list=[df]*N, x_timestamp_list=[ts]*N,
                                y_timestamp_list=[y]*N, sample_count=1, ...)
```

---

## 6. 数据校验（全部会拒绝推理）

| 检查 | 处理 |
| --- | --- |
| 缺列 | 422 |
| 时间戳重复 | 422 |
| OHLC 非法（high<low、非正价） | 422 |
| **未收盘 K 线** | 默认自动剔掉最后一根；`drop_unclosed=false` 时 422 |
| K 线间隔缺口 | 记入 `notes`，不拒绝 |
| 成交量异常（\|z\|>8） | 记入 `notes`，不拒绝 |
| 请求样本数越界 | 422（pydantic 约束） |

未收盘 K 线是硬红线：最后一根的高低价随时会变，拿它当历史等于用了未来函数。

---

## 7. 端口与暴露面

```
LISTEN 0 2048 127.0.0.1:8710 0.0.0.0:*
```

只绑回环。WSL2 的 `mirrored` 网络模式下，Windows 侧的 127.0.0.1:8710 可直达，
不需要端口转发，也**不对局域网/公网开放**。

---

## 8. DSH 接入

`kronos_adapter.py`（纯标准库）：

```python
from kronos_adapter import KronosClient
kc = KronosClient()                    # http://127.0.0.1:8710
kc.available()                         # 服务活着且模型已加载
fc = kc.predict("CAKEUSDT", "4h", bars, lookback=400, pred_len=6, sample_count=16)
if fc and fc.is_confident:
    ...
kc.record(fc, path="runs/kronos_signals.jsonl")
```

**边界（硬性）**：

- 适配器不 import 任何钱包/下单模块，不持有私钥；
- 预测失败返回 `None`，调用方必须按"没有预测"处理 ——
  **不允许在预测失败时生成交易信号**；
- `is_confident` 要求 `prob_up` 偏离 0.5 超过 0.15 **且** p10/p90 区间不跨 0；
  区间跨 0 时模型自己都说不好方向，任何方向判断都是噪声；
- 预测上涨 ≠ 买入。最终否定权在 `src/risk/policy.py::assess()`。

---

## 9. 已知偏差与局限（必须写在验收报告里）

1. **pandas 版本**：Kronos 声明 `pandas==2.2.2`，实际装的是 `3.1.0`；
   `numpy` 声明兼容 2.x，实际 `2.5.4`。目前推理正常，但上游若用了
   已被移除的 pandas API 会在特定路径下报错。要稳妥可降级：
   `pip install "pandas==2.2.2" "numpy<2.3"`。
2. **数据口径**：用的是 Binance 现货 OHLCV，**volume 是交易所成交量**，
   不是 DEX 成交量。BTCB/WBNB 是 PancakeSwap 交易对，CEX 无直接对应，
   此处用 `BTCUSDT` 作价格代理。两者量级差一个数量级，且 DEX 没有"池深"概念，
   所以模型的 volume 特征不能跨口径泛化。
3. **GPU 未启用**：2 GB 显存 + 无 CUDA toolkit。CPU 上 2.2s/16 样本；
   若要更高频（1m/5m）需要换机器。
4. **不做训练/微调**：用的是预训练权重，全程冻结。

---

## 10. 目录

```
kronos-service/
├── Kronos/                 上游仓库，**未改动**
├── app/
│   ├── core.py             模型加载、数据校验、采样分布
│   └── main.py             FastAPI：/health /predict /metrics + SQLite 落库
├── tools/
│   ├── fetch_data.py       拉 OHLCV（丢未收盘 K 线）
│   ├── smoke_test.py       接口冒烟（含 4 种必须被拒的脏数据）
│   └── evaluate.py         MAE/RMSE/MAPE/方向/覆盖率 + 三个基准
├── kronos_adapter.py       DSH 侧适配器（纯标准库）
├── run.sh
└── data/
    ├── hf/                 模型缓存
    ├── predictions.db      预测落库
    ├── <SYM>_<IV>.json     OHLCV 缓存
    └── reports/            评估报告
```
