"""Deterministic + LLM-fallback Recipe Binder and Compiler (FR-12 to FR-22)."""

from __future__ import annotations

import re
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from recipe_workbench.compiler.parser import ParsedRecipe, RecipeParagraph
from recipe_workbench.compiler.sidecar import SidecarData, hash_span, load_sidecar, record_binding
from recipe_workbench.engine.ore_models import (
    compare_pricing_engines,
    compare_volatility_models,
    price_single_in_process,
)
from recipe_workbench.engine.ore_xml_runner import run_ore_app_trade
from recipe_workbench.engine.schemas import (
    ComparisonResult,
    OptionContractSpec,
    PriceResult,
)
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.keyword_detector import (
    KeywordAnalysisResult,
    KeywordCategory,
    analyze_ore_keywords,
    detect_spans_in_text,
)
from recipe_workbench.graph.picker import RootPicker
from recipe_workbench.graph.schema import Axis, ScopeSpec, Verb
from recipe_workbench.graph.scope_walker import compute_scope
from recipe_workbench.llm.gemini_client import BindingProposalBatch, call_structured, has_gemini_key
from recipe_workbench.snapshots.store import LoadedSnapshot, fetch_or_load_snapshot


class BoundSpan(BaseModel):
    start: int
    end: int
    phrase: str
    target_id: str
    source: Literal[
        "sidecar",
        "scope_match",
        "literal",
        "definition",
        "llm_proposed",
        "result_ref",
    ]
    category: Optional[KeywordCategory] = None
    proposed: bool = False


class AmbiguityOption(BaseModel):
    id: str
    title: str
    description: str
    brings_into_scope: str


class AmbiguityPrompt(BaseModel):
    key: str = "american_put"
    matched_phrase: str
    heading: str
    lead: str
    options: List[AmbiguityOption]


class ParagraphAnnotation(BaseModel):
    paragraph_index: int
    line_number: int
    text: str
    spans: List[BoundSpan] = Field(default_factory=list)
    margin_ok: List[str] = Field(default_factory=list)
    provenance_notes: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    ambiguity: Optional[AmbiguityPrompt] = None
    inline_result_pill: Optional[str] = None
    comparison_result: Optional[ComparisonResult] = None
    price_result: Optional[PriceResult] = None


class CompiledRecipe(BaseModel):
    recipe: ParsedRecipe
    scope: ScopeSpec
    snapshot_id: str
    snapshot_summary: str
    halted_on_ambiguity: bool = False
    annotations: List[ParagraphAnnotation] = Field(default_factory=list)
    defined_contract: Optional[OptionContractSpec] = None
    working_model: Optional[str] = None
    comparison_result: Optional[ComparisonResult] = None
    price_result: Optional[PriceResult] = None
    llm_proposals: List[str] = Field(default_factory=list)
    keyword_analysis: Optional[KeywordAnalysisResult] = None


def _find_non_overlapping_spans(
    text: str, alias_map: List[Tuple[str, str, str]]
) -> List[BoundSpan]:
    """Match longest aliases first, word-boundary safe, preserving exact character spans."""
    occupied: List[Tuple[int, int]] = []
    spans: List[BoundSpan] = []

    # Sort longest phrase first so "SPY option chain" wins over "option chain"
    for phrase, target_id, source in sorted(alias_map, key=lambda x: len(x[0]), reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)
        for match in pattern.finditer(text):
            s, e = match.span()
            if any(not (e <= os_ or s >= oe) for os_, oe in occupied):
                continue
            occupied.append((s, e))
            spans.append(
                BoundSpan(
                    start=s,
                    end=e,
                    phrase=text[s:e],
                    target_id=target_id,
                    source=source,  # type: ignore[arg-type]
                    proposed=False,
                )
            )
    spans.sort(key=lambda sp: sp.start)
    return spans


def _build_ambiguity_prompt(matched_phrase: str) -> AmbiguityPrompt:
    return AmbiguityPrompt(
        key="american_put",
        matched_phrase=matched_phrase,
        heading=f"“{matched_phrase}” matches two things",
        lead="Pick one. The scope on the right will rebuild from your choice.",
        options=[
            AmbiguityOption(
                id="ore_instrument",
                title="ORE In-Process Instrument",
                description="ORE.VanillaOption with ORE.AmericanExercise. Price a single contract in-process; fastest for model comparison.",
                brings_into_scope="Brings in: ORE FD and binomial engines, 4 volatility models",
            ),
            AmbiguityOption(
                id="ore_trade",
                title="ORE Trade (OREApp XML)",
                description="ORE EquityOption with Style = American. Goes into portfolio.xml and ORE reports (NPV, cashflows).",
                brings_into_scope="Brings in: Envelope, pricingengine.xml, npv.csv, flows.csv",
            ),
        ],
    )


def _extract_contract_from_sentence(
    text: str, snapshot: LoadedSnapshot, default_style: str = "American"
) -> OptionContractSpec:
    lower = text.lower()
    dte_match = re.search(r"(\d+)\s*[- ]?day", lower)
    target_dte = int(dte_match.group(1)) if dte_match else 30

    moneyness = "ATM"
    for word in ("atm", "otm", "itm"):
        if re.search(rf"\b{word}\b", lower):
            moneyness = word.upper()
            break

    opt_type: Literal["Call", "Put"] = "Call" if re.search(r"\bcall\b", lower) else "Put"
    ex_style: Literal["American", "European"] = (
        "European" if re.search(r"\beuropean\b", lower) else default_style  # type: ignore[assignment]
    )

    row = snapshot.resolve_contract(target_dte=target_dte, moneyness=moneyness, option_type=opt_type)
    return OptionContractSpec(
        symbol=snapshot.manifest.symbol,
        contract_symbol=row.contract_symbol,
        option_type=row.option_type,
        exercise_style=ex_style,
        strike=row.strike,
        expiry=row.expiry,
        dte=row.dte,
        quantity=1.0,
        currency="USD",
    )


def _propose_llm_bindings(
    text: str,
    existing_spans: List[BoundSpan],
    scope: ScopeSpec,
) -> List[BoundSpan]:
    if not has_gemini_key():
        return []
    in_scope = [
        {"id": n.id, "label": n.label, "aliases": n.aliases}
        for n in scope.all_in_scope_nodes()
    ]
    already = [sp.phrase for sp in existing_spans]
    prompt = (
        f"Recipe sentence: {text!r}\n"
        f"Already bound phrases: {already}\n"
        f"Allowed scope nodes (ONLY pick from these IDs): {in_scope}\n"
        "Return only clear, un-bound phrases in the sentence that map to an allowed scope node ID."
    )
    batch = call_structured(prompt, BindingProposalBatch)
    if batch is None:
        return []

    allowed_ids = scope.in_scope_ids()
    occupied = [(sp.start, sp.end) for sp in existing_spans]
    proposed: List[BoundSpan] = []
    for prop in batch.proposals:
        if prop.target_node_id not in allowed_ids:
            continue
        idx = text.lower().find(prop.phrase.lower())
        if idx < 0:
            continue
        s, e = idx, idx + len(prop.phrase)
        if any(not (e <= os_ or s >= oe) for os_, oe in occupied):
            continue
        occupied.append((s, e))
        proposed.append(
            BoundSpan(
                start=s,
                end=e,
                phrase=text[s:e],
                target_id=prop.target_node_id,
                source="llm_proposed",
                proposed=True,
            )
        )
    return proposed


def _infer_span_category(target_id: str, graph: OREDomainGraph) -> KeywordCategory:
    if target_id.startswith(("literal:", "param:")):
        return "input_param"
    if target_id.startswith("contract:"):
        return "instrument"
    if target_id.startswith("model:"):
        return "model"
    node = graph.get_node(target_id)
    if node is None:
        return "input_param"
    if node.category.value == "root" or node.id == "OREInProcessInstrument":
        return "instrument"
    if node.category.value == "varies":
        return "engine" if node.axis == Axis.PRICING_ENGINE.value else "model"
    if node.category.value == "formula":
        return "calculator"
    if node.category.value == "sibling":
        return "adjacent_ore"
    if node.id in {"ORETradeXML", "PricingEngineRouter"}:
        return "engine"
    return "input_param"


def _merge_keyword_spans(
    text: str,
    existing_spans: List[BoundSpan],
    graph: OREDomainGraph,
) -> List[BoundSpan]:
    """Ensure every ORE framework keyword detected in text has a typed, separated span."""
    for sp in existing_spans:
        if sp.category is None:
            sp.category = _infer_span_category(sp.target_id, graph)

    occupied = [(sp.start, sp.end) for sp in existing_spans]
    merged = list(existing_spans)
    for kw in detect_spans_in_text(text, graph):
        if any(not (kw.end <= os_ or kw.start >= oe) for os_, oe in occupied):
            continue
        occupied.append((kw.start, kw.end))
        merged.append(
            BoundSpan(
                start=kw.start,
                end=kw.end,
                phrase=kw.phrase,
                target_id=kw.node_id,
                source="literal" if kw.category == "input_param" else "scope_match",
                category=kw.category,
                proposed=False,
            )
        )
    merged.sort(key=lambda sp: sp.start)
    return merged


def compile_recipe(
    recipe: ParsedRecipe,
    graph: OREDomainGraph,
    sidecar: Optional[SidecarData] = None,
    snapshots_dir: str = "snapshots",
    allow_network: bool = True,
    ore_run_dir: str = "ore_runs/latest",
    use_llm_fallback: bool = True,
) -> CompiledRecipe:
    picker = RootPicker(graph)
    sc = sidecar or load_sidecar(recipe.path)
    disambig = sc.disambiguations.get("american_put")

    full_text = "\n".join([recipe.title_line.lstrip("#").strip(), *(p.text for p in recipe.paragraphs)])
    keyword_analysis = analyze_ore_keywords(
        full_text,
        graph,
        selected_models=sc.selected_models or None,
        selected_calculators=sc.selected_calculators or None,
    )

    is_freeform = False
    try:
        root_id, verb, axis = picker.parse_title(recipe.title_line)
    except ValueError:
        is_freeform = True
        root_id = "EquityOption"
        verb = keyword_analysis.inferred_verb
        axis = keyword_analysis.inferred_axis
        if verb == Verb.EXPLAIN:
            verb = Verb.PRICE

    # Allow user-checked calculator in Notion to override the active verb/axis
    active_calcs = [c.id for c in keyword_analysis.recommended_calculators if c.selected]
    active_models = [m.id for m in keyword_analysis.recommended_models if m.selected]
    if sc.selected_calculators:
        if "compare_pricing_engines" in active_calcs:
            verb = Verb.COMPARE
            axis = Axis.PRICING_ENGINE.value
        elif "compare_vol_models" in active_calcs:
            verb = Verb.COMPARE
            axis = Axis.VOLATILITY_MODEL.value
        elif "price_ore_app" in active_calcs:
            verb = Verb.PRICE
            axis = None
            disambig = "ore_trade"
        elif "price_single" in active_calcs:
            verb = Verb.PRICE
            axis = None

    scope = compute_scope(
        graph=graph,
        root_id=root_id,
        verb=verb,
        axis=axis,
        title=recipe.title_line.lstrip("#").strip(),
        uses=recipe.uses,
        disambiguation=disambig,
    )

    snapshot = fetch_or_load_snapshot(
        symbol=recipe.symbol,
        asof=recipe.asof,
        snapshots_dir=snapshots_dir,
        allow_network=allow_network,
    )

    # Build deterministic alias table strictly from scope
    alias_table: List[Tuple[str, str, str]] = []
    for node in scope.all_in_scope_nodes():
        for al in [node.label, *node.aliases]:
            if al.strip():
                alias_table.append((al, node.id, "scope_match"))

    # Add literals from snapshot & recipe
    alias_table.extend(
        [
            (recipe.asof, "literal:asof", "literal"),
            (recipe.symbol, "literal:symbol", "literal"),
            ("30-day", "literal:tenor:30D", "literal"),
            ("ATM", "literal:moneyness:ATM", "literal"),
            ("OTM", "literal:moneyness:OTM", "literal"),
            ("ITM", "literal:moneyness:ITM", "literal"),
        ]
    )

    defined_names: Dict[str, str] = {}
    defined_contract: Optional[OptionContractSpec] = None
    halted = False
    comparison_res: Optional[ComparisonResult] = None
    price_res: Optional[PriceResult] = None
    working_model: Optional[str] = None
    annotations: List[ParagraphAnnotation] = []
    llm_proposals: List[str] = []

    suppress_assumptions = "conventions" in recipe.front_matter

    for para in recipe.paragraphs:
        if halted:
            annotations.append(
                ParagraphAnnotation(
                    paragraph_index=para.index,
                    line_number=para.line_number,
                    text=para.text,
                    provenance_notes=["Paused until ambiguity above is answered (FR-15)."],
                )
            )
            continue

        # Refresh alias table with any runtime definitions + sidecar bindings
        dynamic_aliases = list(alias_table)
        for def_phrase, target_id in defined_names.items():
            dynamic_aliases.append((def_phrase, target_id, "definition"))
        for b in sc.bindings.values():
            if b.target_id in scope.in_scope_ids() or b.target_id.startswith(("contract:", "model:", "literal:")):
                dynamic_aliases.append((b.phrase, b.target_id, "sidecar"))

        spans = _find_non_overlapping_spans(para.text, dynamic_aliases)
        spans = _merge_keyword_spans(para.text, spans, graph)
        if use_llm_fallback:
            extra_llm = _propose_llm_bindings(para.text, spans, scope)
            if extra_llm:
                for sp in extra_llm:
                    sp.category = _infer_span_category(sp.target_id, graph)
                spans.extend(extra_llm)
                spans.sort(key=lambda sp: sp.start)
                llm_proposals.extend(f"{sp.phrase} -> {sp.target_id}" for sp in extra_llm)

        ann = ParagraphAnnotation(
            paragraph_index=para.index,
            line_number=para.line_number,
            text=para.text,
            spans=spans,
        )

        lower = para.text.lower()

        # Check ambiguity on "American put" if not resolved in sidecar (FR-15),
        # unless user already explicitly chose a calculator/model in freeform mode
        amb_match = re.search(r"\bamerican\s+put\b", para.text, re.IGNORECASE)
        if amb_match and disambig is None and not is_freeform and not sc.selected_calculators:
            ann.ambiguity = _build_ambiguity_prompt(amb_match.group(0))
            halted = True
            annotations.append(ann)
            continue

        # Handle Define X as ... (FR-13, FR-14)
        def_match = re.match(r"^\s*define\s+(.+?)\s+as\s+(.+)$", para.text, re.IGNORECASE)
        if def_match:
            def_name = def_match.group(1).strip()
            defined_contract = _extract_contract_from_sentence(
                para.text, snapshot, default_style=recipe.conventions.exercise_style
            )
            defined_names[def_name] = f"contract:{defined_contract.contract_symbol}"
            # Re-run spans so `def_name` is highlighted as a definition binding
            dynamic_aliases.append((def_name, f"contract:{defined_contract.contract_symbol}", "definition"))
            ann.spans = _merge_keyword_spans(
                para.text, _find_non_overlapping_spans(para.text, dynamic_aliases), graph
            )
            ann.margin_ok.append(f"{defined_contract.exercise_style}Option ✓")
            ann.provenance_notes.append(
                f"{defined_contract.contract_symbol} · strike {defined_contract.strike:.2f} · exp {defined_contract.expiry}"
            )
            if not suppress_assumptions:
                ann.assumptions.append(
                    f"Assumed: exercise any business day, {recipe.conventions.calendar} calendar, {recipe.conventions.day_count}."
                )
        elif "option chain" in lower:
            ann.margin_ok.append(f"{len(ann.spans)} bindings ✓")
            ann.provenance_notes.append(
                f"chain: {snapshot.manifest.contract_count} contracts, {snapshot.manifest.expiry_count} expiries"
            )

        # Ensure a default contract if user writes "Price ..." directly without a separate Define sentence
        active_contract = defined_contract or _extract_contract_from_sentence(
            para.text if ("put" in lower or "call" in lower) else full_text,
            snapshot,
            default_style=recipe.conventions.exercise_style,
        )

        # Execute formulas depending on verb & bound formula spans
        bound_ids = {sp.target_id for sp in ann.spans}
        is_last_para = para.index == len(recipe.paragraphs) - 1

        if verb == Verb.COMPARE and (
            "formula_price" in bound_ids
            or "shown below" in lower
            or any(n.id in bound_ids for n in scope.varies)
            or (is_freeform and is_last_para and comparison_res is None)
        ):
            if axis == Axis.PRICING_ENGINE.value:
                comparison_res = compare_pricing_engines(
                    active_contract, snapshot, recipe.conventions, scope.graph_version
                )
            else:
                comparison_res = compare_volatility_models(
                    active_contract,
                    snapshot,
                    recipe.conventions,
                    scope.graph_version,
                    selected_model_keys=active_models or None,
                )
            ann.comparison_result = comparison_res
            ann.inline_result_pill = "shown below"
            ann.margin_ok.append(f"{len(comparison_res.rows)} models · ORE engines paired ✓")
            ann.provenance_notes.append(
                f"Snapshot {snapshot.manifest.snapshot_id} · ORE {comparison_res.provenance.ore_version}"
            )
        elif verb == Verb.PRICE and (
            "formula_price" in bound_ids
            or "formula_cashflows" in bound_ids
            or "price" in lower
            or (is_freeform and is_last_para and price_res is None)
        ):
            if disambig == "ore_trade" or "price_ore_app" in active_calcs:
                price_res = run_ore_app_trade(
                    active_contract,
                    snapshot,
                    recipe.conventions,
                    scope.graph_version,
                    run_dir=ore_run_dir,
                )
            else:
                chosen_model = active_models[0] if active_models else "local_vol"
                price_res = price_single_in_process(
                    active_contract,
                    snapshot,
                    recipe.conventions,
                    scope.graph_version,
                    model_key=chosen_model,
                )
            ann.price_result = price_res
            ann.inline_result_pill = f"NPV {price_res.npv:.2f} USD"
            ann.margin_ok.append(f"NPV {price_res.npv:.4f} ✓")
            ann.provenance_notes.append(
                f"{price_res.provenance.engine_name} · {snapshot.manifest.snapshot_id}"
            )
            if not suppress_assumptions:
                ann.assumptions.extend(price_res.assumptions[:2])

        # Handle "call the winner <name>" (FR-22)
        winner_match = re.search(r"call\s+the\s+winner\s+([a-zA-Z0-9_ -]+?)(?:[.,;]|$)", para.text, re.IGNORECASE)
        if winner_match and comparison_res is not None:
            winner_alias = winner_match.group(1).strip()
            working_model = comparison_res.winner_model_name
            defined_names[winner_alias] = f"model:{comparison_res.winner_model_id}"
            dynamic_aliases.append((winner_alias, f"model:{comparison_res.winner_model_id}", "result_ref"))
            ann.spans = _merge_keyword_spans(
                para.text, _find_non_overlapping_spans(para.text, dynamic_aliases), graph
            )
            ann.margin_ok.append(f"{winner_alias} → {working_model}")
            ann.provenance_notes.append("recomputes when the snapshot changes")

        if not ann.margin_ok and ann.spans:
            ann.margin_ok.append(f"{len(ann.spans)} bindings ✓")

        annotations.append(ann)

    return CompiledRecipe(
        recipe=recipe,
        scope=scope,
        snapshot_id=snapshot.manifest.snapshot_id,
        snapshot_summary=f"{snapshot.manifest.symbol} {snapshot.manifest.asof} 16:00 ({snapshot.manifest.contract_count} contracts)",
        halted_on_ambiguity=halted,
        annotations=annotations,
        defined_contract=defined_contract,
        working_model=working_model,
        comparison_result=comparison_res,
        price_result=price_res,
        llm_proposals=llm_proposals,
        keyword_analysis=keyword_analysis,
    )
