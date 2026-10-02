"""News from Alpaca's news API, stored as events.

Phase 0 also uses this to answer an open question: does the free plan include news?
"""

import logging
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import Store

log = logging.getLogger(__name__)


def fetch_news(settings: Settings, store: Store, symbols: list[str] | None = None,
               hours: int = 24, limit: int = 50) -> int:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(settings.api_key, settings.secret_key)
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    req = NewsRequest(symbols=",".join(symbols) if symbols else None, start=start, limit=limit)
    articles = client.get_news(req).data.get("news", [])
    events = []
    for a in articles:
        for symbol in (a.symbols or [None]):
            events.append((f"alpaca-news-{a.id}-{symbol}", "news", symbol, a.created_at,
                           a.headline, a.url, f"alpaca:{a.source}"))
    added = store.insert_events(events)
    log.info("fetched %d articles, %d new events", len(articles), added)
    return added


def check_news_access(settings: Settings) -> tuple[bool, str]:
    """Try one small news request and report whether the account can read news."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    try:
        result = NewsClient(settings.api_key, settings.secret_key).get_news(NewsRequest(limit=1))
        n = len(result.data.get("news", []))
        return True, f"news API reachable on this plan ({n} article returned)"
    except Exception as exc:  # the SDK raises APIError subclasses with the HTTP reason
        return False, f"news API not available: {exc}"
