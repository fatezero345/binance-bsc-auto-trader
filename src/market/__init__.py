"""只读行情层。

ADR-0002：主通路为 Binance 公开 REST（/api/v3/time、/exchangeInfo、/ticker/*），
无需鉴权。本包只读：不得 import src.execution，不得发起任何下单请求。

必须记住的两条（来自 P0 审计）：
  1. 任何单一 price 字段都不足以作为定价依据 —— DEX 侧已实证 WBNB 报价虚高
     9.5%（池深仅 369 USD）。CEX 侧同样要用 24h quoteVolume 判断深浅。
  2. 交易所过滤器（stepSize / minNotional）不是建议值，是硬约束；
     量化失败必须拒绝，不得四舍五入。
"""
