"""
P&L tracker — checks resolved trades against Polymarket outcomes.

For each DRY_RUN trade in the DB, queries the Gamma API to see if the
market resolved YES or NO, then calculates profit/loss and updates the
trade record.

Payout logic (simplified, no fees):
  - BUY YES @ price p, stake s → win: s/p, lose: 0
  - BUY NO  @ price p, stake s → win: s/p, lose: 0
  (price is already the cost per share; payout is always $1 per share)
"""
import asyncio
from datetime import datetime
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal, Trade
from backend.polymarket_client import PolymarketClient


class PnLTracker:
    def __init__(self):
        self.client = PolymarketClient()

    async def update_all(self) -> dict:
        """Check all unresolved trades and update P&L. Returns summary."""
        summary = {"checked": 0, "resolved": 0, "wins": 0, "losses": 0,
                   "total_pnl": 0.0, "total_staked": 0.0}

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Trade).where(Trade.pnl == None, Trade.status == "DRY_RUN")  # noqa
            )
            trades = result.scalars().all()
            summary["checked"] = len(trades)
            logger.info(f"P&L check: {len(trades)} unresolved trades")

            # First bulk-fetch resolved markets from Gamma API
            resolved_markets = await self._fetch_resolved_markets()
            logger.info(f"P&L check: found {len(resolved_markets)} resolved markets from Gamma")

            for trade in trades:
                pnl = self._calc_pnl(trade, resolved_markets)
                if pnl is None:
                    # Fall back to individual lookup
                    pnl = await self._resolve_trade(trade)
                if pnl is not None:
                    trade.pnl = pnl
                    trade.status = "RESOLVED"
                    session.add(trade)
                    summary["resolved"] += 1
                    summary["total_staked"] += trade.amount_usdc
                    summary["total_pnl"] += pnl
                    if pnl > 0:
                        summary["wins"] += 1
                    else:
                        summary["losses"] += 1

            await session.commit()

        summary["total_pnl"] = round(summary["total_pnl"], 4)
        summary["total_staked"] = round(summary["total_staked"], 2)
        summary["win_rate"] = (
            round(summary["wins"] / summary["resolved"] * 100, 1)
            if summary["resolved"] > 0 else 0
        )
        summary["roi_pct"] = (
            round(summary["total_pnl"] / summary["total_staked"] * 100, 2)
            if summary["total_staked"] > 0 else 0
        )
        return summary

    async def get_summary(self) -> dict:
        """Return P&L summary from DB without hitting the API."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Trade).where(Trade.status == "RESOLVED")
            )
            trades = result.scalars().all()

            if not trades:
                return {"resolved": 0, "wins": 0, "losses": 0,
                        "total_pnl": 0.0, "roi_pct": 0.0, "win_rate": 0.0,
                        "total_staked": 0.0}

            wins   = [t for t in trades if t.pnl and t.pnl > 0]
            losses = [t for t in trades if t.pnl and t.pnl <= 0]
            total_pnl    = sum(t.pnl for t in trades if t.pnl)
            total_staked = sum(t.amount_usdc for t in trades)

            return {
                "resolved":      len(trades),
                "wins":          len(wins),
                "losses":        len(losses),
                "win_rate":      round(len(wins) / len(trades) * 100, 1),
                "total_pnl":     round(total_pnl, 2),
                "total_staked":  round(total_staked, 2),
                "roi_pct":       round(total_pnl / total_staked * 100, 2) if total_staked else 0,
                "best_trade":    max((t.pnl for t in trades if t.pnl), default=0),
                "worst_trade":   min((t.pnl for t in trades if t.pnl), default=0),
            }

    async def _fetch_resolved_markets(self) -> dict:
        """Bulk fetch resolved markets from Gamma API. Returns dict keyed by conditionId."""
        client = await self.client._get()
        result = {}
        offset = 0
        while True:
            try:
                r = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"closed": "true", "resolved": "true", "limit": 100, "offset": offset}
                )
                if r.status_code != 200:
                    break
                data = r.json()
                items = data if isinstance(data, list) else data.get("markets", [])
                if not items:
                    break
                for m in items:
                    cid = m.get("conditionId") or m.get("condition_id")
                    if cid:
                        result[cid] = m
                if len(items) < 100:
                    break
                offset += 100
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Bulk fetch error: {e}")
                break
        return result

    def _calc_pnl(self, trade: Trade, markets: dict):
        """Calculate P&L from pre-fetched market dict. Returns None if not found/resolved."""
        market = markets.get(trade.condition_id)
        if not market:
            return None
        resolved = market.get("resolved") or market.get("closed") or market.get("isResolved")
        if not resolved:
            return None
        resolution = market.get("resolution") or market.get("resolvedOutcome")
        if resolution is None:
            return None
        res_str = str(resolution).strip().lower()
        resolved_yes = res_str in ("yes", "1", "true")
        won = (trade.side == "YES" and resolved_yes) or (trade.side == "NO" and not resolved_yes)
        if won:
            payout = trade.amount_usdc / trade.price if trade.price else 0
            return round(payout - trade.amount_usdc, 4)
        return round(-trade.amount_usdc, 4)

    async def _get_market_by_condition(self, condition_id: str):
        """Try multiple Gamma API lookup strategies for a condition ID."""
        client = await self.client._get()

        # Strategy 1: path param (works for numeric market IDs)
        try:
            r = await client.get(f"https://gamma-api.polymarket.com/markets/{condition_id}")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict) and data:
                    return data
        except Exception:
            pass

        # Strategy 2: query param conditionIds
        try:
            r = await client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"conditionIds": condition_id, "limit": 1}
            )
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get("markets", [])
                if items:
                    return items[0]
        except Exception:
            pass

        return None

    async def _resolve_trade(self, trade: Trade):
        """Return net P&L for a trade if market has resolved, else None."""
        market = await self._get_market_by_condition(trade.condition_id)
        if not market:
            return None

        # Check if resolved — Gamma API uses several field names
        resolved = (
            market.get("resolved")
            or market.get("closed")
            or market.get("isResolved")
        )
        if not resolved:
            return None

        # Get resolution outcome
        resolution = (
            market.get("resolution")
            or market.get("resolvedOutcome")
            or market.get("resolutionSource")
        )
        if resolution is None:
            return None

        # Normalise: "Yes"/"No"/1/0/"1"/"0"
        res_str = str(resolution).strip().lower()
        resolved_yes = res_str in ("yes", "1", "true")

        won = (trade.side == "YES" and resolved_yes) or \
              (trade.side == "NO"  and not resolved_yes)

        if won:
            # Payout = stake / price (price is cost per $1 share)
            payout = trade.amount_usdc / trade.price if trade.price else 0
            return round(payout - trade.amount_usdc, 4)   # net profit
        else:
            return round(-trade.amount_usdc, 4)            # net loss
