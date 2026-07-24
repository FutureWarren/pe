"""Regression tests for the Gemini value-reconciliation backstop.

The number written to the databook must be re-derived from the cited raw token
by deterministic code, not accepted from the model — except where the model
applied legitimate contextual scaling.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.extract.gemini import _reconcile_metric_value


def _candidate(normalized_value, raw_value):
    return SimpleNamespace(normalized_value=normalized_value, raw_value=raw_value)


def test_transcription_slip_is_overridden_and_flagged() -> None:
    value, notes = _reconcile_metric_value(_candidate(1520.0, "1250"), "revenue")
    assert value == 1250.0
    assert notes  # flagged


def test_legitimate_contextual_scaling_is_preserved() -> None:
    # Raw token "1250" with a millions column the model saw → 1,250,000 is a clean
    # 1000x scale; keep the model's contextual value, no flag.
    value, notes = _reconcile_metric_value(_candidate(1_250_000.0, "1250"), "revenue")
    assert value == 1_250_000.0
    assert not notes


def test_self_describing_token_agrees() -> None:
    value, notes = _reconcile_metric_value(_candidate(5_000_000.0, "5m"), "revenue")
    assert value == 5_000_000.0
    assert not notes


def test_unparseable_raw_keeps_model_value() -> None:
    value, notes = _reconcile_metric_value(_candidate(999.0, "see attached"), "ebitda")
    assert value == 999.0
    assert not notes
