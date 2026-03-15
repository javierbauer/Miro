"""
Trade executor.

DRY_RUN=true  → paper trading (default, safe to run immediately)
DRY_RUN=false → real trades via Polymarket CLOB API (requires credentials)

Paper trading simulates fills at the current market price and tracks
a virtual P&L so you can validate the strategy before going live.
"""
import uuid
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from backend.config import settings
from backend.database import Trade, DailyStats
from backend.predictor import PredictionResult


class Trader:
    def __init__(self, dry_run: Optional[bool] = None):
        self.dry_run = dry_run if dry_run is not None else settings.dry_run
        self._daily_spent = 0.0
        self._paper_balance = 1000.0   # virtual USDC for paper trading

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #
    async def maybe_trade(
        self,
        prediction: PredictionResult,
        session: AsyncSession,
    ) -> Optional[Trade]:
        """Place a trade if the signal qualifies and budget allows."""
        if prediction.signal == "SKIP":
            return None

        # Daily budget guard
        spent_today = await self._spent_today(session)
        remaining   = settings.max_daily_spend - spent_today
        if remaining <= 0:
            logger.warning("Daily budget exhausted — skipping all trades")
            return None

        # Size the bet via Kelly
        bankroll = self._paper_balance if self.dry_run else settings.max_daily_spend
        raw_size = bankroll * prediction.kelly_fraction
        size = min(raw_size, settings.max_bet_usdc, remaining)
        size = round(max(size, 1.0), 2)   # minimum $1 bet

        side  = prediction.bet_side
        price = prediction.market_yes_price if side == "YES" else (1 - prediction.market_yes_price)

        if self.dry_run:
            trade = await self._paper_trade(prediction, side, price, size, session)
        else:
            trade = await self._live_trade(prediction, side, price, size, session)

        return trade

    # ------------------------------------------------------------------ #
    #  Paper trading                                                       #
    # ------------------------------------------------------------------ #
    async def _paper_trade(
        self,
        pred: PredictionResult,
        side: str,
        price: float,
        size: float,
        session: AsyncSession,
    ) -> Trade:
        self._paper_balance -= size
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        trade = Trade(
            condition_id=pred.condition_id,
            question=pred.question[:200],
            side=side,
            amount_usdc=size,
            price=price,
            status="DRY_RUN",
            order_id=order_id,
        )
        session.add(trade)
        await session.commit()

        logger.info(
            f"[PAPER] {side} ${size:.2f} on '{pred.question[:60]}' "
            f"@ {price:.3f} | edge={pred.edge:+.3f} conf={pred.confidence:.2f} | {order_id}"
        )
        return trade

    # ------------------------------------------------------------------ #
    #  Live trading via py-clob-client                                     #
    # ------------------------------------------------------------------ #
    async def _live_trade(
        self,
        pred: PredictionResult,
        side: str,
        price: float,
        size: float,
        session: AsyncSession,
    ) -> Optional[Trade]:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType

            creds = ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase,
            )
            client = ClobClient(
                host=settings.clob_host,
                key=settings.polymarket_private_key,
                chain_id=137,
                creds=creds,
            )

            # Market order at current price
            order_args = MarketOrderArgs(
                token_id=pred.condition_id,
                amount=size,
            )
            signed = client.create_market_order(order_args)
            resp   = client.post_order(signed, OrderType.FOK)
            order_id = resp.get("orderID", "UNKNOWN")

            trade = Trade(
                condition_id=pred.condition_id,
                question=pred.question[:200],
                side=side,
                amount_usdc=size,
                price=price,
                status="FILLED" if resp.get("status") == "matched" else "PENDING",
                order_id=order_id,
            )
            session.add(trade)
            await session.commit()
            logger.success(f"[LIVE] {side} ${size:.2f} on '{pred.question[:60]}' → {order_id}")
            return trade

        except ImportError:
            logger.error("py-clob-client not installed — falling back to paper trade")
            return await self._paper_trade(pred, side, price, size, session)
        except Exception as exc:
            logger.error(f"Live trade failed: {exc}")
            return None

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #
    async def _spent_today(self, session: AsyncSession) -> float:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        result = await session.execute(
            select(func.coalesce(func.sum(Trade.amount_usdc), 0)).where(
                Trade.created_at >= today
            )
        )
        return float(result.scalar())

    @property
    def mode_label(self) -> str:
        return "PAPER TRADING (DRY RUN)" if self.dry_run else "LIVE TRADING"
