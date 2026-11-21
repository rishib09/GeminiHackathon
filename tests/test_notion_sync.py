"""Unit tests for Notion UI block nesting safety and sync helpers."""

from __future__ import annotations

from typing import Any, Dict, List

from recipe_workbench.compiler.binder import compile_recipe
from recipe_workbench.compiler.parser import parse_recipe_file, parse_recipe_text
from recipe_workbench.compiler.sidecar import SidecarData
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.keyword_detector import analyze_ore_keywords
from recipe_workbench.notion_ui.blocks import (
    build_recipe_page_blocks,
    render_annotated_paragraph_rich_text,
)
from recipe_workbench.notion_ui.sync import (
    _append_blocks_batched,
    _extract_controls_from_blocks,
    _reconstruct_recipe_header,
    normalize_notion_id,
)


def _max_children_depth(blocks: List[Dict[str, Any]], depth: int = 0) -> int:
    max_d = depth
    for blk in blocks:
        btype = blk.get("type", "")
        body = blk.get(btype, {})
        children = body.get("children") if isinstance(body, dict) else None
        if children:
            max_d = max(max_d, _max_children_depth(children, depth + 1))
    return max_d


class _FakeBlocksChildren:
    def __init__(self) -> None:
        self.append_calls: List[Dict[str, Any]] = []
        self._counter = 0
        self._children_by_parent: Dict[str, List[Dict[str, Any]]] = {}

    def append(self, block_id: str, children: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.append_calls.append({"block_id": block_id, "children": children})
        created: List[Dict[str, Any]] = []
        for blk in children:
            self._counter += 1
            bid = f"blk-{self._counter}"
            item = {"id": bid, "type": blk["type"]}
            created.append(item)
            if blk["type"] == "column_list":
                cols = []
                for _ in blk["column_list"]["children"]:
                    self._counter += 1
                    cols.append({"id": f"col-{self._counter}", "type": "column"})
                self._children_by_parent[bid] = cols
        return {"results": created}

    def list(self, block_id: str, page_size: int = 100, start_cursor: str | None = None) -> Dict[str, Any]:
        return {"results": self._children_by_parent.get(block_id, []), "has_more": False}


class _FakeNotionClient:
    def __init__(self) -> None:
        self.children = _FakeBlocksChildren()
        self.blocks = type("Blocks", (), {"children": self.children})()


def test_column_list_append_respects_notion_2_level_nesting_limit() -> None:
    graph = OREDomainGraph()
    parsed = parse_recipe_file("recipes/01_compare_vol_models.md")
    compiled = compile_recipe(parsed, graph, allow_network=False, use_llm_fallback=False)
    blocks = build_recipe_page_blocks(compiled)

    fake = _FakeNotionClient()
    _append_blocks_batched(fake, "page-1", blocks)  # type: ignore[arg-type]

    assert len(fake.children.append_calls) >= 3
    for call in fake.children.append_calls:
        depth = _max_children_depth(call["children"])
        assert depth <= 2, f"Payload exceeded Notion 2-level nesting limit (depth={depth})"


def test_reconstruct_recipe_header_safe() -> None:
    header = _reconstruct_recipe_header("# Price an American equity option", {"asof": "2026-09-10", "symbol": "SPY"})
    assert header.startswith("# Price an American equity option\n\n---\n")
    assert "asof: '2026-09-10'" in header or 'asof: "2026-09-10"' in header


def test_normalize_notion_id_url() -> None:
    raw = "https://www.notion.so/Recipe-Test-page-41fb38fccfe18097825ef752c13de530?pvs=4"
    assert normalize_notion_id(raw) == "41fb38fc-cfe1-8097-825e-f752c13de530"


def test_ore_keyword_detection_separates_categories_and_recommends_models() -> None:
    graph = OREDomainGraph()
    text = (
        "Price a 30-day ATM American put on SPY using Heston and Local Vol, "
        "and compare their delta, NPV, and chain RMSE against Binomial CRR."
    )
    analysis = analyze_ore_keywords(text, graph)

    assert analysis.has_ore_keywords
    assert "instrument" in analysis.by_category
    assert "input_param" in analysis.by_category
    assert "model" in analysis.by_category
    assert "engine" in analysis.by_category
    assert "calculator" in analysis.by_category

    selected_models = {m.id for m in analysis.recommended_models if m.selected}
    assert selected_models == {"heston", "local_vol"}

    selected_calcs = {c.id for c in analysis.recommended_calculators if c.selected}
    assert "compare_vol_models" in selected_calcs


def test_engine_keywords_recommend_pricing_engine_comparator() -> None:
    graph = OREDomainGraph()
    text = "Compare Binomial CRR and Barone-Adesi-Whaley pricing engines on a 30-day ATM SPY American put."
    analysis = analyze_ore_keywords(text, graph)

    selected_calcs = {c.id for c in analysis.recommended_calculators if c.selected}
    assert "compare_pricing_engines" in selected_calcs


def test_freeform_sandbox_compiles_and_renders_separated_keyword_chips() -> None:
    graph = OREDomainGraph()
    parsed = parse_recipe_file("recipes/04_ore_keyword_sandbox.md")
    compiled = compile_recipe(parsed, graph, allow_network=False, use_llm_fallback=False)

    assert compiled.keyword_analysis is not None
    assert compiled.comparison_result is not None
    # Only the 2 explicitly detected vol models (Heston + Local Vol) should be run
    assert len(compiled.comparison_result.rows) == 2

    # Inline annotations render each keyword as a distinct code pill with category color
    rich_segments = render_annotated_paragraph_rich_text(compiled.annotations[0])
    code_pills = [seg for seg in rich_segments if seg["annotations"].get("code")]
    colors = {seg["annotations"]["color"] for seg in code_pills}
    assert len(code_pills) >= 5
    assert len(colors) >= 3  # distinct colors across instrument / input_param / model / engine / calculator

    # Notion blocks stay under 2-level nesting limit
    blocks = build_recipe_page_blocks(compiled)
    fake = _FakeNotionClient()
    _append_blocks_batched(fake, "page-sandbox", blocks)  # type: ignore[arg-type]
    for call in fake.children.append_calls:
        assert _max_children_depth(call["children"]) <= 2

    # Round-trip extraction of [model:*] and [calc:*] checkboxes from left column
    left_blocks = blocks[1]["column_list"]["children"][0]["column"]["children"]
    prose, disambig, models, calcs, has_struct = _extract_controls_from_blocks(left_blocks)
    assert has_struct is True
    assert len(prose) == 1
    assert set(models) == {"local_vol", "heston"}
    assert calcs == ["compare_vol_models"]


def test_user_selected_single_heston_model_via_sidecar() -> None:
    graph = OREDomainGraph()
    parsed = parse_recipe_text(
        "# Custom SPY Desk Check\n\nEvaluate a 30-day ATM American put on SPY with Heston.\n"
    )
    sc = SidecarData(slug="custom", selected_models=["heston"], selected_calculators=["price_single"])
    compiled = compile_recipe(parsed, graph, sidecar=sc, allow_network=False, use_llm_fallback=False)

    assert compiled.price_result is not None
    assert "Heston" in compiled.price_result.provenance.model_name
    assert compiled.price_result.npv > 0
