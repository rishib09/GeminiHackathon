"""Recipe linter enforcing FR-08, FR-10, FR-11, FR-29, and FR-30."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

from recipe_workbench.compiler.parser import parse_recipe_file
from recipe_workbench.compiler.sidecar import load_sidecar
from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.picker import RootPicker
from recipe_workbench.graph.schema import Verb
from recipe_workbench.graph.scope_walker import compute_scope


class LintIssue(BaseModel):
    line: int
    severity: str  # "error" | "warning"
    code: str
    message: str
    fix: str


class LintReport(BaseModel):
    path: str
    ok: bool
    issues: List[LintIssue] = Field(default_factory=list)


def lint_recipe(recipe_path: Path | str, graph: OREDomainGraph) -> LintReport:
    issues: List[LintIssue] = []
    p = Path(recipe_path)
    if not p.exists():
        return LintReport(
            path=str(p),
            ok=False,
            issues=[
                LintIssue(
                    line=1,
                    severity="error",
                    code="FILE_NOT_FOUND",
                    message=f"Recipe file not found: {p}",
                    fix="Create the Markdown recipe file or check the path.",
                )
            ],
        )

    try:
        parsed = parse_recipe_file(p)
    except Exception as exc:
        return LintReport(
            path=str(p),
            ok=False,
            issues=[
                LintIssue(
                    line=1,
                    severity="error",
                    code="INVALID_RECIPE_SYNTAX",
                    message=str(exc),
                    fix="Put a valid canonical title on line 1 (e.g. 'Compare volatility models on an American equity option').",
                )
            ],
        )

    picker = RootPicker(graph)
    try:
        root_id, verb, axis = picker.parse_title(parsed.title_line)
    except ValueError as exc:
        suggestions = [h.name for h in picker.filter_roots()]
        return LintReport(
            path=str(p),
            ok=False,
            issues=[
                LintIssue(
                    line=1,
                    severity="error",
                    code="INVALID_TITLE",
                    message=str(exc),
                    fix=f"Use '<Verb> <Object> [varying <Axis>]'. Supported v1 roots: {suggestions}.",
                )
            ],
        )

    sidecar = load_sidecar(p)
    disambig = sidecar.disambiguations.get("american_put")

    try:
        scope = compute_scope(
            graph=graph,
            root_id=root_id,
            verb=verb,
            axis=axis,
            title=parsed.title_line.lstrip("#").strip(),
            uses=parsed.uses,
            disambiguation=disambig,
        )
    except ValueError as exc:
        issues.append(
            LintIssue(
                line=2,
                severity="error",
                code="INVALID_USES_OR_SCOPE",
                message=str(exc),
                fix="Remove disallowed formula or unknown node from front-matter `uses:`.",
            )
        )
        return LintReport(path=str(p), ok=False, issues=issues)

    # Check verb-gated formulas (FR-08 / FR-29)
    compare_only_tokens = ["chain_rmse", "chain rmse", "fit to the chain", "rank("]
    price_or_compare_tokens = ["price(", "cashflows(", "delta("]
    for para in parsed.paragraphs:
        low = para.text.lower()
        if verb == Verb.PRICE:
            for tok in compare_only_tokens:
                if tok in low:
                    issues.append(
                        LintIssue(
                            line=para.line_number,
                            severity="error",
                            code="FORMULA_NOT_PERMITTED_BY_VERB",
                            message=f"Formula {tok!r} is not permitted for verb '{verb.value}' (Compare only).",
                            fix="Change the recipe title verb to 'Compare' or remove the comparison formula.",
                        )
                    )
        elif verb == Verb.EXPLAIN:
            for tok in [*compare_only_tokens, *price_or_compare_tokens]:
                if tok in low:
                    issues.append(
                        LintIssue(
                            line=para.line_number,
                            severity="error",
                            code="FORMULA_NOT_PERMITTED_BY_VERB",
                            message=f"Formula {tok!r} is not permitted for verb 'Explain' (FR-08).",
                            fix="Change the recipe verb to 'Price' or 'Compare', or keep Explain prose descriptive.",
                        )
                    )

        # Check out-of-scope sibling references (FR-09 / FR-29)
        for sib in scope.siblings:
            for alias in [sib.label, *sib.aliases]:
                if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", para.text, re.IGNORECASE):
                    issues.append(
                        LintIssue(
                            line=para.line_number,
                            severity="error",
                            code="OUT_OF_SCOPE_REFERENCE",
                            message=f"Reference to {alias!r} ({sib.id}) is outside the current scope.",
                            fix=f"Add '{sib.id}' under front-matter `uses:` or widen scope from the right rail.",
                        )
                    )
                    break

        # Check unanswered ambiguity (FR-15 / FR-29)
        if re.search(r"\bamerican\s+put\b", para.text, re.IGNORECASE) and disambig is None:
            issues.append(
                LintIssue(
                    line=para.line_number,
                    severity="error",
                    code="UNANSWERED_AMBIGUITY",
                    message="'American put' matches both OREInProcessInstrument and ORETradeXML.",
                    fix="Choose an option in Notion (or record disambiguation in the .bindings.json sidecar).",
                )
            )

    has_errors = any(i.severity == "error" for i in issues)
    return LintReport(path=str(p), ok=not has_errors, issues=issues)
