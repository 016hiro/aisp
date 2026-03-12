"""Unit tests for the elastic-weight factor engine."""

from __future__ import annotations

from aisp.screening.factor_engine import (
    VetoRule,
    check_veto,
    compute_dynamic_weights,
    score_stock,
)

BASE_WEIGHTS = {
    "fund": 0.20,
    "momentum": 0.10,
    "technical": 0.10,
    "quality": 0.05,
    "indicators": 0.20,
    "macro": 0.10,
    "sentiment": 0.15,
    "sector": 0.10,
}


def test_dynamic_weights_neutral():
    """All-neutral scores should produce weights close to base."""
    scores = {f: 0.5 for f in BASE_WEIGHTS}
    weights = compute_dynamic_weights(scores, BASE_WEIGHTS, elasticity=2.0)
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    # All extremities are 0, so weights should match base
    for f, w in weights.items():
        assert abs(w - BASE_WEIGHTS[f]) < 0.01


def test_dynamic_weights_extreme_boost():
    """An extreme macro factor should get boosted weight."""
    scores = {f: 0.5 for f in BASE_WEIGHTS}
    scores["macro"] = 0.05  # extreme pessimism

    weights = compute_dynamic_weights(scores, BASE_WEIGHTS, elasticity=2.0)
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    # Macro weight should be significantly higher than base 10%
    assert weights["macro"] > BASE_WEIGHTS["macro"] * 1.5


def test_veto_below():
    rules = [VetoRule("macro", 0.15, "below", "macro veto")]
    assert check_veto({"macro": 0.10}, rules) == "macro veto"
    assert check_veto({"macro": 0.20}, rules) is None


def test_veto_above():
    rules = [VetoRule("sentiment", 0.95, "above", "euphoria veto")]
    assert check_veto({"sentiment": 0.98}, rules) == "euphoria veto"
    assert check_veto({"sentiment": 0.50}, rules) is None


def test_veto_missing_factor():
    """Veto should not trigger if factor is missing from scores."""
    rules = [VetoRule("macro", 0.15, "below", "macro veto")]
    assert check_veto({}, rules) is None


def test_score_stock_normal():
    scores = {f: 0.6 for f in BASE_WEIGHTS}
    result = score_stock(scores, BASE_WEIGHTS, elasticity=2.0, veto_rules=[])
    assert result.total_score > 0
    assert result.veto is None
    assert abs(result.total_score - 0.6) < 0.05


def test_score_stock_veto():
    scores = {f: 0.6 for f in BASE_WEIGHTS}
    scores["macro"] = 0.05
    result = score_stock(scores, BASE_WEIGHTS, elasticity=2.0)
    assert result.total_score == 0.0
    assert result.veto is not None
    assert "宏观" in result.veto
