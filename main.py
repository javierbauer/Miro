#!/usr/bin/env python3
"""
Polymarket Predictor — main entry point.

Usage:
  python main.py              # Start web dashboard + API (http://localhost:8000)
  python main.py --scan-only  # Run one scan and exit (cron-friendly)
  python main.py --loop       # Start scanner loop only (no web server)
"""
import argparse
import asyncio
import sys
from loguru import logger


def main():
    parser = argparse.ArgumentParser(description="Polymarket Predictor")
    parser.add_argument("--scan-only", action="store_true", help="Run one scan and exit")
    parser.add_argument("--loop", action="store_true", help="Run scanner loop without web UI")
    parser.add_argument("--interval", type=int, default=900, help="Scan interval in seconds (default 900)")
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    if args.scan_only:
        asyncio.run(_run_scan_once())
    elif args.loop:
        asyncio.run(_run_loop(args.interval))
    else:
        _run_server(args.host, args.port)


async def _run_scan_once():
    from backend.database import init_db
    from backend.scanner import MarketScanner
    await init_db()
    scanner = MarketScanner()
    stats = await scanner.run_scan()
    logger.info(f"Scan complete: {stats}")


async def _run_loop(interval: int):
    from backend.database import init_db
    from backend.scanner import MarketScanner
    await init_db()
    scanner = MarketScanner()
    await scanner.start_loop(interval)


def _run_server(host=None, port=None):
    import uvicorn
    from backend.config import settings
    uvicorn.run(
        "backend.api:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
