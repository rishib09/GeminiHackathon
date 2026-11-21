"""Sidecar persistence (<slug>.bindings.json) for confirmed bindings & ambiguity choices (FR-15, FR-17)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ConfirmedBinding(BaseModel):
    span_hash: str
    phrase: str
    target_id: str
    source: Literal["scope_match", "literal", "definition", "disambiguation", "llm_confirmed"]
    confirmed_at: str


class SidecarData(BaseModel):
    slug: str
    bindings: Dict[str, ConfirmedBinding] = Field(default_factory=dict)
    disambiguations: Dict[str, str] = Field(default_factory=dict)
    selected_models: List[str] = Field(default_factory=list)
    selected_calculators: List[str] = Field(default_factory=list)

    def get_by_phrase(self, phrase: str) -> Optional[ConfirmedBinding]:
        h = hash_span(phrase)
        if h in self.bindings:
            return self.bindings[h]
        for b in self.bindings.values():
            if b.phrase.lower() == phrase.lower():
                return b
        return None


def hash_span(phrase: str) -> str:
    return hashlib.sha256(phrase.strip().lower().encode("utf-8")).hexdigest()[:12]


def sidecar_path_for(recipe_path: Path | str) -> Path:
    p = Path(recipe_path)
    return p.with_suffix(".bindings.json")


def load_sidecar(recipe_path: Path | str, slug: Optional[str] = None) -> SidecarData:
    path = sidecar_path_for(recipe_path)
    if not path.exists():
        return SidecarData(slug=slug or Path(recipe_path).stem)
    return SidecarData.model_validate_json(path.read_text(encoding="utf-8"))


def save_sidecar(recipe_path: Path | str, sidecar: SidecarData) -> Path:
    path = sidecar_path_for(recipe_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sidecar.model_dump(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def record_binding(
    sidecar: SidecarData,
    phrase: str,
    target_id: str,
    source: Literal["scope_match", "literal", "definition", "disambiguation", "llm_confirmed"],
) -> ConfirmedBinding:
    h = hash_span(phrase)
    binding = ConfirmedBinding(
        span_hash=h,
        phrase=phrase,
        target_id=target_id,
        source=source,
        confirmed_at=datetime.now(timezone.utc).isoformat(),
    )
    sidecar.bindings[h] = binding
    return binding
