"""
P&L tracker — checks resolved trades against Polymarket outcomes.

Resolution strategy:
  1. Bulk-fetch closed markets from Gamma API (efficient for recent closings)
  2. Individual CLOB API lookup for any not found in bulk (always correct)

CLOB API is authoritative: condition_id path param works, response includes
tokens[].winner=true/false which directly tells us who won.

Payout logic (no fees):
  - BUY YES @ price p, stake s → win: s/p - s, lose: -s
  - BUY NO  @ price p, stake s → win: s/p - s, lose: -s
"""
import asyncio
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal, Trade
from backend.polymarket_client import PolymarketClient

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


class PnLTracker:
    def __init__(self):
        self.client = PolymarketClient()

    async def update_all(self) -> dict:
        """Check all unresolved trades and update P&L. Returns summary."""
        summary = {"checked": 0, "resolved": 0, "wins": 0, "losses": 0,
                   "total_pnl": 0.0, "total_staked": 0.0}

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.pnl == None,  # noqa
                    Trade.status.in_(["DRY_RUN", "FILLED", "PENDING"]),
                )
            )
            trades = result.scalars().all()
            summary["checked"] = len(trades)
            logger.info(f"P&L check: {len(trades)} unresolved trades")

            # 1. Bulk-fetch resolved markets from Gamma API (covers recent closings quickly)
            gamma_markets = await self._fetch_resolved_markets_gamma()
            logger.info(f"P&L check: Gamma bulk fetch returned {len(gamma_markets)} resolved markets")

            # 2. Individual CLOB lookup for unique condition_ids not found in Gamma bulk
            missing_ids = {t.condition_id for t in trades
                           if t.condition_id not in gamma_markets}
            logger.info(f"P&L check: {len(missing_ids)} condition_ids need CLOB lookup")

            clob_markets = {}
            for cid in missing_ids:
                market = await self._get_market_clob(cid)
                if market:
                    clob_markets[cid] = market
                await asyncio.sleep(0.05)

            logger.info(f"P&L check: CLOB returned {len(clob_markets)} markets "
                        f"({sum(1 for m in clob_markets.values() if m.get('closed'))} closed)")

            # 3. Apply results
            for trade in trades:
                pnl = self._calc_pnl_gamma(trade, gamma_markets)
                if pnl is None:
                    pnl = self._calc_pnl_clob(trade, clob_markets.get(trade.condition_id))
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

            wins         = [t for t in trades if t.pnl and t.pnl > 0]
            losses       = [t for t in trades if t.pnl and t.pnl <= 0]
            total_pnl    = sum(t.pnl for t in trades if t.pnl)
            total_staked = sum(t.amount_usdc for t in trades)

            return {
                "resolved":     len(trades),
                "wins":         len(wins),
                "losses":       len(losses),
                "win_rate":     round(len(wins) / len(trades) * 100, 1),
                "total_pnl":    round(total_pnl, 2),
                "total_staked": round(total_staked, 2),
                "roi_pct":      round(total_pnl / total_staked * 100, 2) if total_staked else 0,
                "best_trade":   max((t.pnl for t in trades if t.pnl), default=0),
                "worst_trade":  min((t.pnl for t in trades if t.pnl), default=0),
            }

    # ------------------------------------------------------------------ #
    #  CLOB API (authoritative, individual lookup)                        #
    # ------------------------------------------------------------------ #
    async def _get_market_clob(self, condition_id: str):
        """Fetch a single market from CLOB API by condition_id."""
        client = await self.client._get()
        try:
            r = await client.get(f"{CLOB_BASE}/markets/{condition_id}")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("condition_id"):
                    return data
        except Exception as e:
            logger.debug(f"CLOB lookup failed for {condition_id}: {e}")
        return None

    def _parse_winner_clob(self, market: dict):
        """
        Returns True if YES (token[0]) won, False if NO (token[1]) won, None if unresolved.
        Uses token index rather than outcome name so it works for both binary
        (Yes/No) and named-outcome (team names) markets.
        token[0] always corresponds to the YES position (outcomePrices[0] in Gamma).
        """
        tokens = market.get("tokens", [])
        for i, token in enumerate(tokens):
            if isinstance(token, dict) and token.get("winner") is True:
                return i == 0  # token[0] = YES, token[1] = NO
        return None

    def _calc_pnl_clob(self, trade: Trade, market):
        """Calculate P&L from a CLOB API market dict."""
        if not market or not market.get("closed"):
            return None
        resolved_yes = self._parse_winner_clob(market)
        if resolved_yes is None:
            return None
        return self._net_pnl(trade, resolved_yes)

    # ------------------------------------------------------------------ #
    #  Gamma API bulk fetch (efficient for high-volume recent closings)   #
    # ------------------------------------------------------------------ #
    async def _fetch_resolved_markets_gamma(self) -> dict:
        """Bulk-fetch closed markets from Gamma API, keyed by conditionId."""
        client = await self.client._get()
        result = {}
        offset = 0
        while True:
            try:
                r = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params={
                        "closed": "true",
                        "limit": 100,
                        "offset": offset,
                        "order": "closedTime",
                        "ascending": "false",
                    }
                )
                if r.status_code != 200:
                    break
                data = r.json()
                items = data if isinstance(data, list) else data.get("markets", [])
                if not items:
                    break

                stop = False
                for m in items:
                    closed_time = m.get("closedTime") or m.get("endDate") or ""
                    if closed_time and closed_time < "2026-04-01":
                        stop = True
                        break
                    cid = m.get("conditionId") or m.get("condition_id")
                    if cid and self._parse_winner_gamma(m) is not None:
                        result[cid] = m

                if stop or len(items) < 100:
                    break
                offset += 100
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Gamma bulk fetch error at offset {offset}: {e}")
                break
        return result

    def _parse_winner_gamma(self, market: dict):
        """
        Returns True if YES won, False if NO won, None if unresolved.
        Reads outcomePrices from Gamma API: ["1","0"]=YES, ["0","1"]=NO.
        """
        import json as _json
        raw = market.get("outcomePrices")
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                return None
        if not isinstance(raw, list) or len(raw) < 2:
            return None
        try:
            yes_price = float(raw[0])
            no_price  = float(raw[1])
        except Exception:
            return None
        if yes_price == 1.0 and no_price == 0.0:
            return True
        if yes_price == 0.0 and no_price == 1.0:
            return False
        return None

    def _calc_pnl_gamma(self, trade: Trade, markets: dict):
        """Calculate P&L from Gamma bulk-fetch dict."""
        market = markets.get(trade.condition_id)
        if not market:
            return None
        resolved_yes = self._parse_winner_gamma(market)
        if resolved_yes is None:
            return None
        return self._net_pnl(trade, resolved_yes)

    # ------------------------------------------------------------------ #
    #  Shared P&L math                                                    #
    # ------------------------------------------------------------------ #
    def _net_pnl(self, trade: Trade, resolved_yes: bool) -> float:
        won = (trade.side == "YES" and resolved_yes) or \
              (trade.side == "NO" and not resolved_yes)
        if won:
            payout = trade.amount_usdc / trade.price if trade.price else 0
            return round(payout - trade.amount_usdc, 4)
        return round(-trade.amount_usdc, 4)
