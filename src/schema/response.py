"""Validated execution decision before destination or evidence retrieval."""

from typing import Literal
from pydantic import BaseModel, ConfigDict


ResponseMode = Literal["retrieval", "llm_only"]


class ResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: ResponseMode
    decision_source: Literal["jev", "override", "constraint", "fallback"]
    fallback_reason: Literal["missing_key", "disabled", "provider_unavailable"] | None = None
