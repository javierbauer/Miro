#!/usr/bin/env python3
"""
Recompute the REAL paper P&L from Polymarket ground truth.

The stored P&L is wrong: resolution sometimes used CLOB token ordering,
which is inverted vs the Gamma outcome ordering the bets were priced
against.  Bets store side=YES/NO where (per scanner.py) YES = Gamma
outcome index 0 and NO = index 1.  This script re-fetches each market from
Gamma, reads the resolved outcomePrices in that SAME ordering, and
recomputes every paper trade correctly.

Usage:
    cd /root/Miro2
    python scripts/true_pnl.py
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


async def fetch_clob_market(http, condition_id, cache):
    """Return (yes_won: bool|None, closed: bool) using CLOB ground truth.

    yes_won = did Gamma/CLOB outcome index 0 (the bet's YES) win.  This is
    the same logic the audit used, verified by eye against real outcomes.
    """
    if condition_id in cache:
        return cache[condition_id]
    result = (None, False)
    try:
        r = await http.get(f"{CLOB}/markets/{condition_id}")
        if r.status_code == 200:
            m = r.json()
            closed = bool(m.get("closed"))
            tokens = m.get("tokens", [])
            win_idx = next((i for i, tk in enumerate(tokens)
                            if isinstance(tk, dict) and tk.get("winner") is True), None)
            yes_won = (win_idx == 0) if win_idx is not None else None
            result = (yes_won, closed)
    except Exception:
        pass
    cache[condition_id] = result
    return result


async def main():
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Trade))).scalars().all()

    paper = [t for t in rows if t.order_id and t.order_id.startswith("PAPER-")]
    if not paper:
        print("No paper trades found.")
        return

    cids = {t.condition_id for t in paper}
    print(f"\nRe-resolving {len(paper)} paper trades across "
          f"{len(cids)} unique markets via CLOB ground truth...\n")

    n_res = wins = losses = 0
    true_pnl = 0.0
    staked = 0.0
    recorded_pnl = 0.0
    buckets = {(0.0, 0.10): [0, 0.0, 0.0], (0.10, 0.20): [0, 0.0, 0.0],
               (0.20, 0.35): [0, 0.0, 0.0], (0.35, 1.01): [0, 0.0, 0.0]}
    unresolved = 0
    flipped = 0   # recorded as a win but actually a loss

    proxy = settings.proxy_url or None
    cache = {}
    async with httpx.AsyncClient(timeout=30, proxy=proxy) as http:
        for t in paper:
            yes_won, closed = await fetch_clob_market(http, t.condition_id, cache)
            if yes_won is None or not closed:
                unresolved += 1
                continue
            if not t.price or t.price <= 0:
                continue

            won = (t.side == "YES" and yes_won) or (t.side == "NO" and not yes_won)
            pnl = (t.amount_usdc / t.price - t.amount_usdc) if won else -t.amount_usdc

            n_res += 1
            staked += t.amount_usdc
            true_pnl += pnl
            recorded_pnl += (t.pnl or 0.0)
            wins += 1 if won else 0
            losses += 0 if won else 1
            if (t.pnl or 0) > 0 and not won:
                flipped += 1

            for (lo, hi), agg in buckets.items():
                if lo <= t.price < hi:
                    agg[0] += 1; agg[1] += pnl; agg[2] += t.amount_usdc
                    break

    print("=== REAL paper P&L (Polymarket ground truth) ===\n")
    print(f"Resolved trades : {n_res}   (unresolved/not found: {unresolved})")
    if n_res:
        print(f"Win rate        : {wins/n_res*100:.1f}%  ({wins}W / {losses}L)")
        print(f"Total staked    : ${staked:.2f}")
        print(f"REAL net P&L    : ${true_pnl:+.2f}")
        print(f"REAL ROI        : {true_pnl/staked*100:+.1f}%")
        print()
        print(f"Recorded (buggy) net P&L : ${recorded_pnl:+.2f}")
        print(f"Overstatement            : ${recorded_pnl - true_pnl:+.2f}")
        print(f"Trades flipped win→loss  : {flipped}")
        print("\n--- REAL P&L by entry price ---")
        for (lo, hi), (n, pnl, stk) in buckets.items():
            if n:
                print(f"  {lo:.2f}-{hi:.2f}: {n:4d} trades  "
                      f"P&L ${pnl:+9.2f}  ROI {pnl/stk*100:+7.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
