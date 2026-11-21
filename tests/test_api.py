"""Unit tests for the FastAPI backend route handlers and Pydantic models."""

from __future__ import annotations

from recipe_workbench.api.server import (
    AnalyzeRequest,
    CompileRequest,
    analyze_text,
    compile_live,
    health,
    picker_axes,
    picker_roots,
)


def test_api_health_and_picker() -> None:
    body = health()
    assert body["ok"] is True
    assert "graph_version" in body

    roots = picker_roots()
    assert len(roots) >= 1
    assert roots[0].id == "EquityOption"

    axes = picker_axes(root_id="EquityOption", verb="Compare", q="vol")
    assert any(a.id == "volatility_model" for a in axes)


def test_api_analyze_detects_separated_ore_keywords() -> None:
    data = analyze_text(
        AnalyzeRequest(
            title="Interactive ORE Keyword Workbench",
            paragraphs=[
                "Price a 30-day ATM American put on SPY using Heston and Local Vol, and check delta."
            ],
        )
    )
    assert data.analysis.has_ore_keywords is True
    assert len(data.paragraph_spans) == 1
    assert len(data.paragraph_spans[0]) >= 5
    # Unresolved American put triggers inline ambiguity prompt
    assert data.ambiguity is not None
    assert data.ambiguity.key == "american_put"


def test_api_compile_runs_live_ore() -> None:
    data = compile_live(
        CompileRequest(
            title="Interactive ORE Keyword Workbench",
            paragraphs=[
                "Price a 30-day ATM American put on SPY using Heston and Local Vol, and check delta."
            ],
            disambiguation="ore_instrument",
            selected_models=["heston", "local_vol"],
            selected_calculators=["compare_vol_models"],
        )
    )
    assert data.comparison_result is not None
    assert len(data.comparison_result.rows) == 2
