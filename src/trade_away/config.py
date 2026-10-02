"""Settings loaded from environment variables (and a local .env file)."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    secret_key: str = ""
    db_path: str = "data/trade_away.db"
    crypto_symbols: list[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    min_price: float = 5.0
    min_dollar_volume: float = 20_000_000
    max_stream_symbols: int = 30

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.secret_key)


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ
    return Settings(
        api_key=env.get("ALPACA_API_KEY", ""),
        secret_key=env.get("ALPACA_SECRET_KEY", ""),
        db_path=env.get("DB_PATH", "data/trade_away.db"),
        crypto_symbols=_list(env.get("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD")),
        min_price=float(env.get("MIN_PRICE", 5)),
        min_dollar_volume=float(env.get("MIN_DOLLAR_VOLUME", 20_000_000)),
        max_stream_symbols=int(env.get("MAX_STREAM_SYMBOLS", 30)),
    )
