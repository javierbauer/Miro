"""
Polymarket API client — fetches markets & prices from CLOB + Gamma APIs.
No auth required for read-only market data.
"""
import asyncio
from datetime import datetime
from typing import Any, Optional
import httpx
from loguru import logger


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"


class PolymarketClient:
    """Async HTTP client for Polymarket read-only data."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    #  Gamma API — rich market metadata                                    #
    # ------------------------------------------------------------------ #
    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
    ) -> list[dict]:
        client = await self._get()
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": "false",
        }
        try:
            resp = await client.get(f"{GAMMA_BASE}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("markets", [])
        except Exception as exc:
            logger.error(f"get_markets error: {exc}")
            return []

    async def get_market(self, condition_id: str) -> Optional[dict]:
        client = await self._get()
        try:
            resp = await client.get(f"{GAMMA_BASE}/markets/{condition_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"get_market({condition_id}) error: {exc}")
            return None

    # ------------------------------------------------------------------ #
    #  CLOB API — real-time order book prices                              #
    # ------------------------------------------------------------------ #
    async def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Return best bid/ask price for a token."""
        client = await self._get()
        try:
            resp = await client.get(
                f"{CLOB_BASE}/price",
                params={"token_id": token_id, "side": side},
            )
            resp.raise_for_status()
            return float(resp.json().get("price", 0))
        except Exception as exc:
            logger.debug(f"get_price({token_id}) error: {exc}")
            return None

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        client = await self._get()
        try:
            resp = await client.get(
                f"{CLOB_BASE}/book", params={"token_id": token_id}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug(f"get_orderbook error: {exc}")
            return None

    async def get_trades(
        self,
        market: str,
        limit: int = 50,
    ) -> list[dict]:
        client = await self._get()
        try:
            resp = await client.get(
                f"{CLOB_BASE}/trades",
                params={"market": market, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.debug(f"get_trades error: {exc}")
            return []

    # ------------------------------------------------------------------ #
    #  Convenience: build enriched market snapshot                         #
    # ------------------------------------------------------------------ #
    async def enrich_market(self, market: dict) -> dict:
        """Add live CLOB prices to a Gamma market dict."""
        tokens = market.get("tokens", []) or market.get("clobTokenIds", [])
        yes_price = market.get("outcomePrices", [None])[0]
        no_price  = market.get("outcomePrices", [None, None])[1]

        # Try to get fresher CLOB price for YES token
        if tokens:
            yes_token = tokens[0] if isinstance(tokens[0], str) else tokens[0].get("token_id")
            if yes_token:
                live = await self.get_price(yes_token, "buy")
                if live:
                    yes_price = live

        try:
            yes_price = float(yes_price) if yes_price else 0.5
            no_price  = float(no_price)  if no_price  else round(1 - yes_price, 4)
        except Exception:
            yes_price, no_price = 0.5, 0.5

        market["_yes_price"] = yes_price
        market["_no_price"]  = no_price
        return market

    async def get_top_markets(self, n: int = 50) -> list[dict]:
        """Return top N active markets by 24h volume, enriched with prices."""
        markets = await self.get_markets(limit=n, active=True)
        tasks = [self.enrich_market(m) for m in markets[:n]]
        return await asyncio.gather(*tasks)
