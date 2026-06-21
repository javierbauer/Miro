#!/usr/bin/env python3
"""
Fetch Polymarket candidates for manual Claude analysis.

Outputs a JSON array of markets that pass basic filters (liquidity, volume,
price range, time to close). Paste the output into Claude and ask it to
evaluate each market. Save Claude's response as recommendations.json, then:

    python3 scripts/apply_recommendations.py recommendations.json

Usage:
    cd /root/Miro2
    python3 scripts/get_candidates.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.polymarket_client import PolymarketClient

MIN_LIQUIDITY = 500.0
MIN_VOLUME    = 100.0
MAX_VOLUME    = 5_000_000.0
MIN_PRICE     = 0.05
MAX_PRICE     = 0.95
MAX_DAYS      = 7.0
MIN_DAYS      = 0.1


def days_left(market) -> float | None:
    end_str = market.get("endDate") or market.get("end_date_iso")
    if not end_str:
        return None
    try:
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return (end - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


async def main():
    client = PolymarketClient()
    try:
        markets = await client.get_top_markets(n=60)
    finally:
        await client.close()

    candidates = []
    for m in markets:
        yes_price = m.get("_yes_price", 0.5)
        no_price  = m.get("_no_price", round(1 - yes_price, 4))
        liquidity = float(m.get("liquidity") or 0)
        volume    = float(m.get("volume24hr") or m.get("volume") or 0)
        d         = days_left(m)

        if liquidity < MIN_LIQUIDITY:
            continue
        if not (MIN_VOLUME <= volume <= MAX_VOLUME):
            continue
        if not (MIN_PRICE < yes_price < MAX_PRICE):
            continue
        if d is None or not (MIN_DAYS < d <= MAX_DAYS):
            continue

        candidates.append({
            "condition_id": m.get("conditionId") or m.get("condition_id", ""),
            "question":     m.get("question", ""),
            "description":  (m.get("description") or "")[:300].strip(),
            "category":     m.get("category") or m.get("groupItemTitle") or "",
            "yes_price":    round(yes_price, 4),
            "no_price":     round(no_price, 4),
            "volume_24h":   int(volume),
            "liquidity":    int(liquidity),
            "days_left":    round(d, 2),
        })

    if not candidates:
        print("No candidates found right now (markets may have just closed or filters too strict).")
        print("Try again in a few minutes.")
        return

    print(f"\n# ─── {len(candidates)} market candidates — copy everything below this line ───\n")
    print(json.dumps(candidates, indent=2, ensure_ascii=False))
    print(f"""
# ─── Prompt to paste into Claude (after the JSON above) ───────────────────
#
# The JSON above lists {len(candidates)} active Polymarket prediction markets.
# Each resolves within {MAX_DAYS:.0f} days. Current market prices are in yes_price/no_price.
#
# For EACH market, estimate the true probability that YES resolves.
# If you don't have reliable information, return the market's own price (= no edge).
# Return ONLY a JSON array — no prose, no markdown fences:
#
# [
#   {{
#     "condition_id": "<same id from input>",
#     "action": "BUY_YES" | "BUY_NO" | "SKIP",
#     "prob": <your estimated YES probability, 0.0-1.0>,
#     "confidence": "low" | "medium" | "high",
#     "reasoning": "<1 sentence>"
#   }},
#   ...
# ]
#
# After Claude replies, save the JSON array to recommendations.json and run:
#   python3 scripts/apply_recommendations.py recommendations.json
""")


if __name__ == "__main__":
    asyncio.run(main())
