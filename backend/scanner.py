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
import json as _json
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.config import settings
from backend.database import AsyncSessionLocal, Market, Prediction, DailyStats, Trade
from backend.polymarket_client import PolymarketClient
from backend.predictor import Predictor
from backend.trader import Trader

CLOB = "https://clob.polymarket.com"


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
                # 0. Take-profit check on open positions
                tp_closed = await self.check_take_profits(session)
                if tp_closed:
                    stats["take_profit_closed"] = tp_closed

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
        pred = await self.predictor.predict(market)
        if pred is None:
            return

        stats["predictions_made"] += 1

        # Overwrite the existing prediction for this market (no accumulation)
        from sqlalchemy import delete
        await session.execute(
            delete(Prediction).where(Prediction.condition_id == pred.condition_id)
        )
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

    async def check_take_profits(self, session: AsyncSession) -> int:
        """Close open positions whose value has reached the take-profit multiplier.

        For paper trades: resolves the trade in the DB at the current market price.
        For live trades: logs a warning (manual sell needed — auto-sell is future work).
        Returns number of positions closed.
        """
        mult = settings.take_profit_multiplier
        if not mult or mult <= 1.0:
            return 0

        result = await session.execute(
            select(Trade).where(
                Trade.pnl.is_(None),
                Trade.status.in_(["DRY_RUN", "FILLED", "PENDING"]),
            )
        )
        open_trades = result.scalars().all()
        if not open_trades:
            return 0

        proxy   = settings.proxy_url or None
        closed  = 0

        async with httpx.AsyncClient(timeout=10, proxy=proxy) as http:
            for trade in open_trades:
                try:
                    r = await http.get(f"{CLOB}/markets/{trade.condition_id}")
                    if r.status_code != 200:
                        continue
                    market = r.json()
                    if market.get("closed"):
                        continue   # let pnl_tracker handle final resolution

                    # Parse current prices from outcomePrices or token prices
                    current_yes: Optional[float] = None
                    raw = market.get("outcomePrices")
                    if isinstance(raw, str):
                        try:
                            raw = _json.loads(raw)
                        except Exception:
                            raw = None
                    if isinstance(raw, list) and len(raw) >= 2:
                        try:
                            current_yes = float(raw[0])
                        except Exception:
                            pass

                    if current_yes is None:
                        tokens = market.get("tokens", [])
                        if len(tokens) >= 2 and isinstance(tokens[0], dict):
                            try:
                                current_yes = float(tokens[0].get("price") or 0) or None
                            except Exception:
                                pass

                    if current_yes is None or not (0.01 < current_yes < 0.99):
                        continue

                    current_our = current_yes if trade.side == "YES" else (1 - current_yes)

                    if not trade.price or trade.price <= 0:
                        continue

                    ratio = current_our / trade.price
                    if ratio < mult:
                        continue

                    # Take profit
                    realized_pnl = round(trade.amount_usdc * (ratio - 1), 4)

                    if trade.status == "DRY_RUN":
                        trade.pnl    = realized_pnl
                        trade.status = "RESOLVED"
                        session.add(trade)
                        closed += 1
                        logger.success(
                            f"[TAKE PROFIT] {trade.side} '{trade.question[:50]}' "
                            f"entry={trade.price:.3f}→now={current_our:.3f} "
                            f"({ratio:.1f}x) P&L=${realized_pnl:+.2f}"
                        )
                    else:
                        # Live: warn for now (auto-sell is future work)
                        logger.warning(
                            f"[TAKE PROFIT] LIVE {trade.side} '{trade.question[:50]}' "
                            f"({ratio:.1f}x) — manual sell needed on Polymarket"
                        )

                except Exception as exc:
                    logger.debug(f"Take-profit check error {trade.condition_id[:12]}: {exc}")

        if closed:
            await session.commit()
            logger.info(f"Take-profit: closed {closed} paper position(s)")
        return closed

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
