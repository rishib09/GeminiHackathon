"""CLI entrypoint for Recipe Workbench v1 (`recipe ...`)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from recipe_workbench.compiler.binder import compile_recipe
from recipe_workbench.compiler.linter import lint_recipe
from recipe_workbench.compiler.parser import parse_recipe_file
from recipe_workbench.explainer.generator import generate_explainer
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.picker import RootPicker
from recipe_workbench.graph.schema import Verb
from recipe_workbench.graph.scope_walker import compute_scope
from recipe_workbench.notion_ui.blocks import render_markdown_preview
from recipe_workbench.notion_ui.sync import (
    STATE_FILE,
    bootstrap_notion_workspace,
    check_notion_connection,
    sync_recipe_page,
    watch_notion,
)

app = typer.Typer(
    name="recipe",
    help="Recipe Workbench v1: Native Notion UI + ORE Python + Graphify + Gemini.",
    no_args_is_help=True,
)
console = Console()


@app.command("check-notion")
def cmd_check_notion(
    parent_page_id: Optional[str] = typer.Option(None, help="Override NOTION_PARENT_PAGE_ID"),
) -> None:
    """Verify Notion integration token and parent page access."""
    result = check_notion_connection(parent_page_id=parent_page_id)
    console.print_json(json.dumps(result))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("lint")
def cmd_lint(
    path: Path = typer.Argument(Path("recipes"), help="Recipe .md file or directory"),
) -> None:
    """Lint one recipe file or all recipes in a directory."""
    graph = OREDomainGraph()
    targets = sorted(path.glob("*.md")) if path.is_dir() else [path]
    if not targets:
        console.print(f"[red]No .md files found at {path}[/red]")
        raise typer.Exit(code=1)

    any_err = False
    for md_path in targets:
        report = lint_recipe(md_path, graph)
        status = "[green]OK[/green]" if report.ok else "[yellow]ISSUES[/yellow]"
        console.print(f"{status}  {md_path}")
        for issue in report.issues:
            color = "red" if issue.severity == "error" else "yellow"
            console.print(
                f"  [{color}]L{issue.line} {issue.code}[/{color}]: {issue.message}\n"
                f"    Fix: {issue.fix}"
            )
            if issue.severity == "error":
                any_err = True
    if any_err:
        raise typer.Exit(code=1)


@app.command("compile")
def cmd_compile(
    path: Path = typer.Argument(..., help="Path to a Markdown recipe file"),
    offline: bool = typer.Option(True, help="Use cached snapshot only without network calls"),
) -> None:
    """Compile a recipe locally with ORE and print a Markdown preview."""
    graph = OREDomainGraph()
    parsed = parse_recipe_file(path)
    picker = RootPicker(graph)
    try:
        root_id, verb, axis = picker.parse_title(parsed.title_line)
    except ValueError:
        root_id, verb, axis = "EquityOption", Verb.PRICE, None

    if verb == Verb.EXPLAIN:
        scope = compute_scope(graph, root_id, verb, axis, title=parsed.title_line.lstrip("#").strip())
        explainer = generate_explainer(scope, graph, asof=parsed.asof)
        console.print(f"[bold blue]{explainer.title}[/bold blue]")
        console.print(f"Summary: {explainer.summary_line} (ungrounded={explainer.ungrounded_count})")
        console.print(f"\n[bold]Layer 5 Verified Output:[/bold]\n{explainer.layer_5_verified_output}")
        return

    compiled = compile_recipe(parsed, graph, allow_network=not offline, use_llm_fallback=not offline)
    md = render_markdown_preview(compiled)
    console.print(Markdown(md))


@app.command("bootstrap-notion")
def cmd_bootstrap_notion(
    parent_page_id: Optional[str] = typer.Option(None, help="Override NOTION_PARENT_PAGE_ID"),
    recipes_dir: Path = typer.Option(Path("recipes"), help="Directory of Markdown recipes"),
    fresh: bool = typer.Option(False, help="Create a brand-new Hub & DB even if .notion_state.json exists"),
) -> None:
    """Create (or reuse) the Notion Hub, Recipes DB, and sync all recipes."""
    state = bootstrap_notion_workspace(
        parent_page_id=parent_page_id,
        recipes_dir=recipes_dir,
        fresh=fresh,
    )
    table = Table(title="Notion Workspace Ready")
    table.add_column("Recipe", style="cyan")
    table.add_column("Page ID", style="green")
    for md_path, page_id in state.get("pages", {}).items():
        table.add_row(md_path, page_id)
    console.print(f"[bold green]Hub URL:[/bold green] {state.get('hub_url') or state['hub_page_id']}")
    console.print(table)


@app.command("sync")
def cmd_sync(
    recipe_path: Optional[Path] = typer.Argument(None, help="Specific recipe .md to sync (default: all)"),
) -> None:
    """Sync one or all tracked Notion pages from .notion_state.json."""
    if not STATE_FILE.exists():
        console.print("[red]No .notion_state.json found. Run `recipe bootstrap-notion` first.[/red]")
        raise typer.Exit(code=1)

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    pages: dict[str, str] = state.get("pages", {})
    graph = OREDomainGraph()

    targets = (
        {str(recipe_path): pages[str(recipe_path)]}
        if recipe_path and str(recipe_path) in pages
        else pages
    )
    for md_path, page_id in targets.items():
        res = sync_recipe_page(page_id, md_path, graph=graph)
        console.print(f"[green]Synced[/green] {md_path} -> {res['status']} ({res['working_model']})")


@app.command("watch")
def cmd_watch(
    interval: float = typer.Option(3.0, help="Polling interval in seconds"),
    debounce: float = typer.Option(4.0, help="Seconds after last edit before syncing"),
) -> None:
    """Poll Notion pages for prose edits or ambiguity checkbox selections."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    console.print(f"[bold cyan]Watching Notion pages every {interval}s (debounce {debounce}s)...[/bold cyan]")
    watch_notion(interval_sec=interval, debounce_sec=debounce)


@app.command("serve")
def cmd_serve(
    host: str = typer.Option("127.0.0.1", help="Host to bind FastAPI server"),
    port: int = typer.Option(8000, help="Port to bind FastAPI server"),
) -> None:
    """Start the FastAPI backend for the React/TipTap Recipe Workbench UI."""
    import uvicorn

    console.print(f"[bold green]Starting Recipe Workbench API at http://{host}:{port}[/bold green]")
    uvicorn.run("recipe_workbench.api.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
