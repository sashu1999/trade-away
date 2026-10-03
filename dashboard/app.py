"""Phase 0 dashboard: live prices, stream health and today's universe.

Run with: streamlit run dashboard/app.py
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from trade_away.config import load_settings
from trade_away.db import Store
from trade_away.health import crypto_gaps

st.set_page_config(page_title="trade-away", layout="wide")
settings = load_settings()


@st.cache_resource
def get_store() -> Store:
    return Store(settings.db_path)


store = get_store()
now = datetime.now(timezone.utc)
st.title("trade-away: live market data")

prices = pd.DataFrame([dict(r) for r in store.query(
    "SELECT symbol, asset_class, price, ts FROM latest_prices ORDER BY asset_class, symbol")])
left, right = st.columns([3, 2])
with left:
    st.subheader("Latest prices")
    if prices.empty:
        st.info("No prices yet. Start the stream with `trade-away stream`.")
    else:
        prices["age_s"] = (now - pd.to_datetime(prices["ts"], utc=True)).dt.total_seconds().round()
        st.dataframe(prices, hide_index=True, use_container_width=True)

with right:
    st.subheader("Stream health, last 24h")
    gaps = crypto_gaps(store, settings.crypto_symbols, hours=24, now=now)
    if gaps:
        st.error(f"{len(gaps)} crypto gaps")
        st.dataframe(pd.DataFrame([{"symbol": g.symbol, "from": g.start, "to": g.end,
                                    "minutes": round(g.minutes)} for g in gaps]), hide_index=True)
    else:
        st.success("No crypto gaps")

st.subheader("Price chart")
symbols = [r["symbol"] for r in store.query("SELECT DISTINCT symbol FROM bars ORDER BY symbol")]
if symbols:
    c1, c2 = st.columns(2)
    symbol = c1.selectbox("Symbol", symbols)
    timeframe = c2.radio("Timeframe", ["1Min", "1Day"], horizontal=True)
    bars = pd.DataFrame([dict(r) for r in store.query(
        "SELECT ts, close FROM bars WHERE symbol = ? AND timeframe = ? ORDER BY ts DESC LIMIT 500",
        (symbol, timeframe))])
    if bars.empty:
        st.info(f"No {timeframe} bars for {symbol} yet.")
    else:
        bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
        st.line_chart(bars.set_index("ts")["close"])

st.subheader("Tradable universe (latest screen)")
universe = pd.DataFrame([dict(r) for r in store.query(
    "SELECT rank, symbol, close, avg_dollar_volume FROM universe "
    "WHERE as_of = (SELECT MAX(as_of) FROM universe) ORDER BY rank")])
if universe.empty:
    st.info("No screen yet. Run `trade-away screen`.")
else:
    st.caption(f"{len(universe)} liquid symbols the agent may trade")
    st.dataframe(universe, hide_index=True, use_container_width=True)

st.header("Strategy bots (one $25k book each)")
equity = pd.DataFrame([dict(r) for r in store.query(
    "SELECT ts, account, equity FROM equity_log ORDER BY ts")])
if equity.empty:
    st.info("No trading runs yet. Run `trade-away run`.")
else:
    equity["ts"] = pd.to_datetime(equity["ts"], utc=True)
    st.line_chart(equity.pivot_table(index="ts", columns="account", values="equity").ffill())

st.subheader("Open trades")
st.dataframe(pd.DataFrame([dict(r) for r in store.query(
    "SELECT account AS bot, symbol, opened_ts, entry, qty, stop FROM open_trades ORDER BY opened_ts DESC")]),
    hide_index=True, use_container_width=True)

st.subheader("Decision journal (latest 100)")
st.dataframe(pd.DataFrame([dict(r) for r in store.query(
    "SELECT ts, account AS bot, symbol, side, price, stop, qty, approved, risk_note, reason, dry_run "
    "FROM decisions ORDER BY id DESC LIMIT 100")]),
    hide_index=True, use_container_width=True)
