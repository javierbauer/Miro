import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import Column, String, Float, DateTime, Integer, Boolean, Text
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "polymarket.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    condition_id = Column(String, primary_key=True)
    question = Column(String)
    category = Column(String)
    end_date = Column(DateTime, nullable=True)
    yes_price = Column(Float, default=0.5)
    no_price = Column(Float, default=0.5)
    volume_24h = Column(Float, default=0.0)
    liquidity = Column(Float, default=0.0)
    last_updated = Column(DateTime, default=datetime.utcnow)
    active = Column(Boolean, default=True)


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String)
    question = Column(String)
    predicted_yes_prob = Column(Float)
    market_yes_price = Column(Float)
    edge = Column(Float)
    confidence = Column(Float)
    signal = Column(String)          # BUY_YES, BUY_NO, SKIP
    reasoning = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String)
    question = Column(String)
    side = Column(String)            # YES, NO
    amount_usdc = Column(Float)
    price = Column(Float)
    status = Column(String)          # PENDING, FILLED, CANCELLED, DRY_RUN
    order_id = Column(String, nullable=True)
    pnl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DailyStats(Base):
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String)
    markets_scanned = Column(Integer, default=0)
    predictions_made = Column(Integer, default=0)
    trades_placed = Column(Integer, default=0)
    total_spent = Column(Float, default=0.0)
    estimated_pnl = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session():
    async with AsyncSessionLocal() as session:
        yield session
