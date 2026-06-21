"""
Claude-powered probability estimator for Polymarket binary markets.

Replaces the heuristic weighted-average with real AI reasoning.  Claude
evaluates the market question and context to produce an independent
probability estimate; the calling code uses that to compute edge vs the
current market price and decide whether to trade.

Caching: results are stored in a module-level dict for _CACHE_TTL seconds
(default 1 h) so a market scanned every 15 minutes only calls the API once
per hour.

Fallback: any exception (missing key, network error, bad JSON) returns the
current market price, which produces zero edge and a SKIP signal — the
conservative outcome.
"""
import json
import time
from typing import Optional

from loguru import logger


_CACHE: dict[str, tuple[float, float, str, float]] = {}
# {condition_id: (prob, ai_conf_0_to_1, reasoning, monotonic_ts)}
_CACHE_TTL = 3600   # seconds


# Maps the AI's text confidence label to a numeric modifier applied to the
# Predictor._confidence() score so low-confidence AI calls require more edge.
_CONF_WEIGHT = {"high": 1.0, "medium": 0.80, "low": 0.55}

_SYSTEM_PROMPT = """\
You are an expert in prediction markets and probabilistic forecasting.
You will be given a binary prediction market question and must estimate
the true probability that YES resolves.

Rules:
- Return ONLY valid JSON — no prose outside the JSON object.
- If you lack reliable information, set confidence to "low" and return
  a probability close to the market price (you have no edge).
- Be honest about uncertainty; do not fabricate news or statistics.
- Your knowledge has a training cutoff; flag that if the question depends
  on very recent events you cannot verify.
"""

_USER_TEMPLATE = """\
Market question: {question}
{description_line}Category: {category}
Days until resolution: {days_str}
Current market price for YES: {yes_price:.3f}  (market implies {yes_pct:.1f}% probability)

Estimate the true probability that YES resolves.  Consider base rates,
resolution criteria, time remaining, and any relevant knowledge you have.
If the market price already looks fair, say so and return close to it.

Respond in JSON:
{{
  "probability": <float 0.0–1.0>,
  "confidence": <"low" | "medium" | "high">,
  "reasoning": "<1–2 concise sentences>"
}}"""


async def ai_estimate(
    condition_id: str,
    question: str,
    description: str,
    yes_price: float,
    category: str,
    days_left: Optional[float],
    api_key: str,
) -> tuple[float, float, str]:
    """Return (estimated_yes_prob, ai_confidence_0_to_1, reasoning).

    Falls back to (yes_price, 0.0, reason) on any error, which produces
    zero edge and a SKIP signal — the safe default.
    """
    cached = _CACHE.get(condition_id)
    if cached:
        prob, conf, reasoning, ts = cached
        if time.monotonic() - ts < _CACHE_TTL:
            return prob, conf, reasoning

    try:
        from anthropic import AsyncAnthropic

        days_str = f"{days_left:.1f}" if days_left is not None else "unknown"
        desc_line = f"Description: {description[:400]}\n" if description else ""

        user_msg = _USER_TEMPLATE.format(
            question=question,
            description_line=desc_line,
            category=category or "General",
            days_str=days_str,
            yes_price=yes_price,
            yes_pct=yes_price * 100,
        )

        client = AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break

        data = json.loads(raw)
        prob = float(data["probability"])
        prob = max(0.01, min(0.99, prob))
        conf_label = str(data.get("confidence", "medium")).lower()
        conf_weight = _CONF_WEIGHT.get(conf_label, 0.80)
        reasoning = str(data.get("reasoning", "AI estimate"))

        _CACHE[condition_id] = (prob, conf_weight, reasoning, time.monotonic())
        logger.debug(
            f"[AI] {question[:60]} | prob={prob:.3f} conf={conf_label} "
            f"edge vs market={prob - yes_price:+.3f}"
        )
        return prob, conf_weight, reasoning

    except Exception as exc:
        logger.warning(f"AI estimate failed for {condition_id[:12]}: {exc}")
        return yes_price, 0.0, f"AI unavailable ({type(exc).__name__}) — market price used"
