"""Interactive backend agent for stepwise American Option comparative workflow construction."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from recipe_workbench.engine.ore_models import (
    compare_pricing_engines,
    compare_volatility_models,
    price_single_in_process,
)
from recipe_workbench.engine.schemas import (
    ComparisonResult,
    ConventionsSpec,
    OptionContractSpec,
)
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.picker import RootPicker
from recipe_workbench.graph.schema import Axis, Verb
from recipe_workbench.snapshots.store import (
    LoadedSnapshot,
    fetch_or_load_snapshot,
    fetch_treasury_rate,
    load_snapshot,
)


class DependencyCategoryInfo(BaseModel):
    category: str
    label: str
    base_class: str
    description: str
    available_count: int
    can_vary_across_workflows: bool = True


class DependencyDiscoveryResult(BaseModel):
    query: str
    root_id: str
    root_label: str
    mode: str
    graph_version: str
    dependency_categories: List[DependencyCategoryInfo]
    next_prompt: str


class DependencyOption(BaseModel):
    key: str
    node_id: str
    label: str
    ore_class: str
    description: str
    compatible_with: List[str] = Field(default_factory=list)


class DependencyOptionsResult(BaseModel):
    root_id: str
    category: str
    base_class: str
    options: List[DependencyOption]
    next_prompt: str


class WorkflowAllocationPlan(BaseModel):
    num_workflows: int
    varied_category: str
    shared_categories: List[str]
    selection_rule: str
    slot_counts: Dict[str, int]


class SharedDependencies(BaseModel):
    pricing_model: str = "BlackScholesMerton"
    pricing_engine: str = "fd_bs"
    symbol: str = "SPY"
    option_type: Literal["Call", "Put"] = "Call"
    exercise_style: Literal["American", "European"] = "American"
    asof: str = Field(default_factory=lambda: date.today().isoformat())
    target_dte: int = 30
    moneyness: Literal["ATM", "OTM", "ITM"] = "ATM"
    rate_tenor: str = "10Y"
    rate_override: Optional[float] = None
    quantity: float = 1.0


class WorkflowSpec(BaseModel):
    workflow_index: int
    name: str
    pricing_model: str
    volatility_model: str
    pricing_engine: str
    symbol: str
    option_type: Literal["Call", "Put"]
    exercise_style: Literal["American", "European"]
    asof: str
    rate_tenor: str


class StrikeSweepRow(BaseModel):
    strike: float
    moneyness_pct: float
    market_mid: float
    market_iv: float
    workflow_prices: Dict[str, float]
    workflow_deltas: Dict[str, Optional[float]]
    price_diff: float


class WorkflowExecutionReport(BaseModel):
    root_id: str
    mode: str
    num_workflows: int
    varied_category: str
    shared: SharedDependencies
    workflows: List[WorkflowSpec]
    snapshot_id: str
    snapshot_source: str
    asof_requested: str
    asof_effective: str
    spot: float
    risk_free_rate: float
    rate_source: str
    dividend_yield: float
    contract: OptionContractSpec
    comparison: ComparisonResult
    strike_sweep: List[StrikeSweepRow] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


_CATEGORY_ALIASES: Dict[str, str] = {
    "model": "pricing_model",
    "models": "pricing_model",
    "pricing_model": "pricing_model",
    "pricing_models": "pricing_model",
    "process": "pricing_model",
    "vol": "volatility_model",
    "volatility": "volatility_model",
    "volatility_model": "volatility_model",
    "volatility_models": "volatility_model",
    "calculator": "pricing_engine",
    "calculators": "pricing_engine",
    "engine": "pricing_engine",
    "engines": "pricing_engine",
    "pricing_engine": "pricing_engine",
    "pricing_engines": "pricing_engine",
    "market": "market_data",
    "market_data": "market_data",
    "data": "market_data",
    "inputs": "market_data",
}

_PRICING_MODELS: List[DependencyOption] = [
    DependencyOption(
        key="BlackScholesMerton",
        node_id="PricingEngineRouter",
        label="Black-Scholes-Merton",
        ore_class="ORE.BlackScholesMertonProcess",
        description=(
            "Generalized Black-Scholes-Merton stochastic process with continuous dividend yield "
            "and pluggable volatility term structure or Dupire local vol surface."
        ),
        compatible_with=["flat_vol", "term_structure", "local_vol"],
    ),
    DependencyOption(
        key="Heston",
        node_id="HestonStochasticVol",
        label="Heston Stochastic Volatility",
        ore_class="ORE.HestonProcess / ORE.HestonModel",
        description=(
            "Two-factor stochastic variance process (v0, kappa, theta, sigma, rho) "
            "priced via 2D finite-difference PDE (ORE.FdHestonVanillaEngine)."
        ),
        compatible_with=["heston"],
    ),
]


class ComparativeWorkflowAgent:
    """Stepwise backend agent that walks ORE downward dependencies and builds N comparative workflows."""

    def __init__(self, graph: Optional[OREDomainGraph] = None) -> None:
        self.graph = graph or OREDomainGraph()
        self.picker = RootPicker(self.graph)
        self.root_id: Optional[str] = None
        self.root_label: Optional[str] = None
        self.mode: str = Verb.COMPARE.value
        self.num_workflows: int = 2
        self.varied_category: str = Axis.VOLATILITY_MODEL.value
        self.shared: SharedDependencies = SharedDependencies()
        self.varied_choices: List[str] = []
        self.workflows: List[WorkflowSpec] = []

    def start(self, product_query: str = "American Option", mode: str = "Compare") -> DependencyDiscoveryResult:
        """Step 1: Resolve product query against ORE graph and list downward dependency base classes."""
        hits = self.picker.filter_roots(product_query)
        if not hits:
            raise ValueError(
                f"No ORE root matched {product_query!r}. Available roots: "
                f"{[h.name for h in self.picker.filter_roots('')]}"
            )
        root = self.graph.get_node(hits[0].id)
        assert root is not None
        self.root_id = root.id
        self.root_label = root.label
        self.mode = mode

        vol_nodes = [
            e.target
            for e in self.graph.edges
            if e.source == root.id and e.relation == "alternatives" and e.axis == Axis.VOLATILITY_MODEL.value
        ]
        eng_nodes = [
            e.target
            for e in self.graph.edges
            if e.source == root.id and e.relation == "alternatives" and e.axis == Axis.PRICING_ENGINE.value
        ]
        data_nodes = [
            e.target
            for e in self.graph.edges
            if e.source == root.id
            and e.relation == "needs"
            and (n := self.graph.get_node(e.target)) is not None
            and n.is_data_leaf
        ]

        categories = [
            DependencyCategoryInfo(
                category="pricing_model",
                label="Base Pricing Model / Process",
                base_class="ORE.StochasticProcess (BlackScholesMertonProcess / HestonProcess)",
                description="Underlying equity dynamics model for American option valuation.",
                available_count=len(_PRICING_MODELS),
                can_vary_across_workflows=True,
            ),
            DependencyCategoryInfo(
                category="volatility_model",
                label="Volatility Model",
                base_class="ORE.BlackVolTermStructure / ORE.HestonModel",
                description="Volatility representation (flat, term structure, Dupire local vol, or stochastic vol).",
                available_count=len(vol_nodes),
                can_vary_across_workflows=True,
            ),
            DependencyCategoryInfo(
                category="pricing_engine",
                label="Pricing Engine / Calculator",
                base_class="ORE.PricingEngine (EquityOptionEngineBuilder)",
                description="Numerical American exercise solver (Finite Difference PDE, Binomial CRR, BAW, Bjerksund-Stensland).",
                available_count=len(eng_nodes),
                can_vary_across_workflows=True,
            ),
            DependencyCategoryInfo(
                category="market_data",
                label="Market Data & Yield Curve Inputs",
                base_class="ORE.Market (QuoteHandle, YieldTermStructureHandle)",
                description="Underlying spot, risk-free curve (e.g. 10Y Treasury), dividend yield, and option chain.",
                available_count=len(data_nodes),
                can_vary_across_workflows=False,
            ),
            DependencyCategoryInfo(
                category="contract_spec",
                label="Contract Specification",
                base_class="ORE.VanillaOption / ORE.AmericanExercise",
                description="Underlying ticker (e.g. SPY), Call/Put, valuation date, strike/moneyness, and expiry.",
                available_count=1,
                can_vary_across_workflows=False,
            ),
        ]

        return DependencyDiscoveryResult(
            query=product_query,
            root_id=root.id,
            root_label=root.label,
            mode=self.mode,
            graph_version=self.graph.graph_version,
            dependency_categories=categories,
            next_prompt=(
                "Pass a dependency category to inspect available implementations "
                "(e.g. 'models', 'volatility_model', 'calculators', or 'market_data')."
            ),
        )

    def inspect_dependency(self, category: str) -> DependencyOptionsResult:
        """Step 2: List available concrete implementations for a downward dependency category."""
        if not self.root_id:
            self.start()
        assert self.root_id is not None

        canonical = _CATEGORY_ALIASES.get(category.strip().lower(), category.strip().lower())

        if canonical == "pricing_model":
            return DependencyOptionsResult(
                root_id=self.root_id,
                category="pricing_model",
                base_class="ORE.StochasticProcess",
                options=list(_PRICING_MODELS),
                next_prompt=(
                    "Select a base pricing model (e.g. 'BlackScholesMerton') and tell me how many "
                    "workflows you want to compare (e.g. 2) plus which axis to vary (e.g. 'volatility_model')."
                ),
            )

        if canonical == "volatility_model":
            options: List[DependencyOption] = []
            for edge in self.graph.edges:
                if edge.source == self.root_id and edge.relation == "alternatives" and edge.axis == Axis.VOLATILITY_MODEL.value:
                    node = self.graph.get_node(edge.target)
                    if node is None:
                        continue
                    compat = ["Heston"] if node.engine_key == "heston" else ["BlackScholesMerton"]
                    options.append(
                        DependencyOption(
                            key=node.engine_key or node.id,
                            node_id=node.id,
                            label=node.label,
                            ore_class=node.code_ref or "ORE.BlackVolTermStructure",
                            description=node.description,
                            compatible_with=compat,
                        )
                    )
            return DependencyOptionsResult(
                root_id=self.root_id,
                category="volatility_model",
                base_class="ORE.BlackVolTermStructure / ORE.HestonModel",
                options=options,
                next_prompt="Pick N volatility models (one per workflow) to compare side-by-side.",
            )

        if canonical == "pricing_engine":
            options = []
            for edge in self.graph.edges:
                if edge.source == self.root_id and edge.relation == "alternatives" and edge.axis == Axis.PRICING_ENGINE.value:
                    node = self.graph.get_node(edge.target)
                    if node is None:
                        continue
                    options.append(
                        DependencyOption(
                            key=node.engine_key or node.id,
                            node_id=node.id,
                            label=node.label,
                            ore_class=node.code_ref or "ORE.PricingEngine",
                            description=node.description,
                            compatible_with=["BlackScholesMerton"],
                        )
                    )
            return DependencyOptionsResult(
                root_id=self.root_id,
                category="pricing_engine",
                base_class="ORE.PricingEngine",
                options=options,
                next_prompt="Pick a shared calculator (or vary calculators across workflows).",
            )

        if canonical == "market_data":
            options = []
            for edge in self.graph.edges:
                if edge.source == self.root_id and edge.relation == "needs":
                    node = self.graph.get_node(edge.target)
                    if node is not None and node.is_data_leaf:
                        options.append(
                            DependencyOption(
                                key=node.id,
                                node_id=node.id,
                                label=node.label,
                                ore_class=node.xml_tag or node.code_ref or "ORE.Market",
                                description=node.description,
                                compatible_with=["BlackScholesMerton", "Heston"],
                            )
                        )
            return DependencyOptionsResult(
                root_id=self.root_id,
                category="market_data",
                base_class="ORE.Market",
                options=options,
                next_prompt="Configure market data inputs (e.g. symbol='SPY', rate_tenor='10Y').",
            )

        raise ValueError(
            f"Unknown dependency category {category!r}. "
            "Valid categories: pricing_model, volatility_model, pricing_engine, market_data."
        )

    def configure_workflows(
        self,
        num_workflows: int = 2,
        varied_category: str = "volatility_model",
    ) -> WorkflowAllocationPlan:
        """Step 3: Declare N workflows and partition dependencies into Shared (1) vs. Varied (N)."""
        if num_workflows < 2:
            raise ValueError("Comparative analysis requires at least 2 workflows.")
        canonical = _CATEGORY_ALIASES.get(varied_category.strip().lower(), varied_category.strip().lower())
        if canonical not in {"volatility_model", "pricing_engine"}:
            raise ValueError(
                f"Varied category {varied_category!r} not supported for ORE comparison. "
                "Use 'volatility_model' or 'pricing_engine'."
            )
        self.num_workflows = num_workflows
        self.varied_category = canonical

        all_slots = ["pricing_model", "volatility_model", "pricing_engine", "market_data", "contract_spec"]
        shared_slots = [s for s in all_slots if s != canonical]
        slot_counts = {s: 1 for s in shared_slots}
        slot_counts[canonical] = num_workflows

        return WorkflowAllocationPlan(
            num_workflows=num_workflows,
            varied_category=canonical,
            shared_categories=shared_slots,
            selection_rule=(
                f"Select 1 value for each shared dependency ({', '.join(shared_slots)}) "
                f"and {num_workflows} values for '{canonical}' (1 per workflow)."
            ),
            slot_counts=slot_counts,
        )

    def select_shared(self, **kwargs: Any) -> SharedDependencies:
        """Step 4: Set shared dependencies (base model, contract, rate tenor, calculator)."""
        self.shared = self.shared.model_copy(update=kwargs)
        return self.shared

    def select_varied(self, choices: List[str]) -> List[WorkflowSpec]:
        """Step 5: Assign the N varied choices (e.g. 2 volatility models) to Workflow 1..N."""
        if len(choices) != self.num_workflows:
            raise ValueError(
                f"Expected {self.num_workflows} choices for '{self.varied_category}', got {len(choices)}: {choices}"
            )

        valid_opts = {opt.key: opt for opt in self.inspect_dependency(self.varied_category).options}
        for c in choices:
            if c not in valid_opts:
                raise ValueError(f"Unknown {self.varied_category} choice {c!r}. Valid: {list(valid_opts)}")
            opt = valid_opts[c]
            if opt.compatible_with and self.shared.pricing_model not in opt.compatible_with:
                raise ValueError(
                    f"Choice {c!r} is not compatible with shared pricing_model={self.shared.pricing_model!r} "
                    f"(compatible with: {opt.compatible_with})."
                )

        self.varied_choices = list(choices)
        self.workflows = []
        for idx, choice in enumerate(choices, start=1):
            vol_model = choice if self.varied_category == "volatility_model" else "flat_vol"
            engine = choice if self.varied_category == "pricing_engine" else self.shared.pricing_engine
            label = valid_opts[choice].label
            self.workflows.append(
                WorkflowSpec(
                    workflow_index=idx,
                    name=f"Workflow {idx}: {self.shared.pricing_model} + {label}",
                    pricing_model=self.shared.pricing_model,
                    volatility_model=vol_model,
                    pricing_engine=engine,
                    symbol=self.shared.symbol,
                    option_type=self.shared.option_type,
                    exercise_style=self.shared.exercise_style,
                    asof=self.shared.asof,
                    rate_tenor=self.shared.rate_tenor,
                )
            )
        return self.workflows

    def _load_market_snapshot(self, allow_network: bool) -> tuple[LoadedSnapshot, List[str]]:
        notes: List[str] = []
        try:
            snap = fetch_or_load_snapshot(
                self.shared.symbol,
                self.shared.asof,
                allow_network=allow_network,
            )
            notes.append(f"Loaded live/cached market snapshot {snap.manifest.snapshot_id} for {self.shared.asof}.")
        except Exception as exc:
            snap = load_snapshot("SPY", "2026-09-10")
            notes.append(
                f"Live market snapshot for {self.shared.symbol} on {self.shared.asof} unavailable "
                f"({type(exc).__name__}); using immutable gold snapshot {snap.manifest.snapshot_id}."
            )

        if self.shared.rate_override is not None:
            rate_val = float(self.shared.rate_override)
            rate_source = f"manual override ({rate_val:.2%})"
        else:
            rate_val, rate_source = fetch_treasury_rate(
                tenor=self.shared.rate_tenor,
                allow_network=allow_network,
            )

        snap_with_rate = snap.with_rate_override(rate_val, source_note=rate_source)
        notes.append(f"Applied {self.shared.rate_tenor} Treasury rate: {rate_val:.4%} ({rate_source}).")
        return snap_with_rate, notes

    def execute(
        self,
        allow_network: bool = True,
        run_strike_sweep: bool = True,
    ) -> WorkflowExecutionReport:
        """Step 6: Execute the N configured workflows in ORE-SWIG and return structured comparison report."""
        if not self.root_id:
            self.start()
        if not self.workflows:
            raise RuntimeError("No workflows configured. Call select_varied([...]) before execute().")

        snapshot, notes = self._load_market_snapshot(allow_network=allow_network)
        conventions = ConventionsSpec(exercise_style=self.shared.exercise_style)

        row = snapshot.resolve_contract(
            target_dte=self.shared.target_dte,
            moneyness=self.shared.moneyness,
            option_type=self.shared.option_type,
        )
        contract = OptionContractSpec(
            symbol=self.shared.symbol,
            contract_symbol=row.contract_symbol,
            option_type=row.option_type,
            exercise_style=self.shared.exercise_style,
            strike=row.strike,
            expiry=row.expiry,
            dte=row.dte,
            quantity=self.shared.quantity,
        )

        if self.varied_category == "volatility_model":
            comparison = compare_volatility_models(
                contract=contract,
                snapshot=snapshot,
                conventions=conventions,
                graph_version=self.graph.graph_version,
                selected_model_keys=self.varied_choices,
            )
        else:
            comparison = compare_pricing_engines(
                contract=contract,
                snapshot=snapshot,
                conventions=conventions,
                graph_version=self.graph.graph_version,
                selected_engine_keys=self.varied_choices,
            )

        strike_sweep: List[StrikeSweepRow] = []
        if run_strike_sweep:
            same_exp = sorted(
                [c for c in snapshot.chain if c.expiry == contract.expiry and c.option_type == self.shared.option_type],
                key=lambda c: c.strike,
            )
            # Pick up to 7 strikes centered around the resolved ATM strike
            atm_idx = min(range(len(same_exp)), key=lambda i: abs(same_exp[i].strike - contract.strike))
            start_i = max(0, atm_idx - 3)
            end_i = min(len(same_exp), start_i + 7)
            selected_rows = same_exp[start_i:end_i]

            for srow in selected_rows:
                scontract = OptionContractSpec(
                    symbol=self.shared.symbol,
                    contract_symbol=srow.contract_symbol,
                    option_type=srow.option_type,
                    exercise_style=self.shared.exercise_style,
                    strike=srow.strike,
                    expiry=srow.expiry,
                    dte=srow.dte,
                    quantity=self.shared.quantity,
                )
                w_prices: Dict[str, float] = {}
                w_deltas: Dict[str, Optional[float]] = {}
                for choice in self.varied_choices:
                    res = price_single_in_process(
                        contract=scontract,
                        snapshot=snapshot,
                        conventions=conventions,
                        graph_version=self.graph.graph_version,
                        model_key=choice,
                    )
                    w_prices[choice] = res.npv
                    w_deltas[choice] = res.delta
                p_vals = list(w_prices.values())
                diff = round(p_vals[1] - p_vals[0], 4) if len(p_vals) >= 2 else 0.0
                strike_sweep.append(
                    StrikeSweepRow(
                        strike=srow.strike,
                        moneyness_pct=round((srow.strike / snapshot.manifest.spot - 1.0) * 100.0, 2),
                        market_mid=srow.mid,
                        market_iv=round(srow.implied_vol * 100.0, 2),
                        workflow_prices=w_prices,
                        workflow_deltas=w_deltas,
                        price_diff=diff,
                    )
                )

        # Extract rate source label cleanly from snapshot.manifest.source
        rate_src = snapshot.manifest.source.split("rate=")[-1] if "rate=" in snapshot.manifest.source else self.shared.rate_tenor

        return WorkflowExecutionReport(
            root_id=self.root_id or "EquityOption",
            mode=self.mode,
            num_workflows=self.num_workflows,
            varied_category=self.varied_category,
            shared=self.shared,
            workflows=self.workflows,
            snapshot_id=snapshot.manifest.snapshot_id,
            snapshot_source=snapshot.manifest.source,
            asof_requested=self.shared.asof,
            asof_effective=snapshot.manifest.asof,
            spot=snapshot.manifest.spot,
            risk_free_rate=snapshot.manifest.risk_free_rate,
            rate_source=rate_src,
            dividend_yield=snapshot.manifest.dividend_yield,
            contract=contract,
            comparison=comparison,
            strike_sweep=strike_sweep,
            notes=notes,
        )
