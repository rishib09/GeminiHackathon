"""Gemini Interactions API client for constrained binding proposals, root prefill, and grounded explainers."""

from __future__ import annotations

import os
from typing import List, Optional, Type, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

T = TypeVar("T", bound=BaseModel)


class RootPrefillProposal(BaseModel):
    root_id: str = Field(description="Graph root ID (e.g., EquityOption)")
    verb: str = Field(description="Action verb: Explain, Price, or Compare")
    axis: Optional[str] = Field(default=None, description="Axis: volatility_model or pricing_engine")
    rationale: str = Field(description="One-sentence reason grounded in the user prompt")


class BindingProposal(BaseModel):
    phrase: str = Field(description="Exact span from the recipe prose")
    target_node_id: str = Field(description="Node ID strictly from scope.json")
    confidence: float = Field(description="0.0 to 1.0 confidence")
    reason: str = Field(description="Brief reason why this phrase maps to target_node_id")


class BindingProposalBatch(BaseModel):
    proposals: List[BindingProposal] = Field(default_factory=list)


class ExplainerSentence(BaseModel):
    text: str = Field(description="One concise technical sentence")
    node_ids: List[str] = Field(
        description="Graph node IDs from scope.json that ground this sentence"
    )


class ExplainerDraft(BaseModel):
    business_intuition: str
    technical_intuition: List[ExplainerSentence]
    architecture_map: List[ExplainerSentence]
    object_drilldown: List[ExplainerSentence]


def has_gemini_key() -> bool:
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def _get_client():
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    return genai.Client(api_key=api_key)


def call_structured(
    prompt: str,
    schema_cls: Type[T],
    system_instruction: str = "You are a precise quantitative finance compiler assistant.",
    model: str = "gemini-4-hydrogen",
) -> Optional[T]:
    """Call Gemini Interactions API with structured JSON output; returns None if offline/no key."""
    if not has_gemini_key():
        return None
    try:
        client = _get_client()
        interaction = client.interactions.create(
            model=model,
            input=prompt,
            system_instruction=system_instruction,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema_cls.model_json_schema(),
            },
        )
        if not interaction.output_text:
            return None
        return schema_cls.model_validate_json(interaction.output_text)
    except Exception:
        return None
