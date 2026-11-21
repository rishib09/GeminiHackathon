"""Markdown recipe parser (FR-11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from recipe_workbench.engine.schemas import ConventionsSpec


class RecipeParagraph(BaseModel):
    index: int
    line_number: int
    text: str


class ParsedRecipe(BaseModel):
    path: str
    title_line: str
    title_line_number: int
    asof: str = "2026-09-10"
    symbol: str = "SPY"
    uses: List[str] = Field(default_factory=list)
    conventions: ConventionsSpec = Field(default_factory=ConventionsSpec)
    front_matter: Dict[str, Any] = Field(default_factory=dict)
    paragraphs: List[RecipeParagraph] = Field(default_factory=list)
    raw_text: str = ""


def parse_recipe_text(text: str, path: str = "<memory>") -> ParsedRecipe:
    lines = text.splitlines()
    if not lines:
        raise ValueError("Recipe file is empty; line 1 must be a canonical title (FR-11).")

    # Line 1 must be the title
    title_line = lines[0].strip()
    if not title_line:
        raise ValueError("Line 1 cannot be blank; line 1 must be the recipe title (FR-11).")

    body_start_idx = 1
    front_matter: Dict[str, Any] = {}

    # Optional YAML front matter starting right after line 1
    idx = 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].strip() == "---":
        end_idx = None
        for j in range(idx + 1, len(lines)):
            if lines[j].strip() == "---":
                end_idx = j
                break
        if end_idx is None:
            raise ValueError(f"Unclosed YAML front matter starting at line {idx + 1}.")
        fm_raw = "\n".join(lines[idx + 1 : end_idx])
        loaded = yaml.safe_load(fm_raw) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Front matter at line {idx + 1} must be a YAML mapping.")
        front_matter = loaded
        body_start_idx = end_idx + 1

    asof = str(front_matter.get("asof", "2026-09-10"))
    symbol = str(front_matter.get("symbol", "SPY")).upper()
    uses_raw = front_matter.get("uses", [])
    uses = [str(u) for u in uses_raw] if isinstance(uses_raw, list) else []

    conv_dict = front_matter.get("conventions", {})
    conventions = ConventionsSpec.model_validate(conv_dict) if isinstance(conv_dict, dict) else ConventionsSpec()

    # Split remaining lines into paragraphs separated by blank lines
    paragraphs: List[RecipeParagraph] = []
    buf: List[str] = []
    start_line = body_start_idx + 1
    for line_i in range(body_start_idx, len(lines)):
        ln = lines[line_i]
        if not ln.strip():
            if buf:
                paragraphs.append(
                    RecipeParagraph(
                        index=len(paragraphs),
                        line_number=start_line,
                        text=" ".join(s.strip() for s in buf),
                    )
                )
                buf = []
            start_line = line_i + 2
        else:
            if not buf:
                start_line = line_i + 1
            buf.append(ln)
    if buf:
        paragraphs.append(
            RecipeParagraph(
                index=len(paragraphs),
                line_number=start_line,
                text=" ".join(s.strip() for s in buf),
            )
        )

    return ParsedRecipe(
        path=str(path),
        title_line=title_line,
        title_line_number=1,
        asof=asof,
        symbol=symbol,
        uses=uses,
        conventions=conventions,
        front_matter=front_matter,
        paragraphs=paragraphs,
        raw_text=text,
    )


def parse_recipe_file(path: Path | str) -> ParsedRecipe:
    p = Path(path)
    return parse_recipe_text(p.read_text(encoding="utf-8"), path=str(p))
