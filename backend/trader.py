"""
Trade executor.

DRY_RUN=true  → paper trading (default, safe to run immediately)
DRY_RUN=false → real trades via polymarket-client SDK (requires credentials)

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


PAPER_STARTING_BALANCE = 10_000.0   # raised to $10k so balance stays positive longer

# USDC tokens on Polygon — Polymarket cash is held as one of these in the
# proxy wallet.  We sum both so it works for native- and bridged-USDC accounts.
NATIVE_USDC  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
BRIDGED_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Public Polygon RPCs (not geo-blocked, so they don't need the trading proxy).
POLYGON_RPCS = ("https://1rpc.io/matic", "https://polygon-rpc.com",
                "https://polygon.llamarpc.com")
_BANKROLL_TTL = 60.0   # seconds — avoid re-querying RPC for every prediction


class Trader:
    def __init__(self, dry_run: Optional[bool] = None):
        self.dry_run = dry_run if dry_run is not None else settings.dry_run
        self._daily_spent = 0.0
        self._bankroll_cache: Optional[tuple] = None   # (value, monotonic_ts)

    async def paper_balance(self, session: AsyncSession) -> float:
        """Calculate balance from trade history so it persists across restarts."""
        result = await session.execute(
            select(func.coalesce(func.sum(Trade.amount_usdc), 0)).where(
                Trade.status == "DRY_RUN"
            )
        )
        spent = float(result.scalar())
        return round(PAPER_STARTING_BALANCE - spent, 2)

    # ------------------------------------------------------------------ #
    #  Live bankroll — real wallet cash, used as the Kelly base in live    #
    # ------------------------------------------------------------------ #
    async def _onchain_usdc(self, wallet: str) -> Optional[float]:
        """Sum native + bridged USDC held by `wallet`. None if all RPCs fail."""
        import httpx
        selector = "0x70a08231" + wallet[2:].lower().zfill(64)
        total: Optional[float] = None
        # trust_env=False so the public RPC call ignores HTTPS_PROXY (which is
        # for Polymarket only and would mangle the JSON-RPC request).
        async with httpx.AsyncClient(timeout=12, trust_env=False) as http:
            for token in (NATIVE_USDC, BRIDGED_USDC):
                for rpc in POLYGON_RPCS:
                    try:
                        r = await http.post(rpc, json={
                            "jsonrpc": "2.0", "method": "eth_call",
                            "params": [{"to": token, "data": selector}, "latest"],
                            "id": 1,
                        })
                        res = r.json().get("result")
                        if res is not None:
                            total = (total or 0.0) + int(res, 16) / 1e6
                            break   # this token done, next token
                    except Exception:
                        continue
        return total

    async def live_bankroll(self) -> float:
        """Kelly bankroll for LIVE mode.

        Precedence: explicit LIVE_BANKROLL override → real on-chain wallet
        cash → max_daily_spend as a safe fallback (so a failed balance fetch
        never inflates bet size).
        """
        import time
        if settings.live_bankroll:
            return float(settings.live_bankroll)

        # short-lived cache so a multi-trade scan hits RPC at most once
        if self._bankroll_cache:
            value, ts = self._bankroll_cache
            if time.monotonic() - ts < _BANKROLL_TTL:
                return value

        bankroll = float(settings.max_daily_spend)   # safe default
        wallet = settings.polymarket_proxy_wallet
        if wallet:
            cash = await self._onchain_usdc(wallet)
            if cash is not None and cash > 0:
                bankroll = cash
            elif cash is None:
                logger.warning(
                    "Live bankroll: could not read on-chain USDC — falling "
                    f"back to max_daily_spend (${bankroll:.2f})"
                )
        self._bankroll_cache = (bankroll, time.monotonic())
        return bankroll

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

        # Size the bet via Kelly.  Paper sizes off its virtual balance; live
        # sizes off the wallet's real cash (same risk logic, real capital).
        bankroll = (await self.paper_balance(session)) if self.dry_run else (await self.live_bankroll())
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
    #  Live trading via polymarket-client SDK                              #
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
            from polymarket import AsyncSecureClient
            from decimal import Decimal
            import httpx
            import os

            proxy = settings.proxy_url or None

            # Fetch YES/NO token IDs from CLOB — condition_id is NOT the token_id
            async with httpx.AsyncClient(timeout=10, proxy=proxy) as http:
                r = await http.get(f"{settings.clob_host}/markets/{pred.condition_id}")
            if r.status_code != 200:
                logger.error(f"CLOB market lookup failed: {r.status_code}")
                return None
            tokens = r.json().get("tokens", [])
            if len(tokens) < 2:
                logger.error(f"No tokens for {pred.condition_id}")
                return None
            # token[0] = YES, token[1] = NO (matches outcomePrices ordering)
            token_id = tokens[0 if side == "YES" else 1].get("token_id")
            if not token_id:
                logger.error(f"Token ID missing for side={side}")
                return None

            # Propagate proxy into the SDK's internal httpx clients (read env at creation time)
            if proxy:
                os.environ["HTTPS_PROXY"] = proxy
                os.environ["HTTP_PROXY"] = proxy

            from polymarket import ApiKeyCreds
            # If the account was set up via the Polymarket web UI it uses a
            # type-1 POLY_PROXY wallet.  Pass the proxy address explicitly so
            # the SDK signs orders as that maker; omit (None) to fall back to
            # the type-3 deposit-wallet flow for bot-only accounts.
            client = await AsyncSecureClient._create(
                private_key=settings.polymarket_private_key,
                wallet=settings.polymarket_proxy_wallet or None,
                credentials=ApiKeyCreds(
                    key=settings.polymarket_api_key,
                    passphrase=settings.polymarket_api_passphrase,
                    secret=settings.polymarket_api_secret,
                ),
                validate_credentials=False,
            )
            logger.info(f"[LIVE] SDK wallet: {client._ctx.wallet}")
            try:
                # side is always "BUY" — we buy YES tokens or NO tokens
                resp = await client.place_market_order(
                    token_id=token_id,
                    side="BUY",
                    amount=Decimal(str(round(size, 2))),
                )
            finally:
                await client.close()

            if not getattr(resp, "ok", False):
                logger.error(f"Order rejected by CLOB: {resp}")
                return None

            order_id = resp.order_id
            status = "FILLED" if getattr(resp, "status", "") == "matched" else "PENDING"

            trade = Trade(
                condition_id=pred.condition_id,
                question=pred.question[:200],
                side=side,
                amount_usdc=size,
                price=price,
                status=status,
                order_id=order_id,
            )
            session.add(trade)
            await session.commit()
            logger.success(
                f"[LIVE] {side} ${size:.2f} on '{pred.question[:60]}' "
                f"@ {price:.3f} | edge={pred.edge:+.3f} | {order_id}"
            )
            return trade

        except ImportError as _ie:
            logger.error(f"ImportError in _live_trade: {_ie} — install: pip install --pre polymarket-client")
            return None
        except Exception as exc:
            logger.error(f"Live trade failed: {exc}")
            return None

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #
    async def _spent_today(self, session: AsyncSession) -> float:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self.dry_run:
            status_filter = Trade.status == "DRY_RUN"
        else:
            status_filter = Trade.status.in_(["FILLED", "PENDING"])
        result = await session.execute(
            select(func.coalesce(func.sum(Trade.amount_usdc), 0)).where(
                Trade.created_at >= today,
                status_filter,
            )
        )
        return float(result.scalar())

    @property
    def mode_label(self) -> str:
        return "PAPER TRADING (DRY RUN)" if self.dry_run else "LIVE TRADING"
