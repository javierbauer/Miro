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
            # Get trades without P&L yet
            result = await session.execute(
                select(Trade).where(Trade.pnl == None, Trade.status == "DRY_RUN")  # noqa
            )
            trades = result.scalars().all()
            summary["checked"] = len(trades)

            for trade in trades:
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

    async def _resolve_trade(self, trade: Trade):
        """Return net P&L for a trade if market has resolved, else None."""
        market = await self.client.get_market(trade.condition_id)
        if not market:
            return None

        # Check if resolved
        resolved = market.get("resolved") or market.get("closed")
        if not resolved:
            return None

        # Get resolution outcome
        resolution = market.get("resolution") or market.get("resolvedOutcome")
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
