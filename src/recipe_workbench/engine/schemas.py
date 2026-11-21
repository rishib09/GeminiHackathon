"""Schemas for ORE engine inputs, pricing outputs, assumptions, and provenance."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ConventionsSpec(BaseModel):
    calendar: str = "NYSE"
    day_count: str = "Actual/365 (Fixed)"
    exercise_style: Literal["American", "European"] = "American"
    dividend_handling: str = "Continuous yield"
    settlement: str = "Cash"


class OptionContractSpec(BaseModel):
    symbol: str = "SPY"
    contract_symbol: str
    option_type: Literal["Call", "Put"] = "Put"
    exercise_style: Literal["American", "European"] = "American"
    strike: float
    expiry: str
    dte: int
    quantity: float = 1.0
    currency: str = "USD"


class ProvenanceRecord(BaseModel):
    asof: str
    snapshot_id: str
    engine_name: str
    model_name: str
    graph_version: str
    ore_version: str


class CashflowRow(BaseModel):
    date: str
    type: str
    amount: float
    currency: str


class PriceResult(BaseModel):
    contract: OptionContractSpec
    npv: float
    delta: Optional[float] = None
    cashflows: List[CashflowRow] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    provenance: ProvenanceRecord
    ore_xml_dir: Optional[str] = None


class ModelRow(BaseModel):
    model_id: str
    model_name: str
    engine_name: str
    price: float
    delta: float
    chain_rmse: float
    rank: int
    elapsed_ms: float
    status: Literal["completed", "running"] = "completed"
    notes: str = ""


class ComparisonResult(BaseModel):
    contract: OptionContractSpec
    axis: str
    rows: List[ModelRow]
    winner_model_id: str
    winner_model_name: str
    assumptions: List[str] = Field(default_factory=list)
    provenance: ProvenanceRecord
