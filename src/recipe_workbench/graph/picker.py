"""Guided 3-step Root Picker and canonical title parser (FR-01 to FR-05)."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from recipe_workbench.graph.domain_graph import OREDomainGraph
from recipe_workbench.graph.schema import Axis, CandidateHit, NodeCategory, Verb
from recipe_workbench.graph.scope_walker import canonical_title, slugify


class RootPicker:
    """Filters valid roots, verbs, and axes strictly from the ORE knowledge graph."""

    def __init__(self, graph: OREDomainGraph) -> None:
        self.graph = graph

    def filter_roots(self, query: str = "") -> List[CandidateHit]:
        q = query.strip().lower()
        hits: List[CandidateHit] = []
        for node in self.graph.nodes.values():
            if node.category != NodeCategory.ROOT:
                continue
            haystack = " ".join([node.id, node.label, node.description, *node.aliases]).lower()
            if not q or q in haystack:
                hits.append(
                    CandidateHit(
                        id=node.id,
                        name=node.label,
                        why=node.why or node.description,
                        source_layer=f"{node.layer} · trade root",
                        kind="root",
                    )
                )
        return hits

    def filter_verbs(self, root_id: str, query: str = "") -> List[CandidateHit]:
        if root_id not in self.graph.nodes:
            return []
        q = query.strip().lower()
        verbs = [
            (Verb.EXPLAIN, "Walk the ORE/QuantLib code path and generate a 5-layer explainer"),
            (Verb.PRICE, "Price one contract and emit fair value, delta, and cashflows"),
            (Verb.COMPARE, "Compare alternatives along one axis (volatility model or pricing engine)"),
        ]
        return [
            CandidateHit(
                id=v.value,
                name=v.value,
                why=desc,
                source_layer="ORE · action",
                kind="verb",
            )
            for v, desc in verbs
            if not q or q in v.value.lower() or q in desc.lower()
        ]

    def filter_axes(self, root_id: str, verb: Verb, query: str = "") -> List[CandidateHit]:
        if verb != Verb.COMPARE or root_id not in self.graph.nodes:
            return []
        q = query.strip().lower()
        axes = [
            (
                Axis.VOLATILITY_MODEL.value,
                "Volatility model",
                "Flat, term structure, local vol, Heston — 4 alternatives valid for American exercise",
                "ORE-QuantLib · vol edge",
            ),
            (
                Axis.PRICING_ENGINE.value,
                "Pricing engine",
                "Finite difference, binomial CRR, Barone-Adesi-Whaley, Bjerksund-Stensland",
                "ORE-QuantLib · engine edge",
            ),
        ]
        # Note: FR-02 specifically requires typing "swap" after EquityOption lists nothing,
        # and typing "vol" lists volatility model and pricing engine (matching mockup).
        hits: List[CandidateHit] = []
        for axis_id, name, why, src in axes:
            if not q or q in name.lower() or q in why.lower() or (q == "vol" and axis_id == Axis.PRICING_ENGINE.value):
                hits.append(
                    CandidateHit(
                        id=axis_id,
                        name=name,
                        why=why,
                        source_layer=src,
                        kind="axis",
                    )
                )
        return hits

    def parse_title(self, title_line: str) -> Tuple[str, Verb, Optional[str]]:
        """Parse line 1 title into (root_id, verb, axis)."""
        clean = title_line.lstrip("#").strip()
        lower = clean.lower()

        if lower.startswith("explain "):
            verb = Verb.EXPLAIN
        elif lower.startswith("price "):
            verb = Verb.PRICE
        elif lower.startswith("compare "):
            verb = Verb.COMPARE
        else:
            raise ValueError(
                f"Title {clean!r} must start with Explain, Price, or Compare (FR-05 / FR-11)."
            )

        if "equity option" not in lower and "equityoption" not in lower:
            raise ValueError(
                f"Unknown product in title {clean!r}. Supported v1 root: EquityOption (American/European equity option)."
            )
        root_id = "EquityOption"

        axis: Optional[str] = None
        if verb == Verb.COMPARE:
            if re.search(r"vol(atility)?\s+model", lower) or "varying volatility_model" in lower:
                axis = Axis.VOLATILITY_MODEL.value
            elif re.search(r"pricing\s+engine", lower) or "varying pricing_engine" in lower:
                axis = Axis.PRICING_ENGINE.value
            else:
                raise ValueError(
                    f"Compare title {clean!r} must specify axis 'volatility model' or 'pricing engine'."
                )
        return root_id, verb, axis

    def check_slug_collision(self, title: str, recipes_dir: Path) -> Optional[Path]:
        target_slug = slugify(title)
        if not recipes_dir.exists():
            return None
        for md_file in recipes_dir.glob("*.md"):
            if md_file.stem == target_slug:
                return md_file
        return None

    @staticmethod
    def build_title(verb: Verb, root_label: str, axis: Optional[str] = None) -> str:
        return canonical_title(verb, root_label, axis)
