"""
FastAPI backend — serves dashboard data + manual controls.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, BackgroundTasks, Query, Request
import httpx as _httpx
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
    global _scanner_task
    await init_db()
    logger.info(f"DB initialized | Mode: {scanner.trader.mode_label}")
    _scanner_task = asyncio.create_task(scanner.start_loop(900))
    logger.info("Scanner loop auto-started (interval=900s)")


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


@app.get("/api/live_balance")
async def get_live_balance(session: AsyncSession = Depends(get_session)):
    """Rich live P&L snapshot: CLOB cash + open positions + resolved P&L."""
    # Open live trades (real money at stake, markets not yet resolved)
    open_result = await session.execute(
        select(Trade).where(Trade.status.in_(["FILLED", "PENDING"]))
    )
    open_trades = [t for t in open_result.scalars().all()
                   if t.order_id and not t.order_id.startswith("PAPER-")]

    # Resolved live trades (P&L already computed)
    resolved_result = await session.execute(
        select(Trade).where(Trade.status == "RESOLVED")
    )
    resolved_trades = [t for t in resolved_result.scalars().all()
                       if t.order_id and not t.order_id.startswith("PAPER-")]

    # CLOB cash balance — reuses scanner.trader's 60s cache, so the
    # dashboard's 30s poll never hits the CLOB more than once per minute.
    clob_balance: Optional[float] = None
    if not settings.dry_run:
        try:
            clob_balance = await scanner.trader.live_bankroll()
        except Exception:
            pass

    wins            = sum(1 for t in resolved_trades if t.pnl and t.pnl > 0)
    losses          = sum(1 for t in resolved_trades if t.pnl and t.pnl <= 0)
    resolved_pnl    = round(sum(t.pnl for t in resolved_trades if t.pnl), 2)
    resolved_staked = sum(t.amount_usdc for t in resolved_trades)
    open_staked     = round(sum(t.amount_usdc for t in open_trades), 2)

    return {
        "clob_balance":   clob_balance,
        "open_count":     len(open_trades),
        "open_staked":    open_staked,
        "resolved":       len(resolved_trades),
        "wins":           wins,
        "losses":         losses,
        "resolved_pnl":   resolved_pnl,
        "total_invested": round(resolved_staked + open_staked, 2),
        "roi_pct":        round(resolved_pnl / resolved_staked * 100, 2) if resolved_staked else 0,
        "mode":           "live",
    }


@app.get("/api/candidates")
async def get_candidates():
    """Fetch and filter market candidates for Claude analysis."""
    from datetime import timezone
    client = PolymarketClient()
    try:
        markets = await client.get_top_markets(n=60)
    finally:
        await client.close()

    MIN_LIQ, MIN_VOL, MAX_VOL = 500.0, 100.0, 5_000_000.0
    MIN_P, MAX_P, MIN_D, MAX_D = 0.05, 0.95, 0.1, 7.0

    candidates = []
    for m in markets:
        yes_price = m.get("_yes_price", 0.5)
        no_price  = m.get("_no_price", round(1 - yes_price, 4))
        liquidity = float(m.get("liquidity") or 0)
        volume    = float(m.get("volume24hr") or m.get("volume") or 0)

        end_str = m.get("endDate") or m.get("end_date_iso")
        d = None
        if end_str:
            try:
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                d = (end - datetime.now(timezone.utc)).total_seconds() / 86400
            except Exception:
                pass

        if liquidity < MIN_LIQ: continue
        if not (MIN_VOL <= volume <= MAX_VOL): continue
        if not (MIN_P < yes_price < MAX_P): continue
        if d is None or not (MIN_D < d <= MAX_D): continue

        candidates.append({
            "condition_id": m.get("conditionId") or m.get("condition_id", ""),
            "question":     m.get("question", ""),
            "description":  (m.get("description") or "")[:300].strip(),
            "category":     m.get("category") or "",
            "yes_price":    round(yes_price, 4),
            "no_price":     round(no_price, 4),
            "volume_24h":   int(volume),
            "liquidity":    int(liquidity),
            "days_left":    round(d, 2),
        })

    return candidates


@app.post("/api/apply_recommendations")
async def apply_recommendations(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Execute trades based on Claude's JSON recommendations."""
    from backend.predictor import PredictionResult

    try:
        recs = await request.json()
    except Exception as e:
        return {"error": f"Invalid JSON: {e}", "applied": 0, "skipped": 0, "errors": 0}

    if not isinstance(recs, list):
        return {"error": "Expected a JSON array", "applied": 0, "skipped": 0, "errors": 0}

    MIN_EDGE    = 0.05
    CONF_WEIGHT = {"high": 1.0, "medium": 0.80, "low": 0.55}
    proxy       = settings.proxy_url or None
    trader      = scanner.trader
    applied     = []
    skipped     = []
    errors      = []

    async with _httpx.AsyncClient(timeout=15, proxy=proxy) as http:
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            cid    = str(rec.get("condition_id") or "")
            action = str(rec.get("action") or "SKIP").upper()
            prob   = float(rec.get("prob") or rec.get("probability") or 0)
            conf   = str(rec.get("confidence") or "medium").lower()
            reason = str(rec.get("reasoning") or "Claude recommendation")
            label  = str(rec.get("question") or cid[:30])

            if "SKIP" in action or not cid:
                skipped.append({"question": label, "reason": reason})
                continue

            # Fetch live YES price from CLOB
            try:
                r = await http.get(f"{settings.clob_host}/markets/{cid}")
                if r.status_code != 200:
                    errors.append({"question": label, "error": f"CLOB {r.status_code}"})
                    continue
                tokens = r.json().get("tokens", [])
                if not tokens or not isinstance(tokens[0], dict):
                    errors.append({"question": label, "error": "no tokens"}); continue
                tid = tokens[0].get("token_id")
                if not tid:
                    errors.append({"question": label, "error": "no token_id"}); continue
                r2 = await http.get(f"{settings.clob_host}/price",
                                    params={"token_id": tid, "side": "buy"})
                yes_price = float(r2.json().get("price", 0.5)) if r2.status_code == 200 else 0.5
            except Exception as e:
                errors.append({"question": label, "error": str(e)}); continue

            if "NO" in action:
                bet_side, bet_price = "NO", round(1 - yes_price, 4)
                edge = (1 - prob) - bet_price
            else:
                bet_side, bet_price = "YES", yes_price
                edge = prob - bet_price

            if edge < MIN_EDGE:
                skipped.append({"question": label,
                                 "reason": f"edge {edge:+.3f} < {MIN_EDGE} (price moved?)"})
                continue

            conf_w     = CONF_WEIGHT.get(conf, 0.80)
            kelly      = round(max(0.0, min(0.05, edge / (1 - bet_price) * 0.5)), 4)
            confidence = round(min(1.0, edge / 0.15 * conf_w), 3)

            pred = PredictionResult(
                condition_id=cid, question=label[:200],
                market_yes_price=yes_price, predicted_yes_prob=prob,
                edge=edge, confidence=confidence,
                signal=f"BUY_{bet_side}", kelly_fraction=kelly,
                bet_side=bet_side, reasoning=f"Claude ({conf}): {reason}",
            )
            trade = await trader.maybe_trade(pred, session)
            if trade:
                applied.append({"question": label, "side": bet_side,
                                 "amount": round(trade.amount_usdc, 2),
                                 "price": bet_price, "edge": round(edge, 3)})
            else:
                skipped.append({"question": label, "reason": "budget exhausted"})

    return {
        "applied": len(applied), "skipped": len(skipped), "errors": len(errors),
        "trades": applied, "skipped_list": skipped, "error_list": errors,
        "mode": trader.mode_label,
    }


@app.get("/api/pnl/debug")
async def debug_pnl(session: AsyncSession = Depends(get_session)):
    """Show resolution status of recent trades via CLOB API."""
    import httpx
    result = await session.execute(
        select(Trade).where(Trade.status == "DRY_RUN").order_by(Trade.created_at).limit(20)
    )
    trades = result.scalars().all()
    debug = []
    async with httpx.AsyncClient(timeout=10) as client:
        for t in trades:
            r = await client.get(f"https://clob.polymarket.com/markets/{t.condition_id}")
            data = r.json() if r.status_code == 200 else {}
            tokens = data.get("tokens", [])
            winner = next(
                (tok.get("outcome") for tok in tokens
                 if isinstance(tok, dict) and tok.get("winner") is True),
                None
            )
            debug.append({
                "question": t.question[:60],
                "side": t.side,
                "price": t.price,
                "closed": data.get("closed"),
                "winner": winner,
            })
    closed_count = sum(1 for d in debug if d["closed"])
    return {"closed_count": closed_count, "total_checked": len(debug), "trades": debug}


@app.get("/api/pnl")
async def get_pnl():
    """Return P&L summary from already-resolved trades."""
    return await pnl_tracker.get_summary()


@app.post("/api/pnl/update")
async def update_pnl(background_tasks: BackgroundTasks):
    """Check Polymarket for resolved markets and update trade P&L."""
    background_tasks.add_task(pnl_tracker.update_all)
    return {"status": "pnl_update_started"}
