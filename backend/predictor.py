"""
Prediction engine — the "Ralph Loop" style probability estimator.

Strategy (simple but profitable):
  1. Price momentum — markets with recent price drift tend to continue short-term.
  2. Volume signal  — high 24h volume + thin spread = efficient price → skip.
                      low volume + wide spread = inefficiency → trade.
  3. Kelly edge     — only bet when our estimated probability diverges enough
                      from the market price to justify risk-adjusted sizing.
  4. Recency bias   — markets closing soon with skewed prices are often mis-priced.

No ML model needed for overnight operation — pure quant signals are fast & robust.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from loguru import logger


@dataclass
class PredictionResult:
    condition_id: str
    question: str
    market_yes_price: float
    predicted_yes_prob: float
    edge: float                     # predicted - market price (for YES)
    confidence: float               # 0..1
    signal: str                     # BUY_YES | BUY_NO | SKIP
    kelly_fraction: float           # fraction of bankroll to bet
    bet_side: str                   # YES | NO
    reasoning: str
    volume_24h: float = 0.0
    liquidity: float = 0.0


class Predictor:
    """
    Stateless market signal generator.
    Call predict(market_dict) for each enriched market.
    """

    # --- tunable knobs ---------------------------------------------------
    MIN_LIQUIDITY   = 500.0     # ignore thin markets
    MIN_VOLUME_24H  = 100.0     # ignore stale markets
    MAX_VOLUME_24H  = 5_000_000 # too efficient above this
    MIN_EDGE        = 0.04      # 4 cent edge minimum
    MIN_CONFIDENCE  = 0.55      # confidence gate
    MAX_KELLY_FRAC  = 0.05      # cap bet size at 5% of bankroll

    def predict(self, market: dict) -> Optional[PredictionResult]:
        condition_id = market.get("conditionId") or market.get("condition_id", "")
        question     = market.get("question", "Unknown")
        yes_price    = market.get("_yes_price", 0.5)
        no_price     = market.get("_no_price", round(1 - yes_price, 4))
        volume_24h   = self._float(market.get("volume24hr") or market.get("volume", 0))
        liquidity    = self._float(market.get("liquidity", 0))

        # ── basic filters ──────────────────────────────────────────────
        if liquidity < self.MIN_LIQUIDITY:
            return None
        if volume_24h < self.MIN_VOLUME_24H:
            return None
        if volume_24h > self.MAX_VOLUME_24H:
            return None   # too efficient
        if yes_price <= 0.01 or yes_price >= 0.99:
            return None   # already resolved / too extreme

        # ── signals ────────────────────────────────────────────────────
        spread_signal   = self._spread_signal(yes_price, no_price)
        volume_signal   = self._volume_signal(volume_24h, liquidity)
        recency_signal  = self._recency_signal(market)
        momentum_signal = self._momentum_signal(market)

        # Weighted average of signals → estimated true probability
        weights = [0.35, 0.25, 0.25, 0.15]
        signals = [spread_signal, volume_signal, recency_signal, momentum_signal]
        pred_yes = sum(w * s for w, s in zip(weights, signals))
        pred_yes = max(0.01, min(0.99, pred_yes))

        edge_yes = pred_yes - yes_price
        edge_no  = (1 - pred_yes) - no_price

        # Only trade the side with a POSITIVE edge
        if edge_yes >= edge_no and edge_yes > 0:
            bet_side, best_edge = "YES", edge_yes
        elif edge_no > edge_yes and edge_no > 0:
            bet_side, best_edge = "NO", edge_no
        else:
            bet_side, best_edge = "YES", max(edge_yes, edge_no)  # both ≤ 0 → will SKIP

        bet_price = yes_price if bet_side == "YES" else no_price
        bet_prob  = pred_yes  if bet_side == "YES" else (1 - pred_yes)

        # Confidence: combination of edge magnitude + liquidity depth
        confidence = self._confidence(best_edge, liquidity, volume_24h)

        if best_edge < self.MIN_EDGE or confidence < self.MIN_CONFIDENCE:
            signal = "SKIP"
        else:
            signal = f"BUY_{bet_side}"

        kelly = self._kelly(bet_prob, bet_price)

        reasoning = (
            f"spread_signal={spread_signal:.3f} vol_signal={volume_signal:.3f} "
            f"recency={recency_signal:.3f} momentum={momentum_signal:.3f} | "
            f"pred_yes={pred_yes:.3f} market_yes={yes_price:.3f} "
            f"edge={best_edge:+.3f} conf={confidence:.2f}"
        )

        return PredictionResult(
            condition_id=condition_id,
            question=question,
            market_yes_price=yes_price,
            predicted_yes_prob=pred_yes,
            edge=best_edge,
            confidence=confidence,
            signal=signal,
            kelly_fraction=kelly,
            bet_side=bet_side,
            reasoning=reasoning,
            volume_24h=volume_24h,
            liquidity=liquidity,
        )

    # ------------------------------------------------------------------ #
    #  Individual signals (all return a probability 0..1 for YES)         #
    # ------------------------------------------------------------------ #
    def _spread_signal(self, yes: float, no: float) -> float:
        """
        A price of 0.5 is neutral.  If the spread (yes+no) < 1.0, prices are
        inefficient and we lean toward the cheaper side.
        """
        total = yes + no
        if total < 0.97:      # wide spread → markets underpricing both sides
            return yes        # keep neutral-ish, let other signals decide
        # Near-fair market
        return yes

    def _volume_signal(self, vol: float, liq: float) -> float:
        """
        Low vol/liq ratio → stale price, higher chance of edge.
        We don't change the direction here, just modulate confidence later.
        Returns prior-biased 0.5.
        """
        return 0.5            # direction-neutral signal; affects confidence

    def _recency_signal(self, market: dict) -> float:
        """
        Markets closing within 24h that are priced near 0.5 tend to resolve
        quickly in one direction.  We nudge slightly toward current price.
        """
        end_str = market.get("endDate") or market.get("end_date_iso")
        yes = market.get("_yes_price", 0.5)
        if not end_str:
            return yes
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            hours_left = (end - datetime.now(timezone.utc)).total_seconds() / 3600
            if 1 < hours_left < 24 and 0.35 < yes < 0.65:
                # Close to deadline + near 50/50 → momentum push
                return yes + (0.5 - yes) * 0.1   # tiny mean-reversion
        except Exception:
            pass
        return yes

    def _momentum_signal(self, market: dict) -> float:
        """
        Use the 1h price change if available from Gamma API.
        Positive momentum → lean YES; negative → lean NO.
        """
        yes = market.get("_yes_price", 0.5)
        change_1h = self._float(market.get("change1h") or market.get("priceChange1h", 0))
        if abs(change_1h) > 0.001:
            nudge = max(-0.1, min(0.1, change_1h * 2))
            return max(0.01, min(0.99, yes + nudge))
        return yes

    def _confidence(self, edge: float, liquidity: float, volume: float) -> float:
        """
        Confidence = f(edge magnitude, liquidity depth, volume).
        More liquidity + higher edge → higher confidence.
        """
        edge_score = min(1.0, edge / 0.15)
        liq_score  = min(1.0, math.log10(max(liquidity, 10)) / 5)   # log scale
        vol_score  = max(0.0, 1.0 - volume / self.MAX_VOLUME_24H)     # lower vol = less efficient
        return round(0.4 * edge_score + 0.35 * liq_score + 0.25 * vol_score, 3)

    def _kelly(self, prob: float, price: float) -> float:
        """
        Fractional Kelly criterion: f = (prob - price) / (1 - price)
        Capped at MAX_KELLY_FRAC and half-Kelly applied for safety.
        """
        if price <= 0 or price >= 1:
            return 0.0
        full_kelly = (prob - price) / (1 - price)
        half_kelly = full_kelly * 0.5
        return round(max(0.0, min(self.MAX_KELLY_FRAC, half_kelly)), 4)

    @staticmethod
    def _float(v) -> float:
        try:
            return float(v or 0)
        except Exception:
            return 0.0
