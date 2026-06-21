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
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import select
from backend.database import AsyncSessionLocal, Trade
from backend.config import settings

GAMMA = "https://gamma-api.polymarket.com"


def winning_index(outcome_prices):
    """Return 0 or 1 for the winning Gamma outcome, or None if unresolved."""
    try:
        op = [float(x) for x in outcome_prices]
    except Exception:
        return None
    if len(op) < 2:
        return None
    if max(op) > 0.9 and min(op) < 0.1:          # cleanly resolved
        return 0 if op[0] > op[1] else 1
    return None


async def fetch_gamma_markets(http, condition_ids):
    """Map condition_id -> (outcomePrices, outcomes, closed) from Gamma."""
    out = {}
    BATCH = 20
    ids = list(condition_ids)
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            r = await http.get(f"{GAMMA}/markets", params=[
                ("limit", "100"), *[("condition_ids", c) for c in batch]
            ])
            if r.status_code != 200:
                continue
            for m in r.json():
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid:
                    continue
                raw = m.get("outcomePrices")
                if isinstance(raw, str):
                    try: raw = json.loads(raw)
                    except Exception: raw = None
                names = m.get("outcomes")
                if isinstance(names, str):
                    try: names = json.loads(names)
                    except Exception: names = None
                out[cid] = (raw, names, m.get("closed"))
        except Exception:
            continue
        await asyncio.sleep(0.15)
    return out


async def main():
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Trade))).scalars().all()

    paper = [t for t in rows if t.order_id and t.order_id.startswith("PAPER-")]
    if not paper:
        print("No paper trades found.")
        return

    cids = {t.condition_id for t in paper}
    print(f"\nRe-resolving {len(paper)} paper trades across "
          f"{len(cids)} unique markets via Gamma...\n")

    proxy = settings.proxy_url or None
    async with httpx.AsyncClient(timeout=30, proxy=proxy) as http:
        markets = await fetch_gamma_markets(http, cids)

    n_res = wins = losses = 0
    true_pnl = 0.0
    staked = 0.0
    recorded_pnl = 0.0
    buckets = {(0.0, 0.10): [0, 0.0, 0.0], (0.10, 0.20): [0, 0.0, 0.0],
               (0.20, 0.35): [0, 0.0, 0.0], (0.35, 1.01): [0, 0.0, 0.0]}
    unresolved = 0
    flipped = 0   # trades whose correct result differs from what was recorded

    for t in paper:
        info = markets.get(t.condition_id)
        if not info:
            unresolved += 1
            continue
        op, names, closed = info
        win_idx = winning_index(op) if op else None
        if win_idx is None:
            unresolved += 1
            continue

        bot_idx = 0 if t.side == "YES" else 1
        won = (bot_idx == win_idx)
        if not t.price or t.price <= 0:
            continue
        pnl = (t.amount_usdc / t.price - t.amount_usdc) if won else -t.amount_usdc

        n_res += 1
        staked += t.amount_usdc
        true_pnl += pnl
        recorded_pnl += (t.pnl or 0.0)
        wins += 1 if won else 0
        losses += 0 if won else 1
        if (t.pnl or 0) > 0 and not won:
            flipped += 1   # was recorded win, actually a loss

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
