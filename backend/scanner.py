"""
Market scanner — the overnight "Ralph Loop".

Runs on a schedule (default: every 15 minutes while markets are active).
1. Fetch top markets from Polymarket
2. Generate predictions for each
3. Store predictions in DB
4. Execute trades for high-confidence signals
5. Update daily stats
"""
import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import AsyncSessionLocal, Market, Prediction, DailyStats
from backend.polymarket_client import PolymarketClient
from backend.predictor import Predictor
from backend.trader import Trader


class MarketScanner:
    def __init__(self):
        self.client    = PolymarketClient()
        self.predictor = Predictor()
        self.trader    = Trader()
        self._running  = False
        self._scanning = False
        self._scan_count = 0

    # ------------------------------------------------------------------ #
    #  Main scan loop                                                      #
    # ------------------------------------------------------------------ #
    async def run_scan(self) -> dict:
        """Execute one full scan cycle. Returns summary stats."""
        if self._scanning:
            logger.warning("Scan already in progress — skipping")
            return {}
        self._scanning = True
        self._scan_count += 1
        started = datetime.utcnow()
        logger.info(f"=== Scan #{self._scan_count} started [{self.trader.mode_label}] ===")

        stats = {
            "scan_number": self._scan_count,
            "started_at": started.isoformat(),
            "markets_scanned": 0,
            "predictions_made": 0,
            "trades_placed": 0,
            "total_spent": 0.0,
            "top_signals": [],
        }

        async with AsyncSessionLocal() as session:
            try:
                # 1. Fetch markets
                markets = await self.client.get_top_markets(n=60)
                stats["markets_scanned"] = len(markets)
                logger.info(f"Fetched {len(markets)} markets")

                # 2. Predict + trade
                for market in markets:
                    await self._process_market(market, session, stats)

                # 3. Persist daily stats
                await self._update_daily_stats(session, stats)

            except Exception as exc:
                logger.error(f"Scan error: {exc}", exc_info=True)
                self._scanning = False

        self._scanning = False
        duration = (datetime.utcnow() - started).total_seconds()
        stats["duration_seconds"] = round(duration, 1)
        logger.info(
            f"=== Scan #{self._scan_count} done in {duration:.1f}s | "
            f"{stats['predictions_made']} preds | "
            f"{stats['trades_placed']} trades | "
            f"${stats['total_spent']:.2f} spent ==="
        )
        return stats

    async def _process_market(self, market: dict, session: AsyncSession, stats: dict):
        condition_id = market.get("conditionId") or market.get("condition_id", "")
        if not condition_id:
            return

        # Upsert market record
        db_market = Market(
            condition_id=condition_id,
            question=market.get("question", "")[:500],
            category=market.get("category", ""),
            yes_price=market.get("_yes_price", 0.5),
            no_price=market.get("_no_price", 0.5),
            volume_24h=float(market.get("volume24hr") or market.get("volume", 0) or 0),
            liquidity=float(market.get("liquidity", 0) or 0),
            last_updated=datetime.utcnow(),
        )
        await session.merge(db_market)

        # Generate prediction
        pred = self.predictor.predict(market)
        if pred is None:
            return

        stats["predictions_made"] += 1

        # Persist prediction
        db_pred = Prediction(
            condition_id=pred.condition_id,
            question=pred.question[:200],
            predicted_yes_prob=pred.predicted_yes_prob,
            market_yes_price=pred.market_yes_price,
            edge=pred.edge,
            confidence=pred.confidence,
            signal=pred.signal,
            reasoning=pred.reasoning[:500],
        )
        session.add(db_pred)

        # Track top signals for response
        if pred.signal != "SKIP":
            stats["top_signals"].append({
                "question": pred.question[:80],
                "signal": pred.signal,
                "edge": round(pred.edge, 3),
                "confidence": pred.confidence,
                "market_price": pred.market_yes_price,
            })

        # Execute trade if qualified
        trade = await self.trader.maybe_trade(pred, session)
        if trade:
            stats["trades_placed"] += 1
            stats["total_spent"] += trade.amount_usdc

        await session.commit()

    async def _update_daily_stats(self, session: AsyncSession, stats: dict):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        daily = DailyStats(
            date=today,
            markets_scanned=stats["markets_scanned"],
            predictions_made=stats["predictions_made"],
            trades_placed=stats["trades_placed"],
            total_spent=stats["total_spent"],
        )
        session.add(daily)
        await session.commit()

    async def start_loop(self, interval_seconds: int = 900):
        """Run scans continuously every `interval_seconds` (default 15 min)."""
        self._running = True
        logger.info(f"Scanner loop started — interval={interval_seconds}s")
        while self._running:
            try:
                await self.run_scan()
            except Exception as exc:
                logger.error(f"Loop error: {exc}")
            await asyncio.sleep(interval_seconds)

    def stop(self):
        self._running = False
        logger.info("Scanner loop stopping...")
