"""Tests for the stepwise ComparativeWorkflowAgent backend."""

from __future__ import annotations

import pytest

from recipe_workbench.agent.workflow_builder import ComparativeWorkflowAgent


def test_stepwise_american_option_spy_call_two_vol_models() -> None:
    agent = ComparativeWorkflowAgent()

    # Step 1: Pass "American Option" -> downward dependency base classes
    disc = agent.start("American Option", mode="Compare")
    assert disc.root_id == "EquityOption"
    cat_names = [c.category for c in disc.dependency_categories]
    assert "pricing_model" in cat_names
    assert "volatility_model" in cat_names
    assert "pricing_engine" in cat_names
    assert "market_data" in cat_names

    # Step 2: User passes "models" -> available models for American Option
    models = agent.inspect_dependency("models")
    assert models.category == "pricing_model"
    assert any(o.key == "BlackScholesMerton" for o in models.options)

    vol_models = agent.inspect_dependency("volatility_model")
    vol_keys = [o.key for o in vol_models.options]
    assert "flat_vol" in vol_keys
    assert "local_vol" in vol_keys

    # Step 3: Ask how many workflows -> 2; vary volatility_model, share base model
    plan = agent.configure_workflows(num_workflows=2, varied_category="volatility_model")
    assert plan.num_workflows == 2
    assert plan.slot_counts["pricing_model"] == 1
    assert plan.slot_counts["volatility_model"] == 2
    assert plan.slot_counts["pricing_engine"] == 1

    # Step 4: Select shared dependencies (SPY Call, BSM, 10Y Treasury)
    shared = agent.select_shared(
        pricing_model="BlackScholesMerton",
        pricing_engine="fd_bs",
        symbol="SPY",
        option_type="Call",
        asof="2026-11-21",
        target_dte=30,
        moneyness="ATM",
        rate_tenor="10Y",
    )
    assert shared.symbol == "SPY"
    assert shared.option_type == "Call"
    assert shared.rate_tenor == "10Y"

    # Step 5: Select 2 volatility models compatible with BSM
    workflows = agent.select_varied(["flat_vol", "local_vol"])
    assert len(workflows) == 2
    assert workflows[0].volatility_model == "flat_vol"
    assert workflows[1].volatility_model == "local_vol"

    # Step 6: Execute in ORE-SWIG (offline-safe)
    report = agent.execute(allow_network=False, run_strike_sweep=True)
    assert report.contract.option_type == "Call"
    assert report.contract.exercise_style == "American"
    assert len(report.comparison.rows) == 2
    assert {r.model_id for r in report.comparison.rows} == {"FlatBlackScholesVol", "LocalVolSurface"}
    assert report.risk_free_rate == pytest.approx(0.0428, abs=1e-4)
    assert "DGS10" in report.rate_source
    assert len(report.strike_sweep) >= 5
    for row in report.comparison.rows:
        assert row.price > 0.0
        assert 0.0 < (row.delta or 0.0) < 1.0


def test_incompatibility_guard_blocks_heston_under_bsm() -> None:
    agent = ComparativeWorkflowAgent()
    agent.start("American Option", mode="Compare")
    agent.configure_workflows(num_workflows=2, varied_category="volatility_model")
    agent.select_shared(pricing_model="BlackScholesMerton")

    with pytest.raises(ValueError, match="not compatible"):
        agent.select_varied(["flat_vol", "heston"])
