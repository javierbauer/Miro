#!/usr/bin/env python3
"""
Counterfactual: what if every bet had been flipped (YES→NO, NO→YES)?

For each resolved paper trade we compute:
  - REAL P&L: what we actually recorded (corrected for the labeling bug)
  - FLIPPED P&L: same market, same amount, opposite side

The flip price is (1 - original_price), which matches the complementary
token's price at the time of entry.

Usage:
    cd /root/Miro2
    python scripts/flip_analysis.py
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
    if condition_id in cache:
        return cache[condition_id]
    result = (None, False)
    try:
        r = await http.get(f"{CLOB}/markets/{condition_id}")
        if r.status_code == 200:
            m = r.json()
            closed = bool(m.get("closed"))
            tokens = m.get("tokens", [])
            win_idx = next(
                (i for i, tk in enumerate(tokens)
                 if isinstance(tk, dict) and tk.get("winner") is True),
                None,
            )
            yes_won = (win_idx == 0) if win_idx is not None else None
            result = (yes_won, closed)
    except Exception:
        pass
    cache[condition_id] = result
    return result


def pnl(amount, price, won):
    return (amount / price - amount) if won else -amount


async def main():
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Trade))).scalars().all()

    paper = [t for t in rows if t.order_id and t.order_id.startswith("PAPER-")]
    if not paper:
        print("No paper trades found.")
        return

    proxy = settings.proxy_url or None
    cache = {}

    real_pnl = flip_pnl = 0.0
    real_staked = flip_staked = 0.0
    real_wins = real_losses = 0
    flip_wins = flip_losses = 0
    unresolved = 0

    # per-bucket accumulators: [real_n, real_pnl, real_staked, flip_pnl, flip_staked, flip_wins]
    buckets = {
        (0.00, 0.10): [0, 0.0, 0.0, 0.0, 0.0, 0],
        (0.10, 0.20): [0, 0.0, 0.0, 0.0, 0.0, 0],
        (0.20, 0.35): [0, 0.0, 0.0, 0.0, 0.0, 0],
        (0.35, 1.01): [0, 0.0, 0.0, 0.0, 0.0, 0],
    }

    async with httpx.AsyncClient(timeout=30, proxy=proxy) as http:
        for t in paper:
            yes_won, closed = await fetch_clob_market(http, t.condition_id, cache)
            if yes_won is None or not closed:
                unresolved += 1
                continue
            if not t.price or t.price <= 0 or t.price >= 1:
                continue

            won  = (t.side == "YES" and yes_won) or (t.side == "NO" and not yes_won)
            # Flipped: opposite side, complementary price
            flip_price = round(1 - t.price, 6)
            flip_won   = not won   # exactly the other outcome

            r_pnl = pnl(t.amount_usdc, t.price, won)
            f_pnl = pnl(t.amount_usdc, flip_price, flip_won)

            real_pnl   += r_pnl
            flip_pnl   += f_pnl
            real_staked += t.amount_usdc
            flip_staked += t.amount_usdc   # same amount, just different side
            real_wins  += 1 if won else 0
            real_losses+= 0 if won else 1
            flip_wins  += 1 if flip_won else 0
            flip_losses+= 0 if flip_won else 1

            for (lo, hi), agg in buckets.items():
                if lo <= t.price < hi:
                    agg[0] += 1
                    agg[1] += r_pnl
                    agg[2] += t.amount_usdc
                    agg[3] += f_pnl
                    agg[4] += t.amount_usdc
                    agg[5] += 1 if flip_won else 0
                    break

    n_res = real_wins + real_losses
    print("\n=== Counterfactual analysis: flip every bet ===\n")
    print(f"Resolved trades : {n_res}  (unresolved/not found: {unresolved})\n")

    if not n_res:
        print("Nothing to compare.")
        return

    print(f"{'Metric':<28} {'REAL (as played)':>18} {'FLIPPED (opposite)':>20}")
    print("-" * 68)
    print(f"{'Win rate':<28} {real_wins/n_res*100:>16.1f}%  {flip_wins/n_res*100:>18.1f}%")
    print(f"{'Wins / Losses':<28} {real_wins:>12}W / {real_losses}L   {flip_wins:>12}W / {flip_losses}L")
    print(f"{'Net P&L':<28} ${real_pnl:>+16.2f}  ${flip_pnl:>+18.2f}")
    print(f"{'ROI':<28} {real_pnl/real_staked*100:>16.1f}%  {flip_pnl/flip_staked*100:>18.1f}%")

    print("\n--- By entry price bucket ---\n")
    print(f"  {'Bucket':<12} {'N':>5}  "
          f"{'Real P&L':>10}  {'Real ROI':>8}  {'Flip P&L':>10}  {'Flip ROI':>8}  {'Flip WR':>7}")
    print("  " + "-" * 70)
    for (lo, hi), (n, rp, rs, fp, fs, fw) in buckets.items():
        if n == 0:
            continue
        print(f"  {lo:.2f}-{hi:.2f}     {n:>5}  "
              f"${rp:>+9.2f}  {rp/rs*100:>+7.1f}%  "
              f"${fp:>+9.2f}  {fp/fs*100:>+7.1f}%  "
              f"{fw/n*100:>6.1f}%")

    print()
    delta = flip_pnl - real_pnl
    if flip_pnl > real_pnl:
        print(f"Flipping EVERY bet would have earned ${delta:+.2f} MORE "
              f"({flip_pnl/flip_staked*100:+.1f}% ROI vs {real_pnl/real_staked*100:+.1f}%).")
        print("The predictor was systematically picking the WRONG side.")
    else:
        print(f"Flipping would have earned ${abs(delta):.2f} LESS.")
        print("The predictor was picking the right side (losses from bad sizing or variance).")


if __name__ == "__main__":
    asyncio.run(main())
