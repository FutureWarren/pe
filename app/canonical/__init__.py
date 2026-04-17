"""Canonical analyst-output layer.

This package owns the transformation from upstream resolved P&L periods into
the final analyst-facing artifacts: Model_Input, Exceptions, Source_Map.

Submodules:
  - formatting.py  : period labels, unit normalisation, display formatting
  - selector.py    : deterministic source hierarchy + source selection
  - validation.py  : formula closure + cross-source checks
  - confidence.py  : confidence scoring + status assignment + exception generation
  - build.py       : orchestrator producing ModelInputBundle
  - explain.py     : grounded natural-language answers for the AI copilot
"""
