"""
FastAPI backend — serves dashboard data + manual controls.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from loguru import logger
import os

from backend.config import settings
from backend.database import init_db, get_session, Market, Prediction, Trade, DailyStats
from backend.scanner import MarketScanner
from backend.trader import Trader
from backend.pnl_tracker import PnLTracker

pnl_tracker = PnLTracker()

app = FastAPI(title="Polymarket Predictor", version="1.0.0")

# Global scanner instance
scanner = MarketScanner()
_scanner_task: Optional[asyncio.Task] = None


# ------------------------------------------------------------------ #
#  Startup / Shutdown                                                 #
# ------------------------------------------------------------------ #
@app.on_event("startup")
async def startup():
    await init_db()
    logger.info(f"DB initialized | Mode: {scanner.trader.mode_label}")


@app.on_event("shutdown")
async def shutdown():
    scanner.stop()
    await scanner.client.close()


# ------------------------------------------------------------------ #
#  Dashboard HTML                                                      #
# ------------------------------------------------------------------ #
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    with open(html_path) as f:
        return f.read()


# ------------------------------------------------------------------ #
#  API endpoints                                                       #
# ------------------------------------------------------------------ #
@app.get("/api/status")
async def get_status(session: AsyncSession = Depends(get_session)):
    balance = await scanner.trader.paper_balance(session) if settings.dry_run else None
    return {
        "mode": scanner.trader.mode_label,
        "dry_run": settings.dry_run,
        "scanner_running": _scanner_task is not None and not _scanner_task.done(),
        "scan_count": scanner._scan_count,
        "max_bet_usdc": settings.max_bet_usdc,
        "min_edge": settings.min_edge,
        "min_confidence": settings.min_confidence,
        "max_daily_spend": settings.max_daily_spend,
        "paper_balance": balance,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    """Manually trigger a market scan (ignored if one is already running)."""
    if scanner._scanning:
        return {"status": "already_scanning", "message": "Scan already in progress"}
    background_tasks.add_task(scanner.run_scan)
    return {"status": "scan_started", "message": "Scan triggered in background"}


@app.post("/api/start_loop")
async def start_loop(interval: int = Query(default=900, ge=60, le=3600)):
    """Start the automatic scanning loop."""
    global _scanner_task
    if _scanner_task and not _scanner_task.done():
        return {"status": "already_running"}
    _scanner_task = asyncio.create_task(scanner.start_loop(interval))
    return {"status": "loop_started", "interval_seconds": interval}


@app.post("/api/stop_loop")
async def stop_loop():
    """Stop the automatic scanning loop."""
    scanner.stop()
    return {"status": "loop_stopping"}


@app.get("/api/markets")
async def get_markets(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Market)
        .order_by(desc(Market.volume_24h))
        .limit(limit)
    )
    markets = result.scalars().all()
    return [
        {
            "condition_id": m.condition_id,
            "question": m.question,
            "category": m.category,
            "yes_price": m.yes_price,
            "no_price": m.no_price,
            "volume_24h": m.volume_24h,
            "liquidity": m.liquidity,
            "last_updated": m.last_updated.isoformat() if m.last_updated else None,
        }
        for m in markets
    ]


@app.get("/api/predictions")
async def get_predictions(
    limit: int = 30,
    signal_filter: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    query = select(Prediction).order_by(desc(Prediction.created_at)).limit(limit)
    result = await session.execute(query)
    preds = result.scalars().all()

    data = [
        {
            "id": p.id,
            "question": p.question,
            "signal": p.signal,
            "edge": round(p.edge, 3),
            "confidence": round(p.confidence, 3),
            "predicted_yes_prob": round(p.predicted_yes_prob, 3),
            "market_yes_price": round(p.market_yes_price, 3),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in preds
    ]
    if signal_filter:
        data = [d for d in data if signal_filter.upper() in d["signal"]]
    return data


@app.get("/api/trades")
async def get_trades(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Trade).order_by(desc(Trade.created_at)).limit(limit)
    )
    trades = result.scalars().all()
    return [
        {
            "id": t.id,
            "question": t.question,
            "side": t.side,
            "amount_usdc": t.amount_usdc,
            "price": t.price,
            "status": t.status,
            "order_id": t.order_id,
            "pnl": t.pnl,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in trades
    ]


@app.get("/api/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    """Summary stats for the dashboard."""
    # Total trades
    total_trades = await session.execute(select(func.count(Trade.id)))
    # Total spent
    total_spent = await session.execute(
        select(func.coalesce(func.sum(Trade.amount_usdc), 0))
    )
    # Total unique markets with predictions (one row per market after upsert)
    preds_today = await session.execute(
        select(func.count(Prediction.id))
    )
    # Buy signals
    buy_signals = await session.execute(
        select(func.count(Prediction.id)).where(
            Prediction.signal != "SKIP"
        )
    )
    # Average edge across actionable buy signals
    avg_edge_result = await session.execute(
        select(func.avg(Prediction.edge)).where(Prediction.signal != "SKIP")
    )

    n_trades  = total_trades.scalar() or 0
    n_spent   = float(total_spent.scalar() or 0)
    n_preds   = preds_today.scalar() or 0
    n_signals = buy_signals.scalar() or 0
    avg_edge  = float(avg_edge_result.scalar() or 0)

    return {
        "total_trades": n_trades,
        "total_spent_usdc": round(n_spent, 2),
        "predictions_today": n_preds,
        "buy_signals_total": n_signals,
        "avg_edge_pct": round(avg_edge * 100, 1),
        "mode": scanner.trader.mode_label,
    }


@app.get("/api/pnl/debug")
async def debug_pnl(session: AsyncSession = Depends(get_session)):
    """Show sample condition_ids from DB and check them against Gamma API."""
    import httpx
    result = await session.execute(
        select(Trade).where(Trade.status == "DRY_RUN").limit(5)
    )
    trades = result.scalars().all()
    debug = []
    async with httpx.AsyncClient(timeout=10) as client:
        for t in trades:
            # Try direct lookup
            r1 = await client.get(f"https://gamma-api.polymarket.com/markets/{t.condition_id}")
            # Try conditionIds param
            r2 = await client.get("https://gamma-api.polymarket.com/markets",
                                   params={"conditionIds": t.condition_id, "limit": 1})
            d1 = r1.json() if r1.status_code == 200 else f"HTTP {r1.status_code}"
            d2 = r2.json() if r2.status_code == 200 else f"HTTP {r2.status_code}"
            items2 = d2 if isinstance(d2, list) else (d2.get("markets", []) if isinstance(d2, dict) else [])
            debug.append({
                "condition_id": t.condition_id[:30] + "...",
                "question": t.question[:50],
                "side": t.side,
                "path_lookup_status": r1.status_code,
                "path_lookup_has_data": bool(d1) and d1 != f"HTTP {r1.status_code}",
                "query_lookup_found": len(items2) > 0,
                "query_outcome_prices": items2[0].get("outcomePrices") if items2 else None,
                "query_closed": items2[0].get("closed") if items2 else None,
            })
    return debug


@app.get("/api/pnl")
async def get_pnl():
    """Return P&L summary from already-resolved trades."""
    return await pnl_tracker.get_summary()


@app.post("/api/pnl/update")
async def update_pnl(background_tasks: BackgroundTasks):
    """Check Polymarket for resolved markets and update trade P&L."""
    background_tasks.add_task(pnl_tracker.update_all)
    return {"status": "pnl_update_started"}
