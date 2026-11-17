# 数据清单 MANIFEST（pull_data.py 自动生成）

- 拉取时间(UTC): 2026-11-17 15:10 UTC
- 目标窗口: 2026-08-19 08:00 UTC .. 2026-11-17 08:00 UTC（最近 90 天，对齐 8h 结算边界）
- 数据源: OKX 官方公开接口 www.okx.com（无密钥）

## 资金费率覆盖与完整性

| 合约 | 条数 | 首条 | 末条 | 相邻间隔分布(ms×次数) |
|---|---|---|---|---|
| BTC-USDT-SWAP | 276 | 2026-08-17 16:00 UTC | 2026-11-17 08:00 UTC | 28800000×275 |
| ETH-USDT-SWAP | 276 | 2026-08-17 16:00 UTC | 2026-11-17 08:00 UTC | 28800000×275 |

（间隔应全部为 28800000=8h；出现其他值即为缺口或结算周期变更，报告中须注明。）

## K线覆盖（1H）

| 交易对 | 条数 | 首条 | 末条 | 缺失小时数 |
|---|---|---|---|---|
| BTC-USDT | 2200 | 2026-08-18 00:00 UTC | 2026-11-17 15:00 UTC | 0 |
| ETH-USDT | 2200 | 2026-08-18 00:00 UTC | 2026-11-17 15:00 UTC | 0 |
| BTC-USDT-SWAP | 2200 | 2026-08-18 00:00 UTC | 2026-11-17 15:00 UTC | 0 |
| ETH-USDT-SWAP | 2200 | 2026-08-18 00:00 UTC | 2026-11-17 15:00 UTC | 0 |

## 全部请求日志（endpoint, 参数, 返回条数）

```
/api/v5/public/funding-rate {"instId": "BTC-USDT-SWAP"} -> 1
/api/v5/public/instruments {"instType": "SPOT", "instId": "BTC-USDT"} -> 1
/api/v5/public/instruments {"instType": "SPOT", "instId": "ETH-USDT"} -> 1
/api/v5/public/instruments {"instType": "SWAP", "instId": "BTC-USDT-SWAP"} -> 1
/api/v5/public/instruments {"instType": "SWAP", "instId": "ETH-USDT-SWAP"} -> 1
/api/v5/public/position-tiers {"instType": "SWAP", "tdMode": "isolated", "instFamily": "BTC-USDT"} -> 99
/api/v5/public/position-tiers {"instType": "SWAP", "tdMode": "isolated", "instFamily": "ETH-USDT"} -> 99
/api/v5/public/funding-rate-history {"instId": "BTC-USDT-SWAP", "limit": 100} -> 100
/api/v5/public/funding-rate-history {"instId": "BTC-USDT-SWAP", "limit": 100, "after": 1792051200000} -> 100
/api/v5/public/funding-rate-history {"instId": "BTC-USDT-SWAP", "limit": 100, "after": 1789171200000} -> 76
/api/v5/public/funding-rate-history {"instId": "ETH-USDT-SWAP", "limit": 100} -> 100
/api/v5/public/funding-rate-history {"instId": "ETH-USDT-SWAP", "limit": 100, "after": 1792051200000} -> 100
/api/v5/public/funding-rate-history {"instId": "ETH-USDT-SWAP", "limit": 100, "after": 1789171200000} -> 76
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1794571200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1794211200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1793851200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1793491200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1793131200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1792771200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1792411200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1792051200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1791691200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1791331200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1790971200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1790611200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1790251200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1789891200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1789531200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1789171200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1788811200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1788451200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1788091200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1787731200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT", "bar": "1H", "limit": 100, "after": 1787371200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1794571200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1794211200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1793851200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1793491200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1793131200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1792771200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1792411200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1792051200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1791691200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1791331200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1790971200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1790611200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1790251200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1789891200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1789531200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1789171200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1788811200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1788451200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1788091200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1787731200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT", "bar": "1H", "limit": 100, "after": 1787371200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1794571200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1794211200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1793851200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1793491200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1793131200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1792771200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1792411200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1792051200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1791691200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1791331200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1790971200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1790611200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1790251200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1789891200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1789531200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1789171200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1788811200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1788451200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1788091200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1787731200000} -> 100
/api/v5/market/history-candles {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1787371200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1794571200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1794211200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1793851200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1793491200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1793131200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1792771200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1792411200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1792051200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1791691200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1791331200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1790971200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1790611200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1790251200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1789891200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1789531200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1789171200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1788811200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1788451200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1788091200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1787731200000} -> 100
/api/v5/market/history-candles {"instId": "ETH-USDT-SWAP", "bar": "1H", "limit": 100, "after": 1787371200000} -> 100
```
