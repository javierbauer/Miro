#!/usr/bin/env python3
"""
Audit paper-trade resolution against Polymarket ground truth.

The paper P&L shows underdogs (≤10¢) winning ~21% — 3x their implied
probability.  That's either a labeling bug or unrealistic fills.  This
script re-checks a sample of resolved paper trades directly against the
CLOB market data and flags any where our recorded win/loss disagrees with
the real winning outcome.  It also prints the Polymarket URL so you can
verify by eye.

Usage:
    cd /root/Miro2
    python scripts/audit_resolution.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import select
from backend.database import AsyncSessionLocal, Trade
from backend.config import settings

CLOB = "https://clob.polymarket.com"


async def fetch_market(http, condition_id):
    try:
        r = await http.get(f"{CLOB}/markets/{condition_id}")
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def main():
    # Sample: cheap-bucket trades (the suspicious ones), wins and losses
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Trade).where(Trade.status == "RESOLVED")
        )).scalars().all()

    paper = [t for t in rows
             if t.order_id and t.order_id.startswith("PAPER-")
             and t.pnl is not None and t.price <= 0.10]
    # take up to 20: the biggest recorded "wins" first (most suspicious)
    paper.sort(key=lambda t: t.pnl, reverse=True)
    sample = paper[:20]

    if not sample:
        print("No cheap-bucket resolved paper trades to audit.")
        return

    proxy = settings.proxy_url or None
    mism = 0
    checked = 0
    print(f"\nAuditing {len(sample)} cheap (≤10¢) resolved paper trades "
          f"against CLOB ground truth...\n")

    async with httpx.AsyncClient(timeout=20, proxy=proxy) as http:
        for t in sample:
            m = await fetch_market(http, t.condition_id)
            if not m or not m.get("closed"):
                print(f"  · {t.question[:45]:45s} | market not closed/found, skip")
                continue
            tokens = m.get("tokens", [])
            # winning outcome (token with winner=true)
            win_tok = next((tk for tk in tokens
                            if isinstance(tk, dict) and tk.get("winner") is True), None)
            if not win_tok:
                print(f"  · {t.question[:45]:45s} | no winner flag, skip")
                continue
            checked += 1
            # token[0]=YES, token[1]=NO  → did YES (index 0) win?
            yes_won = (tokens.index(win_tok) == 0)
            our_win = (t.side == "YES" and yes_won) or (t.side == "NO" and not yes_won)
            recorded_win = t.pnl > 0
            slug = m.get("market_slug", "")
            url = f"https://polymarket.com/event/{slug}" if slug else m.get("question", "")

            flag = "OK " if our_win == recorded_win else "❌ MISMATCH"
            if our_win != recorded_win:
                mism += 1
            print(f"  {flag} {t.side:3s} @ {t.price:.3f}  pnl=${t.pnl:+7.2f}  "
                  f"won_outcome='{win_tok.get('outcome','?')}'")
            print(f"         {t.question[:60]}")
            print(f"         {url}")

    print(f"\n--- Result: {checked} checked, {mism} mismatches ---")
    if mism == 0 and checked:
        print("Labeling looks CORRECT — the inflated win rate is therefore")
        print("about unrealistic paper FILLS (stale/in-game prices, no")
        print("slippage), which live trading will not reproduce.")
    elif mism:
        print("Labeling BUG confirmed — some recorded wins/losses disagree")
        print("with the real Polymarket outcome. The paper P&L is wrong.")


if __name__ == "__main__":
    asyncio.run(main())
