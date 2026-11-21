"""Live Notion.so API synchronization, workspace bootstrap, and polling watcher."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from notion_client import Client

from recipe_workbench.compiler.binder import compile_recipe
from recipe_workbench.compiler.parser import parse_recipe_file, parse_recipe_text
from recipe_workbench.compiler.sidecar import load_sidecar, save_sidecar
from recipe_workbench.explainer.generator import generate_explainer
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.picker import RootPicker
from recipe_workbench.graph.schema import Verb
from recipe_workbench.graph.scope_walker import compute_scope
from recipe_workbench.notion_ui.blocks import (
    build_explainer_blocks,
    build_hub_blocks,
    build_recipe_page_blocks,
)

load_dotenv()

STATE_FILE = Path(".notion_state.json")


def get_notion_client() -> Client:
    token = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing NOTION_API_KEY / NOTION_TOKEN in environment or .env. "
            "See README.md for safe hidden-input setup."
        )
    return Client(auth=token, notion_version="2022-06-28")


def _extract_plain_text(rich_text: List[dict]) -> str:
    return "".join(seg.get("plain_text", "") for seg in (rich_text or []))


def _append_blocks_batched(client: Client, block_id: str, blocks: List[dict], batch_size: int = 50) -> None:
    for i in range(0, len(blocks), batch_size):
        client.blocks.children.append(block_id=block_id, children=blocks[i : i + batch_size])


def _clear_page_children(client: Client, page_id: str) -> None:
    resp = client.blocks.children.list(block_id=page_id)
    for blk in resp.get("results", []):
        client.blocks.delete(block_id=blk["id"])


def _read_prose_and_disambig_from_page(client: Client, page_id: str) -> tuple[List[str], Optional[str]]:
    """Inspect Notion page blocks (and left column) for user prose edits or checked ambiguity checkboxes."""
    prose_lines: List[str] = []
    chosen_disambig: Optional[str] = None

    def scan_blocks(parent_id: str) -> None:
        nonlocal chosen_disambig
        res = client.blocks.children.list(block_id=parent_id)
        in_prose_section = False
        for blk in res.get("results", []):
            btype = blk.get("type")
            if btype == "column_list":
                cols = client.blocks.children.list(block_id=blk["id"]).get("results", [])
                if cols:
                    scan_blocks(cols[0]["id"])
                continue
            if btype == "heading_3":
                txt = _extract_plain_text(blk["heading_3"].get("rich_text", []))
                in_prose_section = "Recipe Prose" in txt
                continue
            if btype in {"divider", "toggle"}:
                in_prose_section = False
                continue
            if in_prose_section and btype == "paragraph":
                txt = _extract_plain_text(blk["paragraph"].get("rich_text", [])).strip()
                if txt:
                    prose_lines.append(txt)
            if btype == "to_do":
                td = blk["to_do"]
                if td.get("checked"):
                    txt = _extract_plain_text(td.get("rich_text", []))
                    m = re.match(r"^\s*\[(ore_instrument|ore_trade)\]", txt)
                    if m:
                        chosen_disambig = m.group(1)

    scan_blocks(page_id)
    return prose_lines, chosen_disambig


def sync_recipe_page(
    page_id: str,
    recipe_path: Path | str,
    graph: Optional[OREDomainGraph] = None,
    client: Optional[Client] = None,
) -> Dict[str, Any]:
    """Read edits/checkboxes from Notion, compile with ORE or Explainer, and refresh the Notion page."""
    c = client or get_notion_client()
    g = graph or OREDomainGraph()
    rpath = Path(recipe_path)
    parsed = parse_recipe_file(rpath)

    prose_lines, checked_disambig = _read_prose_and_disambig_from_page(c, page_id)
    sidecar = load_sidecar(rpath)

    if checked_disambig and sidecar.disambiguations.get("american_put") != checked_disambig:
        sidecar.disambiguations["american_put"] = checked_disambig
        save_sidecar(rpath, sidecar)

    # If user edited prose inside Notion, reflect it back into the recipe Markdown body
    if prose_lines and "\n\n".join(prose_lines).strip() != "\n\n".join(p.text for p in parsed.paragraphs).strip():
        header_part = parsed.title_line + "\n\n"
        if "---" in parsed.raw_text:
            parts = parsed.raw_text.split("---", 2)
            if len(parts) == 3:
                header_part = parts[0] + "---" + parts[1] + "---\n\n"
        rpath.write_text(header_part + "\n\n".join(prose_lines) + "\n", encoding="utf-8")
        parsed = parse_recipe_file(rpath)

    picker = RootPicker(g)
    root_id, verb, axis = picker.parse_title(parsed.title_line)

    if verb == Verb.EXPLAIN:
        scope = compute_scope(g, root_id, verb, axis, title=parsed.title_line.lstrip("#").strip())
        explainer = generate_explainer(scope, g, asof=parsed.asof)
        blocks = build_explainer_blocks(explainer)
        status_label = f"Grounded ({explainer.ungrounded_count} ungrounded)"
        working_model = "Explainer"
    else:
        compiled = compile_recipe(parsed, g, sidecar=sidecar)
        blocks = build_recipe_page_blocks(compiled)
        status_label = "Needs Answer" if compiled.halted_on_ambiguity else "Compiled ✓"
        working_model = compiled.working_model or (
            f"NPV {compiled.price_result.npv:.2f}" if compiled.price_result else "—"
        )

    _clear_page_children(c, page_id)
    _append_blocks_batched(c, page_id, blocks)

    # Update Notion database properties if page belongs to our database
    try:
        c.pages.update(
            page_id=page_id,
            properties={
                "Status": {"select": {"name": status_label}},
                "WorkingModel": {"rich_text": [{"type": "text", "text": {"content": working_model}}]},
            },
        )
    except Exception:
        pass

    return {"page_id": page_id, "status": status_label, "working_model": working_model}


def bootstrap_notion_workspace(
    parent_page_id: Optional[str] = None,
    recipes_dir: Path | str = Path("recipes"),
) -> Dict[str, Any]:
    """Create the Recipe Workbench Hub page, Recipes database, and 3 demo pages in Notion."""
    pid = parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID")
    if not pid:
        raise RuntimeError(
            "Missing NOTION_PARENT_PAGE_ID. Pass --parent-page-id or set it in .env."
        )

    c = get_notion_client()
    g = OREDomainGraph()
    picker = RootPicker(g)

    hub = c.pages.create(
        parent={"page_id": pid},
        icon={"type": "emoji", "emoji": "🧪"},
        properties={"title": {"title": [{"text": {"content": "Recipe Workbench — ORE Quant Lab"}}]}},
    )
    hub_id = hub["id"]

    roots = picker.filter_roots()
    verbs = picker.filter_verbs("EquityOption")
    axes = picker.filter_axes("EquityOption", Verb.COMPARE)
    _append_blocks_batched(c, hub_id, build_hub_blocks(roots, verbs, axes))

    db = c.databases.create(
        parent={"type": "page_id", "page_id": hub_id},
        title=[{"type": "text", "text": {"content": "Recipes"}}],
        properties={
            "Name": {"title": {}},
            "Product": {"select": {"options": [{"name": "American equity option", "color": "blue"}]}},
            "Verb": {
                "select": {
                    "options": [
                        {"name": "Compare", "color": "green"},
                        {"name": "Price", "color": "orange"},
                        {"name": "Explain", "color": "purple"},
                    ]
                }
            },
            "Axis": {"rich_text": {}},
            "AsOf": {"rich_text": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "Compiled ✓", "color": "green"},
                        {"name": "Needs Answer", "color": "orange"},
                        {"name": "Grounded (0 ungrounded)", "color": "blue"},
                    ]
                }
            },
            "WorkingModel": {"rich_text": {}},
            "Slug": {"rich_text": {}},
        },
    )
    db_id = db["id"]

    page_map: Dict[str, str] = {}
    for md_path in sorted(Path(recipes_dir).glob("*.md")):
        parsed = parse_recipe_file(md_path)
        root_id, verb, axis = picker.parse_title(parsed.title_line)
        title_clean = parsed.title_line.lstrip("#").strip()

        page = c.pages.create(
            parent={"database_id": db_id},
            icon={"type": "emoji", "emoji": "📄"},
            properties={
                "Name": {"title": [{"text": {"content": title_clean}}]},
                "Product": {"select": {"name": "American equity option"}},
                "Verb": {"select": {"name": verb.value}},
                "Axis": {"rich_text": [{"text": {"content": axis or "—"}}]},
                "AsOf": {"rich_text": [{"text": {"content": parsed.asof}}]},
                "Slug": {"rich_text": [{"text": {"content": md_path.stem}}]},
            },
        )
        sync_recipe_page(page["id"], md_path, graph=g, client=c)
        page_map[str(md_path)] = page["id"]

    state = {
        "hub_page_id": hub_id,
        "hub_url": hub.get("url", ""),
        "database_id": db_id,
        "pages": page_map,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def watch_notion(interval_sec: float = 3.0) -> None:
    """Poll Notion pages in .notion_state.json for checkbox selections or prose edits."""
    if not STATE_FILE.exists():
        raise RuntimeError("No .notion_state.json found. Run `recipe bootstrap-notion` first.")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    c = get_notion_client()
    g = OREDomainGraph()

    last_edited: Dict[str, str] = {}
    while True:
        for md_path, page_id in state.get("pages", {}).items():
            page_meta = c.pages.retrieve(page_id=page_id)
            edited_time = page_meta.get("last_edited_time", "")
            if last_edited.get(page_id) != edited_time:
                sync_recipe_page(page_id, md_path, graph=g, client=c)
                refreshed = c.pages.retrieve(page_id=page_id)
                last_edited[page_id] = refreshed.get("last_edited_time", edited_time)
        time.sleep(interval_sec)
