# trade-away

An AI trading agent that paper trades US stocks, ETFs and crypto on live data, so results can be judged before any real money is involved. The full design is in the [system design doc](https://claude.ai/code/artifact/066b0060-85dc-41a4-8088-6ddcddffdf10).

This is **Phase 0: foundation**. It streams live prices into SQLite, screens the whole US market each night, backfills history and pulls news, and a dashboard shows it all. It doesn't trade yet.

## What's here

| Command | What it does |
| --- | --- |
| `trade-away check` | Checks your keys and paper account, and whether your plan includes news |
| `trade-away screen` | Pulls daily bars for every tradable US stock and ETF, then keeps the liquid ones (price above $5, over $20M traded a day) ranked by dollar volume |
| `trade-away backfill` | Fetches 2 years of daily history for the top 200 screened names plus the crypto pairs |
| `trade-away stream` | Streams live minute bars and trades: the top 30 screened stocks over IEX (the free plan's limit) plus BTC/USD and ETH/USD |
| `trade-away news` | Saves the last 24h of Alpaca news to the events table |
| `trade-away health` | Lists gaps in the crypto stream over the last week, which is the Phase 0 exit gate |
| `streamlit run dashboard/app.py` | Dashboard: latest prices, stream health, charts, today's universe |

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

## Phase 0 exit gate

The stream has to run for a full week without gaps. Check with `trade-away health`, which exits non-zero if crypto minute bars have holes longer than 5 minutes.

## Tests

```bash
pytest -q
```
