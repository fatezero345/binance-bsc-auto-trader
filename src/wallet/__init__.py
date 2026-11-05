"""只读钱包/账户层。

ADR-0002：交易场所为 Binance 现货。本地持有的是 **API Key**，不是私钥；
密钥从环境变量读取，不落盘、不入日志。

Binance API Key 可关掉提币权限，因此本层最重要的产出是
`AccountPermissions.can_withdraw == False` 这个事实——
它是整个 CEX 方案的安全前提，由 src/risk/ 强制校验。

`gmgn_bsc` 通路已归档（ADR-0002），代码保留但其意图会被风控拒绝。
"""
