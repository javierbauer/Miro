#!/usr/bin/env python3
"""
Analyze paper-trading results to distinguish real edge from lucky variance.

The strategy buys cheap "underdog" tokens, so a single winner can pay 3-15x
and dominate the P&L.  This script breaks the results down so you can see
whether profits come from a consistent edge or a handful of longshots.

Usage:
    cd /root/Miro2
    python scripts/analyze_paper.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from backend.database import AsyncSessionLocal, Trade


def bar(frac, width=24):
    n = int(max(0.0, min(1.0, frac)) * width)
    return "█" * n + "·" * (width - n)


async def main():
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Trade).where(Trade.status == "RESOLVED")
        )).scalars().all()

    # paper trades only (live orders don't start with PAPER-)
    trades = [t for t in rows
              if t.order_id and t.order_id.startswith("PAPER-") and t.pnl is not None]

    if not trades:
        print("No resolved paper trades yet — nothing to analyze.")
        return

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl    = sum(t.pnl for t in trades)
    total_staked = sum(t.amount_usdc for t in trades)
    gross_win    = sum(t.pnl for t in wins)
    gross_loss   = sum(t.pnl for t in losses)

    print("\n=== Paper trading breakdown ===\n")
    print(f"Resolved trades : {len(trades)}")
    print(f"Win rate        : {len(wins)/len(trades)*100:.1f}%  "
          f"({len(wins)}W / {len(losses)}L)")
    print(f"Total staked    : ${total_staked:.2f}")
    print(f"Net P&L         : ${total_pnl:+.2f}")
    print(f"ROI             : {total_pnl/total_staked*100:+.1f}%")
    print(f"Avg win         : ${(gross_win/len(wins)) if wins else 0:+.2f}")
    print(f"Avg loss        : ${(gross_loss/len(losses)) if losses else 0:+.2f}")

    # ---- concentration: how much of the profit is a few big wins? --------
    top = sorted(wins, key=lambda t: t.pnl, reverse=True)
    print("\n--- Top 5 winning trades (entry price → payout) ---")
    for t in top[:5]:
        mult = (t.amount_usdc / t.price) / t.amount_usdc if t.price else 0
        print(f"  ${t.pnl:+7.2f}  @ {t.price:.3f} ({mult:4.1f}x)  "
              f"{(t.question or '')[:50]}")

    if gross_win > 0:
        top3 = sum(t.pnl for t in top[:3])
        share = top3 / gross_win * 100
        print(f"\nTop 3 wins = {share:.0f}% of ALL gross profit "
              f"({bar(share/100)})")
        if share > 60:
            print("  ⚠️  Profit is concentrated in a few longshots — this is "
                  "the variance signature, NOT a repeatable edge.")

    # ---- edge check by entry-price bucket --------------------------------
    print("\n--- P&L by entry price (are cheap longshots actually +EV?) ---")
    buckets = [(0.0, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 1.01)]
    for lo, hi in buckets:
        b = [t for t in trades if lo <= t.price < hi]
        if not b:
            continue
        bw = sum(1 for t in b if t.pnl > 0)
        bp = sum(t.pnl for t in b)
        bs = sum(t.amount_usdc for t in b)
        print(f"  {lo:.2f}-{hi:.2f}: {len(b):3d} trades  "
              f"win {bw/len(b)*100:4.0f}%  P&L ${bp:+8.2f}  "
              f"ROI {bp/bs*100:+6.1f}%")

    print("\nInterpretation:")
    print("  • If ROI is positive only because of 1-3 huge longshot hits,")
    print("    the strategy is variance-driven and likely mean-reverts to a")
    print("    loss as more trades resolve.")
    print("  • A real edge shows up as consistently positive ROI ACROSS the")
    print("    price buckets, not concentrated in one lucky bucket.\n")


if __name__ == "__main__":
    asyncio.run(main())
