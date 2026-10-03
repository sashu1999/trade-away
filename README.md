# trade-away

An AI trading agent that paper trades US stocks, ETFs and crypto on live data, so results can be judged before any real money is involved. The full design is in the [system design doc](https://claude.ai/code/artifact/066b0060-85dc-41a4-8088-6ddcddffdf10).

Built so far:

- **Phase 0, foundation:** live prices into SQLite, a nightly screen of the whole US market, history backfill, news and a dashboard.
- **Phase 1, rules-only baseline:** two strategies, hard risk limits, a backtester and paper trading with a decision journal. No AI yet. This is the baseline the AI agent has to beat in Phase 2.

## What's here

| Command | What it does |
| --- | --- |
| `trade-away check` | Checks your keys and paper account, and whether your plan includes news |
| `trade-away screen` | Pulls daily bars for every tradable US stock and ETF, then keeps the liquid ones (price above $5, over $20M traded a day) ranked by dollar volume |
| `trade-away backfill` | Fetches 2 years of daily history for the top 200 screened names plus the crypto pairs |
| `trade-away stream` | Streams live minute bars and trades: the top 30 screened stocks over IEX (the free plan's limit) plus BTC/USD and ETH/USD |
| `trade-away news` | Saves the last 24h of Alpaca news to the events table |
| `trade-away health` | Lists gaps in the crypto stream over the last week, which is the Phase 0 exit gate |
| `trade-away backtest` | Backtests both strategies on stored daily bars with a 0.1% cost per side, and compares the result with buying and holding SPY |
| `trade-away run` | Daily trading run on the Alpaca paper account: exits first, then entries, all through the risk layer. Use `--dry-run` to see the decisions without sending orders |
| `trade-away stops` | Enforces crypto stop-losses from streamed prices. Alpaca can't attach stops to crypto orders; stock stops are placed at Alpaca along with the entry |
| `trade-away publish` | Pushes a snapshot of the paper account (equity vs SPY, open positions, every trade and why) to the public page on GitHub Pages. `--no-push --out snap.json` just writes the file |
| `streamlit run dashboard/app.py` | Dashboard: latest prices, stream health, charts, today's universe |

## Phase 1 strategies and limits

| Strategy | Buys when | Sells when | Stop |
| --- | --- | --- | --- |
| Trend following (`trend`) | 20-day average crosses above the 50-day | 20-day falls back below the 50-day | 3 x ATR(14) below entry |
| Dip buying (`dip`) | RSI(2) under 10 while price is above the 200-day average | Close above the 5-day average | 2 x ATR(14) below entry |

Risk limits live in `src/trade_away/risk.py`, and no strategy or agent can override them:

- Each trade risks 1% of equity.
- Each symbol is capped at 10% of equity.
- Total exposure is capped at 80% in stocks and 30% in crypto.
- At most 10 positions are open at once.
- New entries stop after a 3% loss in a day, and also after a 15% drawdown from the peak.
- Every entry needs a stop-loss.
- No shorting and no leverage.
- Only screened symbols can be traded.
- Data more than 4 days old is rejected.

## Setup

1. Create a free [Alpaca](https://alpaca.markets) account. Open the **Paper Trading** dashboard and generate API keys.
2. Install the project and add your keys:
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e ".[dashboard,dev]"
   cp .env.example .env   # paste ALPACA_API_KEY and ALPACA_SECRET_KEY
   ```
3. Run it once by hand:
   ```bash
   trade-away check
   trade-away screen
   trade-away backfill
   trade-away backtest    # how the rules would have done
   trade-away run --dry-run
   trade-away stream      # Ctrl+C to stop
   ```

## Deploy on a free cloud server

The target is an Oracle Cloud **Always Free** VM running Ubuntu. Google Cloud's free e2-micro is the fallback.

```bash
git clone https://github.com/sashu1999/trade-away.git && cd trade-away
python3 -m venv .venv && .venv/bin/pip install -e ".[dashboard]"
cp .env.example .env && nano .env
sudo cp deploy/trade-away-*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now trade-away-stream trade-away-dashboard
crontab deploy/crontab
```

The dashboard listens only on localhost. To view it from your computer, use `ssh -L 8501:localhost:8501 ubuntu@<vm-ip>` and open http://localhost:8501.

## Public trade tracker

Anyone can follow the paper account at **https://sashu1999.github.io/trade-away/**. The page is `site/index.html`; the server publishes it with a fresh `data.json` every 30 minutes (see `deploy/crontab`) as a single force-pushed commit on the `gh-pages` branch. The snapshot holds only prices, quantities, P/L and the strategy's reason for each trade: no keys, account number or order ids.

One-time setup:

1. On GitHub, create a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to only `sashu1999/trade-away` and the permission **Contents: Read and write**.
2. On the server, add it to `.env` as `PAGES_TOKEN=...`, then run `.venv/bin/trade-away publish` once and reinstall the crontab with `crontab deploy/crontab`.
3. In the repo's **Settings > Pages**, set the source to **Deploy from a branch**, branch `gh-pages`, folder `/ (root)`.

## Phase 0 exit gate

The stream has to run for a full week without gaps. Check with `trade-away health`, which exits non-zero if crypto minute bars have holes longer than 5 minutes.

## Tests

```bash
pytest -q
```
