#!/usr/bin/env python3
"""
Apply Claude's market recommendations as paper or live trades.

Reads a JSON file (Claude's response from get_candidates.py output) and
creates trades for BUY_YES / BUY_NO signals with sufficient edge.

Usage:
    python3 scripts/apply_recommendations.py recommendations.json
    cat recommendations.json | python3 scripts/apply_recommendations.py -

Expected JSON format (Claude's response):
    [
      {
        "condition_id": "0x...",
        "action": "BUY_YES",
        "prob": 0.55,
        "confidence": "high",
        "reasoning": "..."
      },
      ...
    ]
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.predictor import PredictionResult
from backend.trader import Trader

CLOB    = "https://clob.polymarket.com"
MIN_EDGE = 0.05

_CONF_WEIGHT = {"high": 1.0, "medium": 0.80, "low": 0.55}


async def fetch_current_yes_price(http: httpx.AsyncClient, condition_id: str) -> float | None:
    """Get live YES price from the CLOB order book."""
    try:
        r = await http.get(f"{CLOB}/markets/{condition_id}")
        if r.status_code != 200:
            return None
        tokens = r.json().get("tokens", [])
        if not tokens or not isinstance(tokens[0], dict):
            return None
        token_id = tokens[0].get("token_id")
        if not token_id:
            return None
        r2 = await http.get(f"{CLOB}/price", params={"token_id": token_id, "side": "buy"})
        if r2.status_code == 200:
            price = float(r2.json().get("price", 0))
            if 0.01 < price < 0.99:
                return price
    except Exception:
        pass
    return None


async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/apply_recommendations.py <recommendations.json>")
        print("       python3 scripts/apply_recommendations.py -   (stdin)")
        sys.exit(1)

    src = sys.argv[1]
    raw = sys.stdin.read() if src == "-" else open(src).read()

    try:
        recs = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON — {e}")
        sys.exit(1)

    if not isinstance(recs, list):
        print("ERROR: JSON must be an array of recommendation objects")
        sys.exit(1)

    trader  = Trader()
    proxy   = settings.proxy_url or None
    applied = skipped = errors = 0

    print(f"\nApplying {len(recs)} recommendations  [{trader.mode_label}]\n")

    async with httpx.AsyncClient(timeout=15, proxy=proxy) as http:
        async with AsyncSessionLocal() as session:
            for rec in recs:
                cid    = str(rec.get("condition_id") or "")
                action = str(rec.get("action") or rec.get("signal") or "SKIP").upper()
                prob   = float(rec.get("prob") or rec.get("probability") or 0)
                conf   = str(rec.get("confidence") or "medium").lower()
                reason = str(rec.get("reasoning") or "Claude recommendation")
                label  = str(rec.get("question") or cid[:20])

                if "SKIP" in action or not cid:
                    print(f"  SKIP  {label[:55]}  ← {reason[:60]}")
                    skipped += 1
                    continue

                # Fetch live price (not stale from scan time)
                yes_price = await fetch_current_yes_price(http, cid)
                if yes_price is None:
                    print(f"  ERR   {label[:55]}  ← can't fetch current price")
                    errors += 1
                    continue

                if "NO" in action:
                    bet_side  = "NO"
                    bet_price = round(1 - yes_price, 4)
                    edge      = (1 - prob) - bet_price
                else:
                    bet_side  = "YES"
                    bet_price = yes_price
                    edge      = prob - bet_price

                if edge < MIN_EDGE:
                    print(f"  SKIP  {label[:55]}  ← edge {edge:+.3f} < {MIN_EDGE} (price moved?)")
                    skipped += 1
                    continue

                conf_weight  = _CONF_WEIGHT.get(conf, 0.80)
                kelly        = round(max(0.0, min(0.05, (edge / (1 - bet_price)) * 0.5)), 4)
                confidence   = round(min(1.0, (edge / 0.15) * conf_weight), 3)

                pred = PredictionResult(
                    condition_id    = cid,
                    question        = label[:200],
                    market_yes_price= yes_price,
                    predicted_yes_prob = prob,
                    edge            = edge,
                    confidence      = confidence,
                    signal          = f"BUY_{bet_side}",
                    kelly_fraction  = kelly,
                    bet_side        = bet_side,
                    reasoning       = f"Claude ({conf}): {reason}",
                )

                trade = await trader.maybe_trade(pred, session)
                if trade:
                    print(f"  TRADE {bet_side} ${trade.amount_usdc:.2f} @ {bet_price:.3f} "
                          f"edge={edge:+.3f}  {label[:50]}")
                    applied += 1
                else:
                    print(f"  BLOCK {label[:55]}  ← budget exhausted or below config threshold")
                    skipped += 1

    print(f"\n{'─'*60}")
    print(f"  {applied} trades placed  |  {skipped} skipped  |  {errors} errors")
    print(f"  Mode: {trader.mode_label}")
    if applied and settings.dry_run:
        print("  (paper trades — set DRY_RUN=false to trade real money)")


if __name__ == "__main__":
    asyncio.run(main())
