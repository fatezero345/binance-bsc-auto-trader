# kronos-service（镜像）

这里只放**源码与报告**。真正的运行环境在 WSL：

    ~/kronos-service/          .venv / Kronos 上游 / 模型缓存 / 数据库

不镜像进来的东西（体积大、可重建）：
    .venv/          依赖（按 README 重装）
    Kronos/         上游仓库（git clone 即可）
    data/hf/        模型缓存（首次加载自动下载）
    data/*.json     OHLCV 缓存（tools/fetch_data.py 可重建）
    data/predictions.db

部署与验收细节见 README.md 与 reports/P1-ACCEPTANCE.md。
