"""FastAPI server wiring ORE Python, Graphify Domain Graph, and Keyword Detector to the Web UI."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import ORE
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from recipe_workbench.compiler.binder import (
    AmbiguityPrompt,
    CompiledRecipe,
    _build_ambiguity_prompt,
    compile_recipe,
)
from recipe_workbench.compiler.parser import parse_recipe_text
from recipe_workbench.compiler.sidecar import SidecarData
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.keyword_detector import (
    CATEGORY_META,
    DetectedKeyword,
    KeywordAnalysisResult,
    analyze_ore_keywords,
    detect_spans_in_text,
)
from recipe_workbench.graph.picker import RootPicker
from recipe_workbench.graph.schema import Axis, CandidateHit, ScopeSpec, Verb
from recipe_workbench.graph.scope_walker import canonical_title, compute_scope

app = FastAPI(title="Recipe Workbench API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Singleton graph & picker initialized once at startup
_graph = OREDomainGraph()
_picker = RootPicker(_graph)


class AnalyzeRequest(BaseModel):
    title: str = "Compare volatility models on an American equity option"
    paragraphs: List[str] = Field(default_factory=list)
    disambiguation: Optional[str] = None
    selected_models: List[str] = Field(default_factory=list)
    selected_calculators: List[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    analysis: KeywordAnalysisResult
    paragraph_spans: List[List[DetectedKeyword]]
    ambiguity: Optional[AmbiguityPrompt] = None
    scope: ScopeSpec
    category_meta: Dict[str, Dict[str, str]]


class CompileRequest(BaseModel):
    title: str = "Compare volatility models on an American equity option"
    paragraphs: List[str] = Field(default_factory=list)
    asof: str = "2026-09-10"
    symbol: str = "SPY"
    disambiguation: Optional[str] = None
    selected_models: List[str] = Field(default_factory=list)
    selected_calculators: List[str] = Field(default_factory=list)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "graph_version": _graph.graph_version,
        "ore_version": getattr(ORE, "__version__", "1.8.17.0"),
    }


@app.get("/api/picker/roots", response_model=List[CandidateHit])
def picker_roots(q: str = "") -> List[CandidateHit]:
    return _picker.filter_roots(q)


@app.get("/api/picker/verbs", response_model=List[CandidateHit])
def picker_verbs(root_id: str = "EquityOption", q: str = "") -> List[CandidateHit]:
    return _picker.filter_verbs(root_id, q)


@app.get("/api/picker/axes", response_model=List[CandidateHit])
def picker_axes(
    root_id: str = "EquityOption",
    verb: str = "Compare",
    q: str = "",
) -> List[CandidateHit]:
    try:
        verb_enum = Verb(verb)
    except ValueError:
        verb_enum = Verb.COMPARE
    return _picker.filter_axes(root_id, verb_enum, q)


@app.get("/api/picker/title")
def picker_canonical_title(
    root_id: str = "EquityOption",
    verb: str = "Compare",
    axis: Optional[str] = None,
) -> Dict[str, str]:
    root = _graph.get_node(root_id)
    root_label = root.label if root else "American equity option"
    try:
        verb_enum = Verb(verb)
    except ValueError:
        verb_enum = Verb.COMPARE
    return {"title": canonical_title(verb_enum, root_label, axis)}


@app.get("/api/scope", response_model=ScopeSpec)
def get_scope(
    root_id: str = "EquityOption",
    verb: str = "Compare",
    axis: Optional[str] = "volatility_model",
    disambiguation: Optional[str] = None,
) -> ScopeSpec:
    try:
        verb_enum = Verb(verb)
    except ValueError:
        verb_enum = Verb.COMPARE
    resolved_axis = axis if verb_enum == Verb.COMPARE else None
    return compute_scope(
        _graph,
        root_id=root_id,
        verb=verb_enum,
        axis=resolved_axis,
        disambiguation=disambiguation,
    )


def _resolve_scope_for_request(
    title: str,
    analysis: KeywordAnalysisResult,
    selected_calculators: List[str],
    disambiguation: Optional[str],
) -> ScopeSpec:
    try:
        root_id, verb, axis = _picker.parse_title(title)
    except ValueError:
        root_id = "EquityOption"
        verb = analysis.inferred_verb
        axis = analysis.inferred_axis
        if verb == Verb.EXPLAIN:
            verb = Verb.PRICE

    if selected_calculators:
        if "compare_pricing_engines" in selected_calculators:
            verb, axis = Verb.COMPARE, Axis.PRICING_ENGINE.value
        elif "compare_vol_models" in selected_calculators:
            verb, axis = Verb.COMPARE, Axis.VOLATILITY_MODEL.value
        elif "price_ore_app" in selected_calculators:
            verb, axis, disambiguation = Verb.PRICE, None, "ore_trade"
        elif "price_single" in selected_calculators:
            verb, axis = Verb.PRICE, None

    return compute_scope(
        _graph,
        root_id=root_id,
        verb=verb,
        axis=axis,
        title=title.lstrip("#").strip(),
        disambiguation=disambiguation,
    )


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze_text(req: AnalyzeRequest) -> AnalyzeResponse:
    """Fast keystroke-level ORE keyword detection, span separation, and model/calculator recommendations."""
    full_text = "\n".join([req.title, *req.paragraphs])
    analysis = analyze_ore_keywords(
        full_text,
        _graph,
        selected_models=req.selected_models or None,
        selected_calculators=req.selected_calculators or None,
    )
    paragraph_spans = [detect_spans_in_text(p, _graph) for p in req.paragraphs]

    ambiguity: Optional[AmbiguityPrompt] = None
    if req.disambiguation is None:
        for p in req.paragraphs:
            m = re.search(r"\bamerican\s+put\b", p, re.IGNORECASE)
            if m:
                ambiguity = _build_ambiguity_prompt(m.group(0))
                break

    scope = _resolve_scope_for_request(
        req.title,
        analysis,
        req.selected_calculators,
        req.disambiguation,
    )
    cat_meta = {
        k: {"label": v[0], "emoji": v[1], "color": v[2]}
        for k, v in CATEGORY_META.items()
    }
    return AnalyzeResponse(
        analysis=analysis,
        paragraph_spans=paragraph_spans,
        ambiguity=ambiguity,
        scope=scope,
        category_meta=cat_meta,
    )


@app.post("/api/compile", response_model=CompiledRecipe)
def compile_live(req: CompileRequest) -> CompiledRecipe:
    """Compile freeform or structured recipe text live with ORE Python."""
    clean_title = req.title.lstrip("#").strip() or "Interactive ORE Recipe"
    body = "\n\n".join(p.strip() for p in req.paragraphs if p.strip())
    if not body:
        body = "Price a 30-day ATM American put on SPY."
    md_text = f"# {clean_title}\n\n---\nasof: \"{req.asof}\"\nsymbol: {req.symbol}\n---\n\n{body}\n"
    parsed = parse_recipe_text(md_text, path="<web-editor>")

    sidecar = SidecarData(
        slug="web-editor",
        disambiguations={"american_put": req.disambiguation} if req.disambiguation else {},
        selected_models=req.selected_models,
        selected_calculators=req.selected_calculators,
    )
    return compile_recipe(
        parsed,
        _graph,
        sidecar=sidecar,
        allow_network=False,
        use_llm_fallback=False,
    )


_WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
if _WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")
