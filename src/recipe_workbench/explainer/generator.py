"""Generates 5-layer ORE Explainers grounded in scope.json and Graphify graph paths (FR-26..FR-28)."""

from __future__ import annotations

import json
from typing import List, Optional

from pydantic import BaseModel, Field

from recipe_workbench.engine.ore_models import compare_volatility_models, price_single_in_process
from recipe_workbench.engine.schemas import ConventionsSpec, OptionContractSpec
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.schema import GraphNode, ScopeSpec
from recipe_workbench.llm.gemini_client import ExplainerDraft, call_structured, has_gemini_key
from recipe_workbench.snapshots.store import load_snapshot


class GroundedSentence(BaseModel):
    text: str
    cited_node_ids: List[str] = Field(default_factory=list)
    grounded: bool = True
    graph_paths: List[List[str]] = Field(default_factory=list)


class ObjectDrilldownEntry(BaseModel):
    id: str
    label: str
    layer: str
    code_ref: str
    xml_tag: str
    description: str


class ExplainerDocument(BaseModel):
    title: str
    scope_slug: str
    graph_version: str
    human_curated_editable: bool = True
    layer_1_business: str
    layer_2_quant: List[GroundedSentence]
    layer_3_architecture_mermaid: str
    layer_3_architecture_notes: List[GroundedSentence]
    layer_4_objects: List[ObjectDrilldownEntry]
    layer_5_code: str
    layer_5_verified_output: str
    ungrounded_count: int = 0
    summary_line: str = ""


def verify_sentence_grounding(
    sentence_text: str,
    cited_node_ids: List[str],
    scope: ScopeSpec,
    graph: OREDomainGraph,
) -> GroundedSentence:
    valid_scope_ids = scope.in_scope_ids() | {scope.root_id}
    verified_ids: List[str] = []
    paths: List[List[str]] = []

    for nid in cited_node_ids:
        if nid in valid_scope_ids and graph.get_node(nid) is not None:
            if nid == scope.root_id:
                verified_ids.append(nid)
                paths.append([scope.root_id])
            else:
                p = graph.find_path(scope.root_id, nid)
                if p:
                    verified_ids.append(nid)
                    paths.append(p)

    is_grounded = len(verified_ids) > 0
    display_text = sentence_text if is_grounded else f"⚠️ [Ungrounded] {sentence_text}"
    return GroundedSentence(
        text=display_text,
        cited_node_ids=verified_ids,
        grounded=is_grounded,
        graph_paths=paths,
    )


def _build_mermaid(scope: ScopeSpec) -> str:
    lines = ["flowchart LR"]
    lines.append(f'  {scope.root_id}["{scope.root_label} ({scope.root_id})"]')
    for n in scope.needs:
        lines.append(f'  {scope.root_id} -->|needs| {n.id}["{n.label}"]')
    for v in scope.varies:
        lines.append(f'  {scope.root_id} -->|varies: {scope.axis}| {v.id}["{v.label}"]')
    for fnode in scope.formulas:
        lines.append(f'  {scope.root_id} -->|produces| {fnode.id}["{fnode.label}"]')
    return "\n".join(lines)


def _worked_example_code_and_output(asof: str = "2026-09-10") -> tuple[str, str]:
    snap = load_snapshot("SPY", asof)
    c_row = snap.resolve_contract(target_dte=30, moneyness="ATM", option_type="Put")
    contract = OptionContractSpec(
        symbol="SPY",
        contract_symbol=c_row.contract_symbol,
        option_type=c_row.option_type,
        exercise_style="American",
        strike=c_row.strike,
        expiry=c_row.expiry,
        dte=c_row.dte,
    )
    res = price_single_in_process(contract, snap, ConventionsSpec(), "worked-example")
    cmp_res = compare_volatility_models(contract, snap, ConventionsSpec(), "worked-example")

    code = f"""import ORE

# 1. Evaluation date & market handles (SPY {asof} snapshot)
asof = ORE.Date(10, 9, 2026)
ORE.Settings.instance().evaluationDate = asof
cal, dc = ORE.UnitedStates(ORE.UnitedStates.NYSE), ORE.Actual365Fixed()
spot = ORE.QuoteHandle(ORE.SimpleQuote({snap.manifest.spot:.2f}))
q_ts = ORE.YieldTermStructureHandle(ORE.FlatForward(asof, {snap.manifest.dividend_yield:.4f}, dc))
r_ts = ORE.YieldTermStructureHandle(ORE.FlatForward(asof, {snap.manifest.risk_free_rate:.4f}, dc))
vol_ts = ORE.BlackVolTermStructureHandle(ORE.BlackConstantVol(asof, cal, 0.188, dc))

# 2. Build ORE American put & price via ORE.FdBlackScholesVanillaEngine
process = ORE.BlackScholesMertonProcess(spot, q_ts, r_ts, vol_ts)
payoff = ORE.PlainVanillaPayoff(ORE.Option.Put, {contract.strike:.2f})
exercise = ORE.AmericanExercise(asof, ORE.Date(9, 10, 2026))
option = ORE.VanillaOption(payoff, exercise)
option.setPricingEngine(ORE.FdBlackScholesVanillaEngine(process, 100, 100))
print(f"ORE American Put NPV={{option.NPV():.4f}}, Delta={{option.delta():.4f}}")
"""

    rows_summary = " | ".join(
        f"#{r.rank} {r.model_name}: ${r.price:.4f} (RMSE {r.chain_rmse:.4f})"
        for r in sorted(cmp_res.rows, key=lambda x: x.rank)
    )
    verified = (
        f"Contract {contract.contract_symbol} (Strike {contract.strike:.2f}, Exp {contract.expiry}): "
        f"Local-Vol FD NPV = ${res.npv:.4f} (Delta {res.delta:.4f}) | "
        f"4-Model ORE Rank: {rows_summary}"
    )
    return code, verified


def generate_explainer(
    scope: ScopeSpec,
    graph: OREDomainGraph,
    asof: str = "2026-09-10",
    extra_ungrounded_test_sentences: Optional[List[str]] = None,
) -> ExplainerDocument:
    """Generate the 5-layer Explainer grounded strictly in `scope` and Graphify nodes."""
    allowed_context = [
        {
            "id": n.id,
            "label": n.label,
            "layer": n.layer,
            "code_ref": n.code_ref,
            "description": n.description,
        }
        for n in scope.all_in_scope_nodes()
    ]

    draft: Optional[ExplainerDraft] = None
    if has_gemini_key():
        prompt = (
            f"Create a 5-layer technical explainer for ORE scope '{scope.title}' ({scope.root_id}).\n"
            f"Allowed scope nodes (ONLY cite IDs from this list or '{scope.root_id}'): {json.dumps(allowed_context)}\n"
            "Requirements:\n"
            "- business_intuition: 2-3 clear sentences for a PM/trader.\n"
            "- technical_intuition: 3 concise sentences citing node_ids.\n"
            "- architecture_map: 3 concise sentences tracing ORE Trade/Instrument -> EngineBuilder -> Market Data citing node_ids.\n"
            "- object_drilldown: 2 concise sentences summarizing key classes citing node_ids."
        )
        draft = call_structured(
            prompt,
            ExplainerDraft,
            system_instruction="You are a quantitative developer writing ORE documentation grounded strictly in provided graph node IDs.",
        )

    if draft is not None:
        l1 = draft.business_intuition
        raw_l2 = [(s.text, s.node_ids) for s in draft.technical_intuition]
        raw_l3 = [(s.text, s.node_ids) for s in draft.architecture_map]
    else:
        l1 = (
            "An American equity put gives the holder the right to sell SPY at the strike price on any NYSE business day "
            "before expiry. Because early exercise can capture interest on the strike when deep in-the-money, pricing "
            "requires backward-induction PDEs or lattice trees rather than a closed-form European Black-Scholes formula."
        )
        raw_l2 = [
            (
                "American option pricing solves the Black-Scholes-Merton or Heston PDE backwards from expiry, enforcing the free-boundary condition V(S, t) >= max(K - S, 0) at every time step.",
                ["EquityOption", "PricingEngineRouter"],
            ),
            (
                "Market inputs enter through the EquitySpot quote, RiskFreeCurve discount term structure, and DividendYield carry curve.",
                ["EquitySpot", "RiskFreeCurve", "DividendYield"],
            ),
            (
                "Evaluating across the OptionChain calibrates volatility models and measures cross-strike pricing error.",
                ["OptionChain", *(n.id for n in scope.varies[:2])],
            ),
        ]
        raw_l3 = [
            (
                "ORE parses EquityOption trades in ored/portfolio/equityoption.hpp and routes American exercise through EquityOptionEngineBuilder.",
                ["EquityOption", "PricingEngineRouter"],
            ),
            (
                "Market data leaves resolve lazily via ored/marketdata/market.hpp into Spot, DiscountCurve, DividendYield, and OptionChain handles.",
                ["EquitySpot", "RiskFreeCurve", "DividendYield", "OptionChain"],
            ),
            (
                "Execution supports both OREApp XML reports (npv.csv, flows.csv) and in-process ORE-SWIG finite-difference engines.",
                ["EquityOption", "PricingEngineRouter"],
            ),
        ]

    if extra_ungrounded_test_sentences:
        for txt in extra_ungrounded_test_sentences:
            raw_l2.append((txt, []))

    l2_verified = [verify_sentence_grounding(txt, cids, scope, graph) for txt, cids in raw_l2]
    l3_verified = [verify_sentence_grounding(txt, cids, scope, graph) for txt, cids in raw_l3]

    l4_objects: List[ObjectDrilldownEntry] = []
    root_node = graph.get_node(scope.root_id)
    nodes_for_l4: List[GraphNode] = ([root_node] if root_node else []) + scope.all_in_scope_nodes()
    for node in nodes_for_l4:
        l4_objects.append(
            ObjectDrilldownEntry(
                id=node.id,
                label=node.label,
                layer=node.layer,
                code_ref=node.code_ref or "ore_corpus/",
                xml_tag=node.xml_tag or "N/A",
                description=node.description,
            )
        )

    code, verified_out = _worked_example_code_and_output(asof=asof)
    ungrounded_count = sum(1 for s in [*l2_verified, *l3_verified] if not s.grounded)
    total_sentences = len(l2_verified) + len(l3_verified)
    summary_line = (
        f"Grounded {total_sentences - ungrounded_count}/{total_sentences} technical/architecture sentences "
        f"against graph {scope.graph_version}; {ungrounded_count} ungrounded sentence(s) flagged."
    )

    return ExplainerDocument(
        title=f"Explainer: {scope.title}",
        scope_slug=scope.slug,
        graph_version=scope.graph_version,
        human_curated_editable=True,
        layer_1_business=l1,
        layer_2_quant=l2_verified,
        layer_3_architecture_mermaid=_build_mermaid(scope),
        layer_3_architecture_notes=l3_verified,
        layer_4_objects=l4_objects,
        layer_5_code=code,
        layer_5_verified_output=verified_out,
        ungrounded_count=ungrounded_count,
        summary_line=summary_line,
    )


def diff_explainers(old: ExplainerDocument, new: ExplainerDocument) -> List[str]:
    """Summarize structural differences when regenerating after a graph change (FR-28)."""
    diffs: List[str] = []
    if old.graph_version != new.graph_version:
        diffs.append(f"graph_version: {old.graph_version} -> {new.graph_version}")
    old_ids = {o.id for o in old.layer_4_objects}
    new_ids = {o.id for o in new.layer_4_objects}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    if added:
        diffs.append(f"added_nodes: {added}")
    if removed:
        diffs.append(f"removed_nodes: {removed}")
    if old.ungrounded_count != new.ungrounded_count:
        diffs.append(f"ungrounded_count: {old.ungrounded_count} -> {new.ungrounded_count}")
    return diffs or ["no structural changes"]
