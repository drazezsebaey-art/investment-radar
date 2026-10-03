# Lab data probe (RC-1.1) - 2026-10-03T06:17:10.671767+00:00

**Decision:** venue **BINANCE**, universe **Tier A** - Binance archive reachable and lists delisted pairs -> point-in-time universe possible

| Check | Result |
| --- | --- |
| archive spot_1h_2024_01 | 200  rows=744 ts=milliseconds |
| archive spot_1h_2025_06 | 200  rows=720 ts=microseconds |
| archive futures_funding_2024_01 | 200  rows=94 ts=milliseconds |
| archive futures_metrics_2024_01_01 | 200  rows=289 ts=- |
| REST api.binance.com | 451 HTTP 451 |
| REST data-api.binance.vision | 200  |
| REST fapi.binance.com | 451 HTTP 451 |
| depth ref_1h | first 2017-08 last 2026-08 n=109  |
| depth funding | first 2020-01 last 2026-09 n=81  |
| depth metrics | first 2020-09-01 last 2026-10-01 n=2222  |
| OKX fallback | 200 rows=5 first=2024-01-01T19:00:00+00:00 |
| archive symbols (all) | 3710 |
| USDT pairs / active / delisted | 735 / 491 / 244 |
| USDT pairs with data since 2024-01 | 481 |
