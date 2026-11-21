"""ORE Framework Keyword Detector & Model/Calculator Recommender.

Scans arbitrary Notion prose or recipe text for ORE framework keywords
(instruments, market inputs, volatility models, pricing engines, calculators,
and adjacent ORE modules), separates every detected input keyword into
typed spans, and recommends appropriate ORE models and calculators.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.schema import Axis, NodeCategory, Verb

KeywordCategory = Literal[
    "instrument",
    "input_param",
    "model",
    "engine",
    "calculator",
    "adjacent_ore",
]

CATEGORY_META: Dict[KeywordCategory, Tuple[str, str, str]] = {
    # category -> (display_label, emoji, notion_color)
    "instrument": ("Instrument", "🟦", "blue"),
    "input_param": ("Input Param", "🟧", "orange"),
    "model": ("ORE Model", "🟪", "purple"),
    "engine": ("ORE Engine", "🩷", "pink"),
    "calculator": ("Calculator", "🟩", "green"),
    "adjacent_ore": ("Adjacent ORE", "🟥", "red"),
}


class DetectedKeyword(BaseModel):
    phrase: str
    canonical: str
    category: KeywordCategory
    ore_ref: str
    node_id: str
    start: int = 0
    end: int = 0
    resolved_value: Optional[str] = None


class ModelRecommendation(BaseModel):
    id: str
    name: str
    ore_class: str
    reason: str
    matched_explicitly: bool = False
    selected: bool = False


class CalculatorRecommendation(BaseModel):
    id: str
    name: str
    ore_engine: str
    outputs: str
    reason: str
    selected: bool = False


class KeywordAnalysisResult(BaseModel):
    keywords: List[DetectedKeyword] = Field(default_factory=list)
    by_category: Dict[str, List[DetectedKeyword]] = Field(default_factory=dict)
    recommended_models: List[ModelRecommendation] = Field(default_factory=list)
    recommended_calculators: List[CalculatorRecommendation] = Field(default_factory=list)
    inferred_verb: Verb = Verb.PRICE
    inferred_axis: Optional[str] = None
    inferred_contract_params: Dict[str, Any] = Field(default_factory=dict)
    has_ore_keywords: bool = False


# Catalog of runnable ORE models (keyed by engine_key in ore_models.py)
_MODEL_CATALOG: List[Dict[str, str]] = [
    {
        "id": "flat_vol",
        "node_id": "FlatBlackScholesVol",
        "name": "Flat Black-Scholes Vol",
        "ore_class": "ORE.BlackConstantVol + ORE.FdBlackScholesVanillaEngine",
        "default_reason": "Baseline constant-volatility benchmark for equity options",
    },
    {
        "id": "term_structure",
        "node_id": "VolTermStructure",
        "name": "Vol Term Structure",
        "ore_class": "ORE.BlackVarianceCurve + ORE.FdBlackScholesVanillaEngine",
        "default_reason": "Captures maturity slope of ATM implied volatility across expiries",
    },
    {
        "id": "local_vol",
        "node_id": "LocalVolSurface",
        "name": "Dupire Local Vol Surface",
        "ore_class": "ORE.BlackVarianceSurface (localVol=True)",
        "default_reason": "Fits strike skew and term structure across the option chain",
    },
    {
        "id": "heston",
        "node_id": "HestonStochasticVol",
        "name": "Heston Stochastic Vol",
        "ore_class": "ORE.HestonModel + ORE.FdHestonVanillaEngine",
        "default_reason": "Models stochastic variance, vol-of-vol, and spot-vol correlation",
    },
]

# Catalog of runnable ORE calculators / engine suites
_CALCULATOR_CATALOG: List[Dict[str, str]] = [
    {
        "id": "compare_vol_models",
        "name": "Volatility Model Comparator",
        "ore_engine": "ORE.FdBlackScholesVanillaEngine + ORE.FdHestonVanillaEngine",
        "outputs": "Price, Delta, Chain RMSE, Model Rank",
        "default_reason": "Compare selected volatility models side-by-side against market chain",
    },
    {
        "id": "compare_pricing_engines",
        "name": "Pricing Engine Comparator",
        "ore_engine": "FD PDE, Binomial CRR, Barone-Adesi-Whaley, Bjerksund-Stensland",
        "outputs": "Price, Delta, Chain RMSE, Engine Rank",
        "default_reason": "Benchmark numerical & analytic American option engines in ORE",
    },
    {
        "id": "price_single",
        "name": "Single-Contract ORE Pricer & Greeks",
        "ore_engine": "ORE.VanillaOption + Selected Model/Engine",
        "outputs": "NPV (USD), Delta, Payoff Cashflow",
        "default_reason": "Fast in-process valuation and Greeks for one option contract",
    },
    {
        "id": "price_ore_app",
        "name": "OREApp XML Portfolio Runner",
        "ore_engine": "ORE.OREApp (portfolio.xml, pricingengine.xml)",
        "outputs": "npv.csv, flows.csv, Full ORE XML Audit Trail",
        "default_reason": "Full ORE XML batch run for trade-level reporting and cashflows",
    },
]

_KNOWN_TICKERS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "SPX", "TSLA", "IWM")


def _node_category_to_keyword_category(node_id: str, cat: NodeCategory, axis: Optional[str]) -> KeywordCategory:
    if cat == NodeCategory.ROOT:
        return "instrument"
    if cat == NodeCategory.VARIES:
        if axis == Axis.PRICING_ENGINE.value:
            return "engine"
        return "model"
    if cat == NodeCategory.FORMULA:
        return "calculator"
    if cat == NodeCategory.SIBLING:
        return "adjacent_ore"
    # NEEDS nodes
    if node_id in {"OREInProcessInstrument"}:
        return "instrument"
    if node_id in {"ORETradeXML", "PricingEngineRouter"}:
        return "engine"
    return "input_param"


def _build_graph_alias_entries(
    graph: OREDomainGraph,
) -> List[Tuple[str, str, KeywordCategory, str, str]]:
    """Return list of (alias, canonical_label, category, ore_ref, node_id)."""
    entries: List[Tuple[str, str, KeywordCategory, str, str]] = []
    for node in graph.nodes.values():
        kw_cat = _node_category_to_keyword_category(node.id, node.category, node.axis)
        ore_ref = node.xml_tag or node.code_ref or node.id
        # Include label + aliases
        candidates = [node.label, *node.aliases]
        for alias in candidates:
            cleaned = alias.strip()
            if len(cleaned) >= 2:
                entries.append((cleaned, node.label, kw_cat, ore_ref, node.id))

    # Extra high-signal ORE framework keywords not already covered verbatim
    extra_entries: List[Tuple[str, str, KeywordCategory, str, str]] = [
        ("American put", "American Put", "instrument", "ORE.VanillaOption(Put, AmericanExercise)", "EquityOption"),
        ("American call", "American Call", "instrument", "ORE.VanillaOption(Call, AmericanExercise)", "EquityOption"),
        ("European put", "European Put", "instrument", "ORE.VanillaOption(Put, EuropeanExercise)", "EuropeanExerciseSibling"),
        ("European call", "European Call", "instrument", "ORE.VanillaOption(Call, EuropeanExercise)", "EuropeanExerciseSibling"),
        ("Black-Scholes", "Flat Black-Scholes vol", "model", "ORE.BlackScholesMertonProcess", "FlatBlackScholesVol"),
        ("Black Scholes", "Flat Black-Scholes vol", "model", "ORE.BlackScholesMertonProcess", "FlatBlackScholesVol"),
        ("Dupire", "Local vol surface", "model", "ORE.BlackVarianceSurface(localVol=True)", "LocalVolSurface"),
        ("stochastic vol", "Heston stochastic vol", "model", "ORE.HestonModel", "HestonStochasticVol"),
        ("PDE", "Finite Difference (PDE)", "engine", "ORE.FdBlackScholesVanillaEngine", "FdBlackScholesEngine"),
        ("Crank-Nicolson", "Finite Difference (PDE)", "engine", "ORE.FdBlackScholesVanillaEngine", "FdBlackScholesEngine"),
        ("CRR", "Binomial CRR tree", "engine", "ORE.BinomialCRRVanillaEngine", "BinomialCRREngine"),
        ("binomial", "Binomial CRR tree", "engine", "ORE.BinomialCRRVanillaEngine", "BinomialCRREngine"),
        ("Greeks", "delta( )", "calculator", "ORE.VanillaOption.delta()", "formula_delta"),
        ("RMSE", "chain_rmse( )", "calculator", "chain_rmse()", "formula_chain_rmse"),
        ("npv.csv", "ORE Trade (OREApp XML)", "engine", "ORE.OREApp", "ORETradeXML"),
        ("flows.csv", "ORE Trade (OREApp XML)", "engine", "ORE.OREApp", "ORETradeXML"),
        ("swap", "Interest Rate Swap (adjacent)", "adjacent_ore", "ORE.Swap", "AdjacentSwap"),
    ]
    entries.extend(extra_entries)
    return entries


def _detect_structured_param_spans(text: str) -> List[DetectedKeyword]:
    """Detect structured contract & market input parameters (ticker, date, tenor, moneyness, strike)."""
    found: List[DetectedKeyword] = []

    # Ticker symbols (case-sensitive uppercase to avoid matching ordinary words)
    ticker_pat = re.compile(r"(?<!\w)(" + "|".join(_KNOWN_TICKERS) + r")(?!\w)")
    for m in ticker_pat.finditer(text):
        sym = m.group(1)
        found.append(
            DetectedKeyword(
                phrase=sym,
                canonical=f"Underlying: {sym}",
                category="input_param",
                ore_ref=f"EQUITY/PRICE/{sym}/USD",
                node_id=f"param:symbol:{sym}",
                start=m.start(),
                end=m.end(),
                resolved_value=sym,
            )
        )

    # ISO dates YYYY-MM-DD
    for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", text):
        dt = m.group(1)
        found.append(
            DetectedKeyword(
                phrase=dt,
                canonical=f"As-of Date: {dt}",
                category="input_param",
                ore_ref="ORE.Settings.evaluationDate",
                node_id=f"param:asof:{dt}",
                start=m.start(),
                end=m.end(),
                resolved_value=dt,
            )
        )

    # Tenors like 30-day, 45 day, 60D, 3M
    for m in re.finditer(r"\b(\d+)\s*[- ]?(?:day|days|d)\b", text, re.IGNORECASE):
        days = int(m.group(1))
        found.append(
            DetectedKeyword(
                phrase=m.group(0),
                canonical=f"Tenor: {days}D",
                category="input_param",
                ore_ref="ORE.Period(Days)",
                node_id=f"param:tenor:{days}D",
                start=m.start(),
                end=m.end(),
                resolved_value=f"{days}D",
            )
        )

    # Moneyness: ATM / OTM / ITM
    for m in re.finditer(r"\b(ATM|OTM|ITM)\b", text, re.IGNORECASE):
        mon = m.group(1).upper()
        found.append(
            DetectedKeyword(
                phrase=m.group(0),
                canonical=f"Moneyness: {mon}",
                category="input_param",
                ore_ref="OptionChain.resolve_contract(moneyness)",
                node_id=f"param:moneyness:{mon}",
                start=m.start(),
                end=m.end(),
                resolved_value=mon,
            )
        )

    # Explicit strikes: "strike 550" or "K=550" or "K 550"
    for m in re.finditer(r"\b(?:strike\s*=?\s*|K\s*=\s*)\$?(\d+(?:\.\d+)?)\b", text, re.IGNORECASE):
        strike_val = m.group(1)
        found.append(
            DetectedKeyword(
                phrase=m.group(0),
                canonical=f"Strike: {strike_val}",
                category="input_param",
                ore_ref="ORE.PlainVanillaPayoff(strike)",
                node_id=f"param:strike:{strike_val}",
                start=m.start(),
                end=m.end(),
                resolved_value=strike_val,
            )
        )

    return found


def detect_spans_in_text(text: str, graph: OREDomainGraph) -> List[DetectedKeyword]:
    """Return non-overlapping, longest-first ORE keyword spans for a single text block."""
    alias_entries = _build_graph_alias_entries(graph)
    raw_candidates: List[DetectedKeyword] = _detect_structured_param_spans(text)

    for alias, canonical, cat, ore_ref, node_id in alias_entries:
        pat = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)
        for m in pat.finditer(text):
            raw_candidates.append(
                DetectedKeyword(
                    phrase=text[m.start() : m.end()],
                    canonical=canonical,
                    category=cat,
                    ore_ref=ore_ref,
                    node_id=node_id,
                    start=m.start(),
                    end=m.end(),
                )
            )

    # Sort longest span first, breaking ties by preferring structured params & instrument/model/engine
    priority = {
        "instrument": 0,
        "model": 1,
        "engine": 2,
        "input_param": 3,
        "calculator": 4,
        "adjacent_ore": 5,
    }
    raw_candidates.sort(key=lambda kw: (-(kw.end - kw.start), priority[kw.category], kw.start))

    occupied: List[Tuple[int, int]] = []
    accepted: List[DetectedKeyword] = []
    for cand in raw_candidates:
        if any(not (cand.end <= os_ or cand.start >= oe) for os_, oe in occupied):
            continue
        occupied.append((cand.start, cand.end))
        accepted.append(cand)

    accepted.sort(key=lambda kw: kw.start)
    return accepted


def analyze_ore_keywords(
    text: str,
    graph: OREDomainGraph,
    selected_models: Optional[List[str]] = None,
    selected_calculators: Optional[List[str]] = None,
) -> KeywordAnalysisResult:
    """Detect all ORE keywords in text, group them separately, and recommend models & calculators."""
    spans = detect_spans_in_text(text, graph)

    # Deduplicate keywords by (node_id, phrase.lower()) preserving order of appearance
    seen: set[Tuple[str, str]] = set()
    unique_kws: List[DetectedKeyword] = []
    for kw in spans:
        key = (kw.node_id, kw.phrase.lower())
        if key not in seen:
            seen.add(key)
            unique_kws.append(kw)

    by_category: Dict[str, List[DetectedKeyword]] = {}
    for kw in unique_kws:
        by_category.setdefault(kw.category, []).append(kw)

    # Infer contract parameters from detected keywords & text
    lower = text.lower()
    inferred_params: Dict[str, Any] = {
        "symbol": "SPY",
        "asof": "2026-09-10",
        "dte": 30,
        "moneyness": "ATM",
        "option_type": "Call" if re.search(r"\bcall\b", lower) and not re.search(r"\bput\b", lower) else "Put",
        "exercise_style": "European" if re.search(r"\beuropean\b", lower) and not re.search(r"\bamerican\b", lower) else "American",
        "strike": None,
    }
    for kw in unique_kws:
        if kw.node_id.startswith("param:symbol:") and kw.resolved_value:
            inferred_params["symbol"] = kw.resolved_value
        elif kw.node_id.startswith("param:asof:") and kw.resolved_value:
            inferred_params["asof"] = kw.resolved_value
        elif kw.node_id.startswith("param:tenor:") and kw.resolved_value:
            inferred_params["dte"] = int(kw.resolved_value.rstrip("D"))
        elif kw.node_id.startswith("param:moneyness:") and kw.resolved_value:
            inferred_params["moneyness"] = kw.resolved_value
        elif kw.node_id.startswith("param:strike:") and kw.resolved_value:
            inferred_params["strike"] = float(kw.resolved_value)

    matched_node_ids = {kw.node_id for kw in unique_kws}
    explicit_model_ids = [
        m["id"] for m in _MODEL_CATALOG if m["node_id"] in matched_node_ids
    ]
    explicit_engine_nodes = {
        "FdBlackScholesEngine",
        "BinomialCRREngine",
        "BaroneAdesiWhaleyEngine",
        "BjerksundStenslandEngine",
    } & matched_node_ids
    explicit_ore_app = "ORETradeXML" in matched_node_ids

    # Determine verb & axis
    wants_explain = bool(re.search(r"\b(explain|how\s+does|walkthrough|architecture)\b", lower))
    wants_compare = bool(
        re.search(r"\b(compare|versus|vs\.?|rank|benchmark|across)\b", lower)
        or len(explicit_model_ids) >= 2
        or len(explicit_engine_nodes) >= 2
    )

    if wants_explain and not wants_compare and not explicit_model_ids and not explicit_engine_nodes:
        inferred_verb = Verb.EXPLAIN
        inferred_axis = None
    elif wants_compare:
        inferred_verb = Verb.COMPARE
        if len(explicit_engine_nodes) >= 2 or (
            explicit_engine_nodes and not explicit_model_ids and "engine" in lower
        ):
            inferred_axis = Axis.PRICING_ENGINE.value
        else:
            inferred_axis = Axis.VOLATILITY_MODEL.value
    else:
        inferred_verb = Verb.PRICE
        inferred_axis = None

    # Build model recommendations
    user_model_set = set(selected_models) if selected_models else None
    rec_models: List[ModelRecommendation] = []
    for entry in _MODEL_CATALOG:
        mid = entry["id"]
        is_explicit = mid in explicit_model_ids
        if user_model_set is not None:
            is_selected = mid in user_model_set
        elif explicit_model_ids:
            is_selected = is_explicit
        elif inferred_verb == Verb.COMPARE and inferred_axis == Axis.VOLATILITY_MODEL.value:
            is_selected = True
        else:
            # Default to local_vol for single American option pricing
            is_selected = mid == "local_vol"

        reason = (
            "Detected directly in your input keywords"
            if is_explicit
            else entry["default_reason"]
        )
        rec_models.append(
            ModelRecommendation(
                id=mid,
                name=entry["name"],
                ore_class=entry["ore_class"],
                reason=reason,
                matched_explicitly=is_explicit,
                selected=is_selected,
            )
        )

    # Build calculator recommendations
    user_calc_set = set(selected_calculators) if selected_calculators else None
    default_calc_id = "price_single"
    if explicit_ore_app:
        default_calc_id = "price_ore_app"
    elif inferred_verb == Verb.COMPARE:
        default_calc_id = (
            "compare_pricing_engines"
            if inferred_axis == Axis.PRICING_ENGINE.value
            else "compare_vol_models"
        )

    rec_calcs: List[CalculatorRecommendation] = []
    for entry in _CALCULATOR_CATALOG:
        cid = entry["id"]
        if user_calc_set is not None:
            is_selected = cid in user_calc_set
        else:
            is_selected = cid == default_calc_id

        reason = entry["default_reason"]
        if cid == "compare_vol_models" and explicit_model_ids:
            reason = f"Matches detected vol model keyword(s): {', '.join(explicit_model_ids)}"
        elif cid == "compare_pricing_engines" and explicit_engine_nodes:
            reason = f"Matches detected ORE engine keyword(s): {', '.join(sorted(explicit_engine_nodes))}"
        elif cid == "price_ore_app" and explicit_ore_app:
            reason = "Matches detected OREApp / XML trade keyword"

        rec_calcs.append(
            CalculatorRecommendation(
                id=cid,
                name=entry["name"],
                ore_engine=entry["ore_engine"],
                outputs=entry["outputs"],
                reason=reason,
                selected=is_selected,
            )
        )

    return KeywordAnalysisResult(
        keywords=unique_kws,
        by_category=by_category,
        recommended_models=rec_models,
        recommended_calculators=rec_calcs,
        inferred_verb=inferred_verb,
        inferred_axis=inferred_axis,
        inferred_contract_params=inferred_params,
        has_ore_keywords=len(unique_kws) > 0,
    )
