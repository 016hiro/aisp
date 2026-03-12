"""Elastic-weight factor engine with veto rules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FactorResult:
    """Scoring result for a single stock."""

    scores: dict[str, float]
    dynamic_weights: dict[str, float]
    total_score: float
    veto: str | None = None


@dataclass
class VetoRule:
    """Hard rule that overrides scoring when triggered."""

    factor: str
    threshold: float
    direction: str  # "below" or "above"
    veto_action: str

    def is_triggered(self, score: float) -> bool:
        if self.direction == "below":
            return score < self.threshold
        return score > self.threshold


DEFAULT_VETO_RULES = [
    VetoRule("macro", 0.15, "below", "宏观环境极端悲观，禁止买入"),
    VetoRule("sentiment", 0.10, "below", "市场情绪极度恐慌，禁止买入"),
    VetoRule("sentiment", 0.95, "above", "市场情绪过度狂热，警告追高风险"),
]


def check_veto(scores: dict[str, float], rules: list[VetoRule]) -> str | None:
    """Check veto rules. Returns reason string or None."""
    for rule in rules:
        score = scores.get(rule.factor)
        if score is not None and rule.is_triggered(score):
            return rule.veto_action
    return None


def compute_dynamic_weights(
    scores: dict[str, float],
    base_weights: dict[str, float],
    elasticity: float = 2.0,
) -> dict[str, float]:
    """Compute elastic weights: extreme factors get boosted.

    For each factor:
      extremity = |score - 0.5| * 2          # 0=neutral, 1=extreme
      raw_weight = base_weight * (1 + alpha * extremity)
    Then normalize so weights sum to 1.0.
    """
    raw: dict[str, float] = {}
    for factor, base_w in base_weights.items():
        score = scores.get(factor, 0.5)
        extremity = abs(score - 0.5) * 2.0
        raw[factor] = base_w * (1.0 + elasticity * extremity)

    total = sum(raw.values())
    if total == 0:
        return {f: 1.0 / len(base_weights) for f in base_weights}

    return {f: w / total for f, w in raw.items()}


def score_stock(
    scores: dict[str, float],
    base_weights: dict[str, float],
    elasticity: float = 2.0,
    veto_rules: list[VetoRule] | None = None,
) -> FactorResult:
    """Score a stock with elastic weights and veto rules.

    1. Check veto rules — if triggered, total_score=0
    2. Compute dynamic weights
    3. Weighted sum
    """
    if veto_rules is None:
        veto_rules = DEFAULT_VETO_RULES

    veto = check_veto(scores, veto_rules)
    dynamic_weights = compute_dynamic_weights(scores, base_weights, elasticity)

    if veto:
        return FactorResult(
            scores=scores,
            dynamic_weights=dynamic_weights,
            total_score=0.0,
            veto=veto,
        )

    total = sum(dynamic_weights[f] * scores.get(f, 0.5) for f in base_weights)

    return FactorResult(
        scores=scores,
        dynamic_weights=dynamic_weights,
        total_score=round(total, 4),
    )
