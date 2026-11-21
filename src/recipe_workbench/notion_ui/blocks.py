"""Notion block tree builders approximating all 3 screens of the Recipe Workbench mockup."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml

from recipe_workbench.compiler.binder import CompiledRecipe, ParagraphAnnotation
from recipe_workbench.explainer.generator import ExplainerDocument
from recipe_workbench.graph.schema import CandidateHit, ScopeSpec


def rt(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    code: bool = False,
    color: str = "default",
) -> List[Dict[str, Any]]:
    """Build Notion rich_text array, splitting strings safely under the 2000-char Notion limit."""
    if not text:
        return []
    chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
    return [
        {
            "type": "text",
            "text": {"content": chunk},
            "annotations": {
                "bold": bold,
                "italic": italic,
                "strikethrough": False,
                "underline": underline,
                "code": code,
                "color": color,
            },
        }
        for chunk in chunks
    ]


def paragraph(rich_text: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text or rt(" ")}}


def heading(level: int, text: str) -> Dict[str, Any]:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": rt(text)}}


def callout(
    rich_text: List[Dict[str, Any]],
    emoji: str = "📌",
    color: str = "gray_background",
    children: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "rich_text": rich_text,
        "icon": {"type": "emoji", "emoji": emoji},
        "color": color,
    }
    if children:
        body["children"] = children
    return {"object": "block", "type": "callout", "callout": body}


def toggle(title_rt: List[Dict[str, Any]], children: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": title_rt,
            "children": children or [paragraph(rt("(empty)"))],
        },
    }


def bulleted(rich_text: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text}}


def todo(rich_text: List[Dict[str, Any]], checked: bool = False) -> Dict[str, Any]:
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": rich_text, "checked": checked}}


def divider() -> Dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def code_block(code: str, language: str = "python") -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": rt(code),
            "language": language,
        },
    }


def notion_table(headers: List[str], rows: List[List[List[Dict[str, Any]]]]) -> Dict[str, Any]:
    width = len(headers)
    table_rows = [
        {
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": [rt(h, bold=True) for h in headers]},
        }
    ]
    for r in rows:
        padded = list(r[:width])
        while len(padded) < width:
            padded.append(rt(""))
        table_rows.append(
            {
                "object": "block",
                "type": "table_row",
                "table_row": {"cells": padded},
            }
        )
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": table_rows,
        },
    }


def render_annotated_paragraph_rich_text(ann: ParagraphAnnotation) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    cursor = 0
    for sp in sorted(ann.spans, key=lambda x: x.start):
        if sp.start > cursor:
            segments.extend(rt(ann.text[cursor : sp.start]))
        color = "orange" if sp.proposed else "green"
        segments.extend(rt(ann.text[sp.start : sp.end], bold=True, underline=True, color=color))
        cursor = sp.end
    if cursor < len(ann.text):
        segments.extend(rt(ann.text[cursor:]))

    if ann.inline_result_pill:
        segments.extend(rt("  "))
        segments.extend(rt(f"▸ {ann.inline_result_pill}", bold=True, code=True, color="green"))
    return segments


def _build_scope_rail_blocks(scope: ScopeSpec) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [
        heading(3, "🧭 In Scope"),
        paragraph(
            rt(
                f"Walked from {scope.root_label}. Nothing outside this list can bind.",
                italic=True,
                color="gray",
            )
        ),
        heading(3, "Needs · always in"),
    ]
    for node in scope.needs:
        dot = "🟠" if node.is_data_leaf else "🟢"
        blocks.append(
            bulleted(
                rt(f"{dot} ") + rt(node.label, bold=True) + rt(f"  ({node.layer})", color="gray")
            )
        )

    if scope.varies:
        blocks.append(heading(3, f"Varies · {scope.axis or 'axis'}"))
        for node in scope.varies:
            blocks.append(
                bulleted(
                    rt("⚪ ") + rt(node.label, bold=True) + rt(f"  [{node.id}]", color="gray")
                )
            )

    blocks.append(heading(3, "Formulas"))
    for fnode in scope.formulas:
        blocks.append(
            bulleted(rt("ƒ ") + rt(fnode.label, bold=True, code=True))
        )

    if scope.siblings:
        sib_preview = ", ".join(s.label for s in scope.siblings[:3])
        sib_children = [
            bulleted(rt(f"{s.label} ({s.id}) — {s.description}", color="gray"))
            for s in scope.siblings
        ]
        blocks.append(
            toggle(
                rt(f"Show {len(scope.siblings)} more nearby ({sib_preview}…)", color="gray"),
                sib_children,
            )
        )
    return blocks


def build_recipe_page_blocks(compiled: CompiledRecipe) -> List[Dict[str, Any]]:
    """Build Screen 2 (two-column recipe + scope rail) and Screen 3 (ambiguity resolver) in Notion blocks."""
    scope = compiled.scope
    top_callout = callout(
        rt("Scope: ", bold=True)
        + rt(scope.root_label, bold=True)
        + rt(f" · needs {len(scope.needs)}, varies {len(scope.varies)}, {len(scope.siblings)} hidden · ")
        + rt("Snapshot: ", bold=True)
        + rt(compiled.snapshot_summary)
        + rt(f" · Graph {scope.graph_version}", color="gray"),
        emoji="🧪",
        color="gray_background",
    )

    # Left column: Editable prose + Compiled annotations & tables
    left_blocks: List[Dict[str, Any]] = [
        heading(3, "✍️ Recipe Prose (edit below & sync)"),
    ]
    for p in compiled.recipe.paragraphs:
        left_blocks.append(paragraph(rt(p.text)))

    fm_yaml = yaml.safe_dump(
        compiled.recipe.front_matter or {"asof": compiled.recipe.asof, "symbol": compiled.recipe.symbol},
        sort_keys=False,
    ).strip()
    left_blocks.append(
        toggle(
            rt("⚙️ Conventions & Front Matter", bold=True, color="gray"),
            [code_block(fm_yaml, language="yaml")],
        )
    )
    left_blocks.append(divider())
    left_blocks.append(heading(3, "📊 Compiled Annotations & ORE Results"))

    for ann in compiled.annotations:
        left_blocks.append(paragraph(render_annotated_paragraph_rich_text(ann)))

        if ann.margin_ok or ann.provenance_notes:
            msg_rt: List[Dict[str, Any]] = []
            if ann.margin_ok:
                msg_rt.extend(rt(" · ".join(ann.margin_ok), bold=True, color="green"))
            if ann.provenance_notes:
                if msg_rt:
                    msg_rt.extend(rt("\n"))
                msg_rt.extend(rt(" · ".join(ann.provenance_notes), color="gray"))
            left_blocks.append(callout(msg_rt, emoji="✅", color="green_background"))

        for assump in ann.assumptions:
            left_blocks.append(
                callout(rt(assump, color="brown"), emoji="⚠️", color="yellow_background")
            )

        # Screen 3: Interactive Ambiguity Resolver
        if ann.ambiguity is not None:
            left_blocks.append(
                callout(
                    rt(ann.ambiguity.heading + "\n", bold=True)
                    + rt(ann.ambiguity.lead, color="gray"),
                    emoji="❓",
                    color="orange_background",
                )
            )
            for opt in ann.ambiguity.options:
                left_blocks.append(
                    todo(
                        rt(f"[{opt.id}] ", bold=True, code=True)
                        + rt(f"{opt.title} — ", bold=True)
                        + rt(f"{opt.description} ({opt.brings_into_scope})", color="gray"),
                        checked=False,
                    )
                )

        # Compare Table
        if ann.comparison_result is not None:
            cmp_res = ann.comparison_result
            headers = ["Model", "ORE Engine", "Price", "Delta", "Chain RMSE"]
            t_rows: List[List[List[Dict[str, Any]]]] = []
            for row in cmp_res.rows:
                is_best = row.rank == 1
                col_color = "green" if is_best else "default"
                tag = " 🏆" if is_best else ""
                t_rows.append(
                    [
                        rt(f"{row.model_name}{tag}", bold=is_best, color=col_color),
                        rt(row.engine_name, code=True, color=col_color),
                        rt(f"{row.price:.4f}", bold=is_best, color=col_color),
                        rt(f"{row.delta:.4f}", bold=is_best, color=col_color),
                        rt(f"{row.chain_rmse:.4f} (#{row.rank})", bold=is_best, color=col_color),
                    ]
                )
            left_blocks.append(notion_table(headers, t_rows))

        # Single Price / OREApp Table
        if ann.price_result is not None:
            pr = ann.price_result
            headers = ["Contract", "NPV (USD)", "Delta", "Expiry / Cashflow"]
            cf_str = ", ".join(f"{c.date}: {c.amount:.4f} {c.currency}" for c in pr.cashflows)
            t_rows = [
                [
                    rt(pr.contract.contract_symbol, bold=True, code=True),
                    rt(f"{pr.npv:.4f}", bold=True, color="green"),
                    rt(f"{pr.delta:.4f}" if pr.delta is not None else "N/A (OREApp)"),
                    rt(cf_str),
                ]
            ]
            left_blocks.append(notion_table(headers, t_rows))

    right_blocks = _build_scope_rail_blocks(scope)

    two_col = {
        "object": "block",
        "type": "column_list",
        "column_list": {
            "children": [
                {"object": "block", "type": "column", "column": {"children": left_blocks}},
                {"object": "block", "type": "column", "column": {"children": right_blocks}},
            ]
        },
    }
    return [top_callout, two_col]


def build_explainer_blocks(explainer: ExplainerDocument) -> List[Dict[str, Any]]:
    """Build the 5-layer grounded Explainer page in Notion blocks (FR-26, FR-27)."""
    blocks: List[Dict[str, Any]] = [
        callout(
            rt(explainer.summary_line, bold=True)
            + rt(f"\nScope slug: {explainer.scope_slug} · Graph: {explainer.graph_version}", color="gray"),
            emoji="📚",
            color="blue_background",
        ),
        toggle(
            rt("Layer 1 · Business Intuition (human-curated & editable)", bold=True),
            [paragraph(rt(explainer.layer_1_business))],
        ),
        toggle(
            rt("Layer 2 · Technical & Quant Intuition", bold=True),
            [
                bulleted(
                    rt(s.text, color="red" if not s.grounded else "default")
                    + rt(f"  [{', '.join(s.cited_node_ids) or 'UNGROUNDED'}]", code=True, color="gray")
                )
                for s in explainer.layer_2_quant
            ],
        ),
        toggle(
            rt("Layer 3 · ORE Architecture Map (Graphify + Mermaid)", bold=True),
            [
                code_block(explainer.layer_3_architecture_mermaid, language="mermaid"),
                *[
                    bulleted(
                        rt(s.text, color="red" if not s.grounded else "default")
                        + rt(f"  [{', '.join(s.cited_node_ids) or 'UNGROUNDED'}]", code=True, color="gray")
                    )
                    for s in explainer.layer_3_architecture_notes
                ],
            ],
        ),
        toggle(
            rt("Layer 4 · ORE Object Drill-Down", bold=True),
            [
                notion_table(
                    ["Node ID", "Layer", "Code / Header Ref", "XML / Summary"],
                    [
                        [
                            rt(obj.id, bold=True, code=True),
                            rt(obj.layer),
                            rt(obj.code_ref, code=True),
                            rt(f"{obj.xml_tag} — {obj.description}"),
                        ]
                        for obj in explainer.layer_4_objects
                    ],
                )
            ],
        ),
        toggle(
            rt("Layer 5 · Worked Example (Runnable ORE Python)", bold=True),
            [
                code_block(explainer.layer_5_code, language="python"),
                callout(
                    rt("Verified Live ORE Output:\n", bold=True, color="green")
                    + rt(explainer.layer_5_verified_output, code=True),
                    emoji="⚡",
                    color="green_background",
                ),
            ],
        ),
    ]
    return blocks


def build_hub_blocks(
    roots: List[CandidateHit],
    verbs: List[CandidateHit],
    axes: List[CandidateHit],
) -> List[Dict[str, Any]]:
    """Build Screen 1 (Root Picker reference + instructions) for the Notion Hub page."""
    rows: List[List[List[Dict[str, Any]]]] = []
    for r in roots:
        for v in verbs:
            if v.id == "Compare":
                for a in axes:
                    rows.append([rt(r.name, bold=True), rt(v.name), rt(a.name), rt(f"{r.source_layer} · {a.source_layer}")])
            else:
                rows.append([rt(r.name, bold=True), rt(v.name), rt("—"), rt(r.source_layer)])

    return [
        callout(
            rt("Recipe Workbench v1 — ORE + Graphify + Gemini\n", bold=True)
            + rt("Screen 1 · Root Picker: Choose a valid (Product, Action, Axis) combination from the ORE knowledge graph below, or open any recipe in the database."),
            emoji="🔬",
            color="blue_background",
        ),
        heading(2, "Valid Root Picker Candidates (Screen 1)"),
        notion_table(["Product (Root)", "Action (Verb)", "Varies (Axis)", "Source Layer"], rows),
        divider(),
        heading(2, "Recipes Database"),
    ]


def render_markdown_preview(compiled: CompiledRecipe) -> str:
    """Render a human-readable Markdown mirror of the Notion page layout."""
    lines = [
        f"# {compiled.scope.title}",
        "",
        f"> **Scope:** `{compiled.scope.root_label}` · needs {len(compiled.scope.needs)}, "
        f"varies {len(compiled.scope.varies)}, {len(compiled.scope.siblings)} hidden · "
        f"**Snapshot:** `{compiled.snapshot_summary}` · **Graph:** `{compiled.scope.graph_version}`",
        "",
    ]
    for ann in compiled.annotations:
        spans_desc = ", ".join(f"`{s.phrase}`→`{s.target_id}`" for s in ann.spans)
        lines.append(ann.text)
        if ann.margin_ok:
            lines.append(f"- **Margin:** {' | '.join(ann.margin_ok)}")
        if spans_desc:
            lines.append(f"- **Bindings:** {spans_desc}")
        if ann.provenance_notes:
            lines.append(f"- **Provenance:** {' | '.join(ann.provenance_notes)}")
        for a in ann.assumptions:
            lines.append(f"- **Assumption:** {a}")
        if ann.ambiguity:
            lines.append(f"- **AMBIGUITY:** {ann.ambiguity.heading} ({[o.id for o in ann.ambiguity.options]})")
        if ann.comparison_result:
            lines.append("")
            lines.append("| Model | ORE Engine | Price | Delta | Chain RMSE | Rank |")
            lines.append("|---|---|---:|---:|---:|---:|")
            for row in ann.comparison_result.rows:
                lines.append(
                    f"| {row.model_name} | `{row.engine_name}` | {row.price:.4f} | {row.delta:.4f} | {row.chain_rmse:.4f} | #{row.rank} |"
                )
        if ann.price_result:
            pr = ann.price_result
            lines.append("")
            lines.append(
                f"| Contract | NPV | Delta | Engine |\n|---|---:|---:|---|\n"
                f"| `{pr.contract.contract_symbol}` | {pr.npv:.4f} | {pr.delta if pr.delta is not None else 'N/A'} | `{pr.provenance.engine_name}` |"
            )
        lines.append("")

    lines.append("## In Scope Rail")
    lines.append("- **Needs:** " + ", ".join(n.label for n in compiled.scope.needs))
    if compiled.scope.varies:
        lines.append("- **Varies:** " + ", ".join(v.label for v in compiled.scope.varies))
    lines.append("- **Formulas:** " + ", ".join(f.label for f in compiled.scope.formulas))
    lines.append("- **Hidden siblings:** " + ", ".join(s.label for s in compiled.scope.siblings))
    lines.append("")
    return "\n".join(lines)
