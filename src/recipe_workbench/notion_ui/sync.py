"""Live Notion.so API synchronization, workspace bootstrap, and polling watcher."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

import yaml
from dotenv import load_dotenv
from notion_client import APIResponseError, Client

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
    paragraph,
    rt,
)

load_dotenv()

logger = logging.getLogger(__name__)
STATE_FILE = Path(".notion_state.json")
T = TypeVar("T")


def _with_retry(fn: Callable[[], T], max_attempts: int = 4, base_delay: float = 1.0) -> T:
    """Retry Notion API calls on 429 rate limits or transient 5xx errors."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except APIResponseError as exc:
            status = getattr(exc, "status", None)
            if status in {429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                delay = base_delay * (2**attempt)
                logger.warning("Notion API %s; retrying in %.1fs...", status, delay)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("Unreachable retry loop exit")


def _list_all_children(client: Client, block_id: str) -> List[Dict[str, Any]]:
    """Fetch all child blocks with cursor pagination."""
    results: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"block_id": block_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = _with_retry(lambda: client.blocks.children.list(**kwargs))
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def normalize_notion_id(raw: str) -> str:
    """Extract a canonical 32-char hyphenated UUID from a Notion URL or raw page ID."""
    cleaned = raw.strip()
    # Strip query string / fragment if a full Notion URL was pasted
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0]
    m = re.search(
        r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})",
        cleaned,
    )
    if not m:
        raise ValueError(
            f"Could not extract a valid Notion 32-char hex ID from: {raw!r}. "
            "Paste either the full Notion page URL or the 32-character page ID."
        )
    hex32 = m.group(1).replace("-", "").lower()
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


def get_notion_client() -> Client:
    load_dotenv(override=False)
    token = (os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN") or "").strip()
    if not token or token == "ntn_your_internal_integration_secret_here":
        raise RuntimeError(
            "Missing NOTION_API_KEY / NOTION_TOKEN in environment or .env. "
            "Copy .env.example to .env and add your Notion Internal Integration secret."
        )
    return Client(auth=token, notion_version="2022-06-28")


def check_notion_connection(parent_page_id: Optional[str] = None) -> Dict[str, Any]:
    """Verify Notion Internal Integration auth and access to the target parent page."""
    load_dotenv(override=False)
    c = get_notion_client()

    me = c.users.me()
    bot_name = me.get("name") or "Unknown Integration"
    bot_owner = me.get("bot", {}).get("workspace_name") or "Unknown Workspace"

    raw_pid = (parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID") or "").strip()
    if not raw_pid or "your-parent-page-id" in raw_pid:
        return {
            "ok": False,
            "auth_ok": True,
            "parent_ok": False,
            "bot_name": bot_name,
            "workspace_name": bot_owner,
            "error": (
                "NOTION_PARENT_PAGE_ID is not set in .env. "
                "Paste your Notion parent page URL or ID into .env."
            ),
        }

    pid = normalize_notion_id(raw_pid)
    try:
        page = c.pages.retrieve(page_id=pid)
    except Exception as exc:
        return {
            "ok": False,
            "auth_ok": True,
            "parent_ok": False,
            "bot_name": bot_name,
            "workspace_name": bot_owner,
            "parent_page_id": pid,
            "error": (
                f"Cannot access parent page {pid}: {exc}. "
                f"In Notion, open that page -> click '...' -> Connections -> add '{bot_name}'."
            ),
        }

    title = ""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            title = _extract_plain_text(prop.get("title", []))
            break

    return {
        "ok": True,
        "auth_ok": True,
        "parent_ok": True,
        "bot_name": bot_name,
        "workspace_name": bot_owner,
        "parent_page_id": pid,
        "parent_page_title": title or "(Untitled)",
        "parent_page_url": page.get("url", ""),
    }


def _extract_plain_text(rich_text: List[dict]) -> str:
    parts: List[str] = []
    for seg in rich_text or []:
        if "plain_text" in seg:
            parts.append(seg["plain_text"])
        else:
            parts.append(seg.get("text", {}).get("content", ""))
    return "".join(parts)


def _has_nested_children(block: dict) -> bool:
    btype = block.get("type", "")
    body = block.get(btype, {})
    return isinstance(body, dict) and bool(body.get("children"))


def _append_column_list(client: Client, block_id: str, col_list_block: dict, batch_size: int = 50) -> None:
    """Append a column_list in two passes to stay under Notion's 2-level nested children limit."""
    cols = col_list_block.get("column_list", {}).get("children", [])
    seed_columns: List[dict] = []
    remaining_per_col: List[List[dict]] = []

    for col in cols:
        col_children = list(col.get("column", {}).get("children", []))
        if col_children and not _has_nested_children(col_children[0]):
            seed_columns.append(
                {"object": "block", "type": "column", "column": {"children": [col_children[0]]}}
            )
            remaining_per_col.append(col_children[1:])
        else:
            seed_columns.append(
                {"object": "block", "type": "column", "column": {"children": [paragraph(rt(" "))]}}
            )
            remaining_per_col.append(col_children)

    resp = _with_retry(
        lambda: client.blocks.children.append(
            block_id=block_id,
            children=[{"object": "block", "type": "column_list", "column_list": {"children": seed_columns}}],
        )
    )
    col_list_id = resp["results"][0]["id"]
    created_cols = _list_all_children(client, col_list_id)
    for created_col, rem in zip(created_cols, remaining_per_col):
        if rem:
            _append_blocks_batched(client, created_col["id"], rem, batch_size=batch_size)


def _append_blocks_batched(client: Client, block_id: str, blocks: List[dict], batch_size: int = 50) -> None:
    buf: List[dict] = []

    def flush() -> None:
        nonlocal buf
        for i in range(0, len(buf), batch_size):
            chunk = buf[i : i + batch_size]
            _with_retry(lambda c=chunk: client.blocks.children.append(block_id=block_id, children=c))
        buf = []

    for blk in blocks:
        if blk.get("type") == "column_list":
            flush()
            _append_column_list(client, block_id, blk, batch_size=batch_size)
        else:
            buf.append(blk)
    flush()


def _clear_page_children(client: Client, page_id: str) -> None:
    for blk in _list_all_children(client, page_id):
        _with_retry(lambda bid=blk["id"]: client.blocks.delete(block_id=bid))


def _extract_controls_from_blocks(
    blocks: List[Dict[str, Any]],
) -> tuple[List[str], Optional[str], List[str], List[str], bool]:
    """Extract (prose_lines, chosen_disambig, checked_models, checked_calcs, has_structured_layout)."""
    prose_lines: List[str] = []
    fallback_paragraphs: List[str] = []
    chosen_disambig: Optional[str] = None
    checked_models: List[str] = []
    checked_calcs: List[str] = []
    in_prose_section = False
    has_structured_layout = False

    for blk in blocks:
        btype = blk.get("type")
        if btype == "heading_3":
            txt = _extract_plain_text(blk["heading_3"].get("rich_text", []))
            if "Recipe Prose" in txt:
                in_prose_section = True
                has_structured_layout = True
            else:
                in_prose_section = False
            continue
        if btype in {"divider", "toggle"}:
            in_prose_section = False
            continue
        if btype == "paragraph":
            txt = _extract_plain_text(blk["paragraph"].get("rich_text", [])).strip()
            if txt:
                if in_prose_section:
                    prose_lines.append(txt)
                elif not has_structured_layout:
                    fallback_paragraphs.append(txt)
        if btype == "to_do":
            td = blk["to_do"]
            txt = _extract_plain_text(td.get("rich_text", []))
            if td.get("checked"):
                m_dis = re.match(r"^\s*\[(ore_instrument|ore_trade)\]", txt)
                if m_dis:
                    chosen_disambig = m_dis.group(1)
                m_mod = re.match(r"^\s*\[model:([a-zA-Z0-9_]+)\]", txt)
                if m_mod:
                    checked_models.append(m_mod.group(1))
                m_calc = re.match(r"^\s*\[calc:([a-zA-Z0-9_]+)\]", txt)
                if m_calc:
                    checked_calcs.append(m_calc.group(1))

    final_prose = prose_lines if has_structured_layout else fallback_paragraphs
    return final_prose, chosen_disambig, checked_models, checked_calcs, has_structured_layout


def _read_page_controls(
    client: Client, page_id: str
) -> tuple[List[str], Optional[str], List[str], List[str], bool]:
    """Inspect Notion page blocks (and left column) for prose edits & interactive checkboxes."""
    top_blocks = _list_all_children(client, page_id)
    for blk in top_blocks:
        if blk.get("type") == "column_list":
            cols = _list_all_children(client, blk["id"])
            if cols:
                left_blocks = _list_all_children(client, cols[0]["id"])
                return _extract_controls_from_blocks(left_blocks)
    return _extract_controls_from_blocks(top_blocks)


def _read_prose_and_disambig_from_page(client: Client, page_id: str) -> tuple[List[str], Optional[str]]:
    """Backward-compatible wrapper returning (prose_lines, chosen_disambig)."""
    prose_lines, chosen_disambig, _, _, _ = _read_page_controls(client, page_id)
    return prose_lines, chosen_disambig


def _reconstruct_recipe_header(title_line: str, front_matter: Dict[str, Any]) -> str:
    """Safely rebuild title + YAML front-matter without splitting raw prose on '---'."""
    if not front_matter:
        return f"{title_line}\n\n"
    fm_yaml = yaml.safe_dump(front_matter, sort_keys=False).strip()
    return f"{title_line}\n\n---\n{fm_yaml}\n---\n\n"


def _set_page_status(client: Client, page_id: str, status_label: str, working_model: Optional[str] = None) -> None:
    props: Dict[str, Any] = {"Status": {"select": {"name": status_label}}}
    if working_model is not None:
        props["WorkingModel"] = {"rich_text": [{"type": "text", "text": {"content": working_model}}]}
    try:
        _with_retry(lambda: client.pages.update(page_id=page_id, properties=props))
    except Exception:
        pass


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

    prose_lines, checked_disambig, checked_models, checked_calcs, has_structured = _read_page_controls(c, page_id)
    sidecar = load_sidecar(rpath)
    sidecar_dirty = False

    if checked_disambig and sidecar.disambiguations.get("american_put") != checked_disambig:
        sidecar.disambiguations["american_put"] = checked_disambig
        sidecar_dirty = True

    if has_structured:
        if checked_models and sidecar.selected_models != checked_models:
            sidecar.selected_models = checked_models
            sidecar_dirty = True
        if checked_calcs and sidecar.selected_calculators != checked_calcs:
            sidecar.selected_calculators = checked_calcs
            sidecar_dirty = True

    # If user edited prose inside Notion, reflect it back into the recipe Markdown body
    if prose_lines and "\n\n".join(prose_lines).strip() != "\n\n".join(p.text for p in parsed.paragraphs).strip():
        header_part = _reconstruct_recipe_header(parsed.title_line, parsed.front_matter)
        rpath.write_text(header_part + "\n\n".join(prose_lines) + "\n", encoding="utf-8")
        parsed = parse_recipe_file(rpath)
        # When prose changes substantially, reset manual calculator override so keyword detector adapts freshly
        if not checked_calcs:
            sidecar.selected_calculators = []
            sidecar_dirty = True

    if sidecar_dirty:
        save_sidecar(rpath, sidecar)

    _set_page_status(c, page_id, "Compiling…")

    picker = RootPicker(g)
    try:
        root_id, verb, axis = picker.parse_title(parsed.title_line)
    except ValueError:
        root_id, verb, axis = "EquityOption", Verb.PRICE, None

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
    _set_page_status(c, page_id, status_label, working_model)

    return {"page_id": page_id, "status": status_label, "working_model": working_model}


def _create_recipe_db_page(
    client: Client,
    db_id: str,
    md_path: Path,
    picker: RootPicker,
) -> str:
    parsed = parse_recipe_file(md_path)
    try:
        _, verb, axis = picker.parse_title(parsed.title_line)
    except ValueError:
        verb, axis = Verb.COMPARE, "keyword_workbench"
    title_clean = parsed.title_line.lstrip("#").strip()

    page = _with_retry(
        lambda: client.pages.create(
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
    )
    return page["id"]


def bootstrap_notion_workspace(
    parent_page_id: Optional[str] = None,
    recipes_dir: Path | str = Path("recipes"),
    fresh: bool = False,
) -> Dict[str, Any]:
    """Create (or idempotently reuse) the Recipe Workbench Hub page, Recipes database, and demo pages."""
    raw_pid = parent_page_id or os.getenv("NOTION_PARENT_PAGE_ID")
    if not raw_pid:
        raise RuntimeError(
            "Missing NOTION_PARENT_PAGE_ID. Pass --parent-page-id or set it in .env."
        )
    pid = normalize_notion_id(raw_pid)

    md_files = sorted(Path(recipes_dir).glob("*.md"))
    if not md_files:
        raise RuntimeError(f"No Markdown recipe files (*.md) found in {Path(recipes_dir).resolve()}.")

    c = get_notion_client()
    g = OREDomainGraph()
    picker = RootPicker(g)

    existing_state: Optional[Dict[str, Any]] = None
    if not fresh and STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            hub_check = _with_retry(lambda: c.pages.retrieve(page_id=loaded["hub_page_id"]))
            if not hub_check.get("archived"):
                existing_state = loaded
        except Exception:
            existing_state = None

    if existing_state is not None:
        hub_id = existing_state["hub_page_id"]
        hub_url = existing_state.get("hub_url", "")
        db_id = existing_state["database_id"]
        page_map: Dict[str, str] = dict(existing_state.get("pages", {}))
    else:
        hub = _with_retry(
            lambda: c.pages.create(
                parent={"page_id": pid},
                icon={"type": "emoji", "emoji": "🧪"},
                properties={"title": {"title": [{"text": {"content": "Recipe Workbench — ORE Quant Lab"}}]}},
            )
        )
        hub_id = hub["id"]
        hub_url = hub.get("url", "")

        roots = picker.filter_roots()
        verbs = picker.filter_verbs("EquityOption")
        axes = picker.filter_axes("EquityOption", Verb.COMPARE)
        _append_blocks_batched(c, hub_id, build_hub_blocks(roots, verbs, axes))

        db = _with_retry(
            lambda: c.request(
                path="databases",
                method="POST",
                body={
                    "parent": {"type": "page_id", "page_id": hub_id},
                    "is_inline": True,
                    "title": [{"type": "text", "text": {"content": "Recipes"}}],
                    "properties": {
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
                                    {"name": "Compiling…", "color": "yellow"},
                                    {"name": "Compiled ✓", "color": "green"},
                                    {"name": "Needs Answer", "color": "orange"},
                                    {"name": "Grounded (0 ungrounded)", "color": "blue"},
                                ]
                            }
                        },
                        "WorkingModel": {"rich_text": {}},
                        "Slug": {"rich_text": {}},
                    },
                },
            )
        )
        db_id = db["id"]
        page_map = {}

    for md_path in md_files:
        key = str(md_path)
        page_id = page_map.get(key)
        if page_id:
            try:
                meta = _with_retry(lambda pid_=page_id: c.pages.retrieve(page_id=pid_))
                if meta.get("archived"):
                    page_id = None
            except Exception:
                page_id = None

        if not page_id:
            page_id = _create_recipe_db_page(c, db_id, md_path, picker)
            page_map[key] = page_id

        sync_recipe_page(page_id, md_path, graph=g, client=c)

    state = {
        "hub_page_id": hub_id,
        "hub_url": hub_url,
        "database_id": db_id,
        "pages": page_map,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _seconds_since_iso(iso_ts: str) -> float:
    if not iso_ts:
        return 999.0
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 999.0


def _find_left_column_id(client: Client, page_id: str) -> Optional[str]:
    """Return the block ID of the left column inside the page's column_list, if present."""
    for blk in _list_all_children(client, page_id):
        if blk.get("type") == "column_list":
            cols = _list_all_children(client, blk["id"])
            if cols:
                return cols[0]["id"]
    return None


PageFingerprint = tuple[tuple[str, ...], Optional[str], tuple[str, ...], tuple[str, ...]]


def _read_page_fingerprint(
    client: Client, page_id: str, left_col_cache: Dict[str, Optional[str]]
) -> PageFingerprint:
    """Read live (prose, disambig, models, calcs) in 1 API call using cached left-column ID."""
    if page_id not in left_col_cache:
        left_col_cache[page_id] = _find_left_column_id(client, page_id)
    target_id = left_col_cache[page_id] or page_id
    blocks = _list_all_children(client, target_id)
    prose, disambig, models, calcs, _ = _extract_controls_from_blocks(blocks)
    return tuple(prose), disambig, tuple(models), tuple(calcs)


def _discover_new_db_pages(
    client: Client,
    state: Dict[str, Any],
    recipes_dir: Path = Path("recipes"),
) -> List[tuple[str, str]]:
    """Query the Recipes database for newly created user pages and materialize local .md recipes."""
    db_id = state.get("database_id")
    if not db_id:
        return []
    known_ids = set(state.get("pages", {}).values())
    try:
        resp = _with_retry(
            lambda: client.request(
                path=f"databases/{db_id}/query",
                method="POST",
                body={"page_size": 50},
            )
        )
    except Exception as exc:
        logger.debug("Database query skipped: %s", exc)
        return []

    discovered: List[tuple[str, str]] = []
    for page in resp.get("results", []):
        pid = page.get("id")
        if not pid or pid in known_ids or page.get("archived"):
            continue
        title = ""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = _extract_plain_text(prop.get("title", [])).strip()
                break
        if not title:
            continue

        # Check if user has typed any prose paragraphs yet
        prose_lines, _, _, _, _ = _read_page_controls(client, pid)
        if not prose_lines:
            continue

        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or f"notion_{pid[:8]}"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        md_path = recipes_dir / f"user_{slug}.md"
        header = _reconstruct_recipe_header(f"# {title}", {"asof": "2026-09-10", "symbol": "SPY"})
        md_path.write_text(header + "\n\n".join(prose_lines) + "\n", encoding="utf-8")

        key = str(md_path)
        state.setdefault("pages", {})[key] = pid
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        discovered.append((key, pid))
        logger.info("Discovered new Notion page %r -> %s", title, md_path)

    return discovered


def watch_notion(interval_sec: float = 3.0, debounce_sec: float = 4.0) -> None:
    """Poll Notion pages in .notion_state.json (and new DB pages) for prose edits or checkbox clicks."""
    if not STATE_FILE.exists():
        raise RuntimeError("No .notion_state.json found. Run `recipe bootstrap-notion` first.")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    c = get_notion_client()
    g = OREDomainGraph()

    left_col_cache: Dict[str, Optional[str]] = {}
    last_state: Dict[str, PageFingerprint] = {}
    pending_since: Dict[str, tuple[PageFingerprint, float]] = {}

    # Seed initial state on startup so we don't immediately rewrite every page
    for md_path, page_id in state.get("pages", {}).items():
        try:
            last_state[page_id] = _read_page_fingerprint(c, page_id, left_col_cache)
        except Exception as exc:
            logger.warning("Failed to seed page %s (%s): %s", page_id, md_path, exc)

    logger.info(
        "Ready — monitoring %d recipe page(s) + Recipes DB for new pages, prose edits, or model/calculator clicks.",
        len(last_state),
    )

    poll_count = 0
    while True:
        now = time.monotonic()
        poll_count += 1

        # Every 3 polls (~9s), check if user created a brand-new page in the Notion Recipes database
        if poll_count % 3 == 1:
            for new_md, new_pid in _discover_new_db_pages(c, state):
                try:
                    res = sync_recipe_page(new_pid, new_md, graph=g, client=c)
                    left_col_cache.pop(new_pid, None)
                    last_state[new_pid] = _read_page_fingerprint(c, new_pid, left_col_cache)
                    logger.info("Auto-compiled new Notion page %s -> %s (%s)", new_md, res["status"], res["working_model"])
                except Exception as exc:
                    logger.warning("Failed to compile discovered page %s: %s", new_md, exc)

        for md_path, page_id in list(state.get("pages", {}).items()):
            try:
                current = _read_page_fingerprint(c, page_id, left_col_cache)
                if current == last_state.get(page_id):
                    pending_since.pop(page_id, None)
                    continue

                # Trigger immediately on any checkbox click (disambig, model, or calculator); debounce prose typing
                prev = last_state.get(page_id, ((), None, (), ()))
                is_checkbox_click = (
                    (current[1] != prev[1] and current[1] is not None)
                    or (current[2] != prev[2])
                    or (current[3] != prev[3])
                )

                if not is_checkbox_click:
                    pend = pending_since.get(page_id)
                    if pend is None or pend[0] != current:
                        pending_since[page_id] = (current, now)
                        continue
                    if (now - pend[1]) < debounce_sec:
                        continue

                pending_since.pop(page_id, None)
                logger.info(
                    "Detected change on %s (disambig=%s, models=%s, calcs=%s); syncing...",
                    md_path,
                    current[1],
                    current[2],
                    current[3],
                )
                res = sync_recipe_page(page_id, md_path, graph=g, client=c)
                # Refresh cached left column ID because sync recreated the column_list
                left_col_cache.pop(page_id, None)
                last_state[page_id] = _read_page_fingerprint(c, page_id, left_col_cache)
                logger.info("Synced %s -> %s (%s)", md_path, res["status"], res["working_model"])
            except Exception as exc:
                left_col_cache.pop(page_id, None)
                logger.warning("Error watching %s (%s): %s", md_path, page_id, exc)
        time.sleep(interval_sec)


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        try:
            result = check_notion_connection()
            print(json.dumps(result, indent=2))
            sys.exit(0 if result.get("ok") else 1)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            sys.exit(1)
    else:
        print("Usage: python -m recipe_workbench.notion_ui.sync --check")
