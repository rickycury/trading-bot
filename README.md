# trading-bot: Multi-Strategy Trading Engine

A modular trading system for SPY, QQQ, BTC, GLD, and USO running three strategies
(mean reversion, momentum breakout, trend following). Strategies are pure (`history -> Signal`); the
engine owns everything stateful (sizing, risk, stops, persistence, kill
switches), so the **backtester and the live loop run identical strategy code**.

## Layout
```
tradingbot/
  core/            types, strategy ABC, sizing, risk, state, execution, engine
  strategies/      mean_reversion, momentum_breakout, trend_following
  indicators.py    hand-rolled SMA/EMA/ATR/stdev/rolling high-low
  backtest.py      simulated-fill broker + driver + metrics
  data/            pluggable feeds behind one DataFeed interface
    yfinance_feed.py  ETFs, free, no account
    ccxt_feed.py      BTC/USD from a public exchange, free, no account
    composite.py      routes crypto vs stock; free_feed() builds the stack
    alpaca.py         alt feed + paper/live execution (needs an account)
  notify/telegram.py  Telegram alerts implementing the Notifier interface
scripts/
  run_backtest.py  replay all 5 instruments through the real engine
  run_live.py      AlertOnly live loop: Alpaca data in, Telegram alerts out
```

## Quick start

```bash
# from this directory (the one containing tradingbot/ and scripts/)
python3 -m scripts.run_backtest            # synthetic data, no keys, no network
```
That exercises the entire pipeline — sizing, every risk gate, hard stops, the
drawdown breaker, metrics, equity-curve PNG. Synthetic data has **no edge**; it's
a smoke test, not a result.

To measure something real on **free data — no account, no API key, no tax id**:
```bash
pip install -r requirements.txt
python3 -m scripts.run_backtest --source free --years 1
```
yfinance covers the ETFs, CCXT (Kraken) covers BTC. Two limits to know:
- yfinance caps **15-minute** history at ~60 days, so the SPY/QQQ mean-reversion
  backtests are shallow no matter the `--years` you pass (the 1h/4h strategies go
  back ~2 years). The script clamps and warns rather than failing.
- Kraken serves ~720 1h BTC bars (~30 days). For deeper BTC history pass
  `--exchange coinbase` (or `binanceus`).

`--source alpaca` is also available with an Alpaca account (deeper, cleaner
data), but it requires identity verification.

## Components

**Backtester (`tradingbot/backtest.py`).** `BacktestBroker` is an `Execution`
backend that fills against the candle stream and keeps mark-to-market books;
`Backtester` merges all symbols into one time-ordered event stream and replays
them through the real `Engine`, so cross-symbol risk gates fire in the right
order. Costs = adverse slippage (bps, always against you) + per-fill fee (bps).
Metrics: total return, CAGR, max drawdown, daily Sharpe, exposure, win rate,
profit factor, per-symbol breakdown. `result.plot()` writes an equity curve if
matplotlib is present (no-ops otherwise).

**Alpaca (`tradingbot/data/alpaca.py`).** `AlpacaDataFeed.history()` feeds the
backtester; `latest_closed()` drives the live loop. `AlpacaExecution` is the
paper/live order backend for when you promote a strategy — equity entries carry a
broker-resting stop (OTO) so the stop sits at the exchange and fills intrabar.
Defaults to **paper**. `alpaca-py` is optional; the module imports without it and
raises a clear install hint only on use.

**Telegram (`tradingbot/notify/telegram.py`).** `TelegramNotifier` implements the
`Notifier` interface, so it drops into AlertOnly as the thing that reaches your
phone. Network failures never propagate — a flaky messenger can't take the
trading loop down. `self_test()` surfaces a bad token at startup. Setup: DM
@BotFather for a token, hit `getUpdates` for your chat id, then copy
`.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

## Alerts vs. auto-execution
Only **auto-execution** needs a verified broker account. Market data and alerts
don't. So the semi-auto path (bot computes the signal, you place the trade) needs
no broker integration at all — just a data feed and Telegram. `scripts/run_live.py`
runs this today: free data in, Telegram alerts out, zero orders placed. (You still
need *somewhere* to place the manual trades, and any real-money US brokerage
requires a tax id — that's KYC law, not a bot thing.)

The live loop uses **`PaperExecution`** (`core/execution.py`): it tracks each
signal as a paper position — no real orders — which is what makes the bot send the
*full lifecycle*, not just entries:

- 🟢 **ENTRY** — side, qty, capital, $ risk (and % of account), stop, and either a
  🎯 target price (mean-reversion) or "trailing stop, no fixed target" (momentum /
  trend).
- 🟡 **TRAIL** — "tighten your stop to X" as a trend-rider's stop ratchets up
  (throttled to material moves so it doesn't spam).
- 🔴 **EXIT** — close price, estimated P/L, and the reason (target hit, stop hit,
  trend flip, time stop).

Because positions are tracked, the **portfolio rules are live too** — buying-power
cap, 4% total heat, per-bucket caps, and the SPY+QQQ→no-BTC correlation filter.
(`AlertOnly` is still in the box if you ever want entries-only with everything
portfolio-level dormant.)

Graduation path, if/when you add auto-execution:
1. **PaperExecution** (now) — tracked paper positions, full-lifecycle alerts, you
   trade by hand. Are the signals good?
2. **SemiAuto** (approve each trade from Telegram) — execution quality, slippage.
3. **Live, one strategy at a time** — promote as each earns trust.

Phases 2–3 swap the `Execution` backend (`PaperExecution` -> SemiAuto wrapper ->
`AlpacaExecution`) per symbol; the data feed and alerts stay as-is.

## Still to build
- **Position reconciliation on startup** — `AlpacaExecution.sync_positions()`
  reads broker state; wiring it to correct the SQLite store on boot is the next
  step before any live (real-order) trading.
- **SemiAuto backend** — inline-keyboard approve/reject over Telegram.
- **PDT guard** if the account is under $25k and trading ETFs intraday on margin.

Not financial advice. This is engineering infrastructure; whether any of these
edges survive costs is an empirical question for the backtests.
