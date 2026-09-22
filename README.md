# Investment Radar — market scanner via GitHub Actions

Automated crypto radar. Runs on a schedule via `.github/workflows/scan.yml`,
writes its results as JSON under `data/` and `config/`, and commits them back
to the repo. No server, no database — the repo itself is the datastore.

## Pipeline (v15)

Five stages, run in this order every cycle (`scripts/scan.py` →
`scripts/check_liquidity.py` → `scripts/breakout_check.py` →
`scripts/auto_paper_trade.py` → `scripts/scalp_signals.py` →
`scripts/track_trades.py` → `scripts/evaluate_signals.py`):

1. **Data collection** — `scan.py` pulls the top ~250 CoinGecko coins +
   watchlist every run, writes the full universe to `data/market-scan.json`,
   accumulates 15-min price snapshots per coin in `data/price-history.json`,
   and keeps a top-40-by-flag-count subset in `data/radar-flags.json`.
2. **Cheap universal screening (Layer 2, added v15)** — also inside
   `scan.py`, runs on every coin with accumulated price-history (not just
   the current top 40) at zero extra API cost: synthetic 4h candles built
   from the price snapshots feed CHoCH/BOS structure detection, an ATR
   volatility-squeeze check, and bullish RSI divergence; `categories.json`
   feeds sector/cluster-rotation-lag detection; BTC's own price-history
   feeds relative-strength-during-consolidation. A hit sets
   `priority_review: true` on that coin's record — it does **not** feed the
   confidence score and never opens a trade by itself, it only earns the
   coin an immediate deep-evaluation slot (see Layer 3).
3. **Priority queue** — `breakout_check.py`'s `select_rotating_candidates()`
   normally rotates through 16 of the current Binance-listed candidates per
   run (full coverage takes several runs). Since v15, any coin flagged
   `priority_review` in this run is added on top of the rotation slot,
   regardless of whose turn it is — this is what fixes the detection-latency
   gap the 22/9/2026 audit found (confirmed live on ZAMA).
4. **Deep evaluation** — the rest of `breakout_check.py`: resistance/
   trendline breakouts, fibonacci extension continuation, EMA trend filter,
   OI/funding via OKX, known-unlock proximity, and the v8 composite
   `confidence_score` (weights in `config/indicator-weights.json`). v14 adds
   Trend-Following Entry mode (ATR-based stop/targets) for coins on a
   sustained multi-run streak with no clean pullback.
5. **Execution + logging** — `auto_paper_trade.py` opens a real paper trade
   once `confidence_score` clears `AUTO_TRADE_MIN_SCORE`, else logs it to
   `data/shadow-trades.json` ("what would have happened if we'd said yes
   anyway"). `scalp_signals.py` runs a separate, looser-threshold
   data-collection track (`data/scalp-trades.json`) — distinct from the
   real `/SCALP` fund, which still requires a full manual Agent Room review
   before any real money moves. `track_trades.py` checks all three tracks
   against current prices and updates the three `*-performance-summary.json`
   files. Every full Agent Room review (manual, by Claude) should be logged
   with `scripts/record_verdict.py` into `data/agent-room-log.json` — this
   is what eventually lets `config/indicator-weights.json` be recalibrated
   from real outcomes instead of left as opinion.

## Design rule

A Layer-2 early signal only escalates a coin for deep evaluation. It never
triggers Layer-5 trade execution directly — every real or shadow trade still
has to earn it through `confidence_score` in the normal way.

## File map

| File | Written by | Purpose |
|---|---|---|
| `data/market-scan.json` | scan.py | full scanned universe, every run |
| `data/radar-flags.json` | scan.py, breakout_check.py | flagged coins + all deep-evaluation fields |
| `data/price-history.json` | scan.py | 15-min price snapshots per tracked coin |
| `data/indicators.json` | scan.py | RSI/EMA approximated from price-history |
| `data/categories.json` | scan.py | CoinGecko category membership, refreshed ~daily |
| `config/known-unlocks.json` | manual | hand-researched unlock calendar — a missing entry means "never checked", not "confirmed safe" (`unlock_data_checked`) |
| `config/indicator-weights.json` | manual | confidence-score weight overrides |
| `config/trades.json` | auto_paper_trade.py, manual | real/auto paper trades |
| `data/shadow-trades.json` | auto_paper_trade.py | signals that fired below the auto-trade bar |
| `data/scalp-trades.json` | scalp_signals.py | separate data-collection track, not the real SCALP fund |
| `data/*-performance-summary.json` | track_trades.py | win rate / profit factor per track |
| `data/agent-room-log.json` | record_verdict.py (manual trigger) | logged outcome of every full Agent Room review |
| `data/backtest-results.json` | separate backtest script | raw-signal vs buy-and-hold baseline |

## Scheduling

`scan.yml` runs every 15 minutes (`workflow_dispatch` also available for a
manual trigger). A full run takes ~21 minutes, so `concurrency: {group:
radar-scan, cancel-in-progress: false}` queues an overlapping trigger
instead of racing two runs against the same files (added v15 — this is also
why the commit step retries with `git pull --rebase`).
