"""Deterministic Scope Walker for Recipe Workbench (FR-06 to FR-10)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.schema import Axis, GraphEdge, GraphNode, NodeCategory, ScopeSpec, Verb


def slugify(title: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return clean or "untitled-recipe"


def canonical_title(verb: Verb, root_label: str, axis: Optional[str] = None) -> str:
    if verb == Verb.COMPARE and axis:
        if axis == Axis.VOLATILITY_MODEL.value:
            return f"Compare volatility models on an {root_label}"
        if axis == Axis.PRICING_ENGINE.value:
            return f"Compare pricing engines on an {root_label}"
        return f"Compare {root_label} varying {axis}"
    if verb == Verb.PRICE:
        return f"Price an {root_label}"
    return f"Explain {root_label}"


def compute_scope(
    graph: OREDomainGraph,
    root_id: str,
    verb: Verb,
    axis: Optional[str] = None,
    title: Optional[str] = None,
    uses: Optional[Iterable[str]] = None,
    disambiguation: Optional[str] = None,
) -> ScopeSpec:
    root = graph.get_node(root_id)
    if root is None or root.category != NodeCategory.ROOT:
        raise ValueError(f"Unknown or non-root node: {root_id!r}. Valid roots: ['EquityOption']")

    if verb == Verb.COMPARE and not axis:
        raise ValueError("Compare verb requires an axis ('volatility_model' or 'pricing_engine').")

    resolved_title = title or canonical_title(verb, root.label, axis)
    needs: List[GraphNode] = []
    varies: List[GraphNode] = []
    formulas: List[GraphNode] = []
    siblings: List[GraphNode] = []
    edges: List[GraphEdge] = []

    for edge in graph.edges:
        if edge.source != root_id:
            continue
        target = graph.get_node(edge.target)
        if target is None:
            continue

        if edge.relation == "needs":
            if target.id == "ORETradeXML" and disambiguation != "ore_trade":
                # Keep core 5 data/engine needs clean unless ORE Trade XML selected; still track in edges
                if disambiguation is None:
                    pass
                else:
                    continue
            if target.id == "OREInProcessInstrument" and disambiguation == "ore_trade":
                continue
            if target.id in {"OREInProcessInstrument", "ORETradeXML"} and disambiguation is None:
                # Only surface the 5 core needs in rail until disambiguated, matching mockup
                continue
            needs.append(target)
            edges.append(edge)
        elif edge.relation == "alternatives" and verb == Verb.COMPARE and edge.axis == axis:
            varies.append(target)
            edges.append(edge)
        elif edge.relation == "produces" and verb in target.allowed_verbs:
            formulas.append(target)
            edges.append(edge)
        elif edge.relation == "sibling":
            siblings.append(target)

    # FR-10: front-matter uses: can bring siblings into scope, still subject to verb rule
    uses_added: List[str] = []
    if uses:
        existing_ids = {n.id for n in [*needs, *varies, *formulas]}
        for extra_id in uses:
            node = graph.get_node(extra_id)
            if node is None:
                raise ValueError(f"Unknown node in front-matter uses: {extra_id!r}")
            if node.category == NodeCategory.FORMULA and verb not in node.allowed_verbs:
                raise ValueError(
                    f"Formula {node.id!r} is not permitted for verb {verb.value!r} (FR-08 / FR-10)."
                )
            if node.id not in existing_ids:
                if node.category == NodeCategory.FORMULA:
                    formulas.append(node)
                else:
                    needs.append(node)
                uses_added.append(node.id)
                existing_ids.add(node.id)

    return ScopeSpec(
        title=resolved_title,
        slug=slugify(resolved_title),
        root_id=root.id,
        root_label=root.label,
        verb=verb,
        axis=axis,
        disambiguation=disambiguation,
        graph_version=graph.graph_version,
        needs=needs,
        varies=varies,
        formulas=formulas,
        siblings=siblings,
        edges=edges,
        uses_added=uses_added,
    )


def save_scope(scope: ScopeSpec, output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Byte-identical deterministic JSON serialization (FR-07)
    serialized = json.dumps(scope.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(serialized, encoding="utf-8")
    return path
