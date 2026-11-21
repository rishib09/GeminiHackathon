"""Pydantic schemas for the ORE Knowledge Graph, Root Picker, and Scope."""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Verb(str, Enum):
    EXPLAIN = "Explain"
    PRICE = "Price"
    COMPARE = "Compare"


class Axis(str, Enum):
    VOLATILITY_MODEL = "volatility_model"
    PRICING_ENGINE = "pricing_engine"


class NodeCategory(str, Enum):
    ROOT = "root"
    NEEDS = "needs"
    VARIES = "varies"
    FORMULA = "formula"
    SIBLING = "sibling"


class GraphNode(BaseModel):
    id: str
    label: str
    category: NodeCategory
    layer: Literal["ORE", "ORE-QuantLib", "data", "formula"]
    description: str
    why: str = ""
    aliases: List[str] = Field(default_factory=list)
    code_ref: Optional[str] = None
    xml_tag: Optional[str] = None
    engine_key: Optional[str] = None
    axis: Optional[str] = None
    allowed_verbs: List[Verb] = Field(default_factory=list)
    is_data_leaf: bool = False


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: Literal["needs", "alternatives", "sibling", "produces", "uses_engine"]
    axis: Optional[str] = None
    note: str = ""


class ScopeSpec(BaseModel):
    title: str
    slug: str
    root_id: str
    root_label: str
    verb: Verb
    axis: Optional[str] = None
    disambiguation: Optional[str] = None
    graph_version: str
    needs: List[GraphNode] = Field(default_factory=list)
    varies: List[GraphNode] = Field(default_factory=list)
    formulas: List[GraphNode] = Field(default_factory=list)
    siblings: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)
    uses_added: List[str] = Field(default_factory=list)

    def all_in_scope_nodes(self) -> List[GraphNode]:
        return [*self.needs, *self.varies, *self.formulas]

    def in_scope_ids(self) -> set[str]:
        return {n.id for n in self.all_in_scope_nodes()}


class CandidateHit(BaseModel):
    id: str
    name: str
    why: str
    source_layer: str
    kind: Literal["root", "verb", "axis"]
    proposed: bool = False
