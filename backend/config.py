from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Polymarket credentials
    polymarket_private_key: Optional[str] = None
    polymarket_api_key: Optional[str] = None
    polymarket_api_secret: Optional[str] = None
    polymarket_api_passphrase: Optional[str] = None

    # Trading config
    max_bet_usdc: float = 10.0
    min_edge: float = 0.04
    min_confidence: float = 0.60
    max_daily_spend: float = 100.0
    dry_run: bool = True

    # Proxy for CLOB API (needed if VPS is in US or other blocked region)
    proxy_url: Optional[str] = None  # e.g. socks5://user:pass@host:port
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"

    # App
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
