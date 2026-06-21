"""
Prediction engine — Claude AI-powered probability estimator.

Strategy:
  1. Basic filters — skip illiquid, stale, too-efficient, or far-out markets.
  2. AI estimate  — call Claude to get a genuine probability for YES resolving,
                    independent of the market price.
  3. Edge check   — only bet when the AI probability diverges enough from the
                    market price to justify risk-adjusted sizing.
  4. Kelly sizing — fractional Kelly with confidence modulation.

When ANTHROPIC_API_KEY is not set, falls back to the heuristic estimator so
the bot can still run (with degraded accuracy) without credentials.
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
    Async market signal generator backed by Claude AI.

    Call `await predict(market_dict)` for each enriched market dict.
    Falls back to heuristic signals when Anthropic API key is not configured.
    """

    # --- tunable knobs ---------------------------------------------------
    MIN_LIQUIDITY       = 500.0
    MIN_VOLUME_24H      = 100.0
    MAX_VOLUME_24H      = 5_000_000
    MIN_EDGE            = 0.05        # slightly higher than heuristic — AI edges are real
    MIN_CONFIDENCE      = 0.55
    MAX_KELLY_FRAC      = 0.05
    MAX_DAYS_TO_CLOSE   = 7

    async def predict(self, market: dict) -> Optional[PredictionResult]:
        condition_id = market.get("conditionId") or market.get("condition_id", "")
        question     = market.get("question", "Unknown")
        yes_price    = market.get("_yes_price", 0.5)
        no_price     = market.get("_no_price", round(1 - yes_price, 4))
        volume_24h   = self._float(market.get("volume24hr") or market.get("volume", 0))
        liquidity    = self._float(market.get("liquidity", 0))
        description  = market.get("description") or market.get("shortDescription") or ""
        category     = market.get("category") or market.get("groupItemTitle") or ""

        # ── basic filters ──────────────────────────────────────────────
        if liquidity < self.MIN_LIQUIDITY:
            return None
        if volume_24h < self.MIN_VOLUME_24H:
            return None
        if volume_24h > self.MAX_VOLUME_24H:
            return None
        if yes_price <= 0.05 or yes_price >= 0.95:
            return None

        days_left = self._days_to_close(market)
        if days_left is not None and days_left > self.MAX_DAYS_TO_CLOSE:
            return None
        if days_left is not None and days_left < 0.1:
            return None

        # ── probability estimate (AI or heuristic fallback) ────────────
        pred_yes, ai_conf, ai_reasoning = await self._estimate_prob(
            condition_id, question, description, yes_price, category, days_left
        )

        edge_yes = pred_yes - yes_price
        edge_no  = (1 - pred_yes) - no_price

        if edge_yes >= edge_no and edge_yes > 0:
            bet_side, best_edge = "YES", edge_yes
        elif edge_no > edge_yes and edge_no > 0:
            bet_side, best_edge = "NO", edge_no
        else:
            bet_side, best_edge = "YES", max(edge_yes, edge_no)

        bet_price = yes_price if bet_side == "YES" else no_price
        bet_prob  = pred_yes  if bet_side == "YES" else (1 - pred_yes)

        confidence = self._confidence(best_edge, liquidity, volume_24h, days_left, ai_conf)

        if best_edge < self.MIN_EDGE or confidence < self.MIN_CONFIDENCE:
            signal = "SKIP"
        else:
            signal = f"BUY_{bet_side}"

        kelly = self._kelly(bet_prob, bet_price)

        days_str = f"{days_left:.1f}d" if days_left is not None else "unknown"
        reasoning = (
            f"{ai_reasoning} | "
            f"pred_yes={pred_yes:.3f} market_yes={yes_price:.3f} "
            f"edge={best_edge:+.3f} conf={confidence:.2f} closes_in={days_str}"
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
    #  Probability estimation                                              #
    # ------------------------------------------------------------------ #
    async def _estimate_prob(
        self,
        condition_id: str,
        question: str,
        description: str,
        yes_price: float,
        category: str,
        days_left: Optional[float],
    ) -> tuple[float, float, str]:
        """Return (prob, ai_conf_weight 0–1, reasoning).

        Tries Claude AI first; falls back to the heuristic blend if the
        API key is missing or any error occurs.
        """
        from backend.config import settings

        if settings.anthropic_api_key:
            from backend.ai_predictor import ai_estimate
            return await ai_estimate(
                condition_id=condition_id,
                question=question,
                description=description,
                yes_price=yes_price,
                category=category,
                days_left=days_left,
                api_key=settings.anthropic_api_key,
            )

        # Heuristic fallback (no AI key)
        return self._heuristic_prob(yes_price), 0.5, "heuristic (no AI key)"

    def _heuristic_prob(self, yes_price: float) -> float:
        """Simple mean-reversion heuristic used when AI is unavailable."""
        return round(yes_price * 0.75 + 0.5 * 0.25, 4)

    # ------------------------------------------------------------------ #
    #  Kelly + confidence                                                  #
    # ------------------------------------------------------------------ #
    def _confidence(
        self,
        edge: float,
        liquidity: float,
        volume: float,
        days_left: Optional[float] = None,
        ai_conf_weight: float = 1.0,
    ) -> float:
        edge_score = min(1.0, edge / 0.15)
        liq_score  = min(1.0, math.log10(max(liquidity, 10)) / 5)
        vol_score  = max(0.0, 1.0 - volume / self.MAX_VOLUME_24H)

        recency_bonus = 0.0
        if days_left is not None:
            if days_left <= 1:
                recency_bonus = 0.10
            elif days_left <= 3:
                recency_bonus = 0.05

        base = 0.4 * edge_score + 0.35 * liq_score + 0.25 * vol_score
        raw  = min(1.0, base + recency_bonus)
        # AI confidence modulates: low-confidence AI calls require more edge
        return round(raw * ai_conf_weight, 3)

    def _kelly(self, prob: float, price: float) -> float:
        if price <= 0 or price >= 1:
            return 0.0
        full_kelly = (prob - price) / (1 - price)
        half_kelly = full_kelly * 0.5
        return round(max(0.0, min(self.MAX_KELLY_FRAC, half_kelly)), 4)

    def _days_to_close(self, market: dict) -> Optional[float]:
        end_str = market.get("endDate") or market.get("end_date_iso")
        if not end_str:
            return None
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            return (end - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            return None

    @staticmethod
    def _float(v) -> float:
        try:
            return float(v or 0)
        except Exception:
            return 0.0
