from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated


class GroundingReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    supported: bool
    issues: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=20)
