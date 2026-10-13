"""Structured destination extraction, before any retrieval is performed."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class LocationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["destination", "no_location", "ambiguous"]
    location: str | None
    candidates: list[str]
    clarification: str | None

    @model_validator(mode="after")
    def validate_decision(self):
        if self.location is not None:
            self.location = self.location.strip()
            if not self.location or len(self.location) > 160:
                raise ValueError("Destination must be a nonblank place name of at most 160 characters.")
        self.candidates = list(dict.fromkeys(name.strip() for name in self.candidates))
        if len(self.candidates) > 5 or any(not name or len(name) > 160 for name in self.candidates):
            raise ValueError("Provide at most five nonblank candidate names.")
        if self.status == "destination":
            if self.location is None or self.candidates or self.clarification is not None:
                raise ValueError("A destination decision needs one location and no clarification/candidates.")
        elif self.status == "no_location":
            if self.location is not None or self.candidates or self.clarification is not None:
                raise ValueError("An unrestricted request must not contain a destination or clarification.")
        elif self.location is not None or not self.clarification or not self.clarification.strip():
            raise ValueError("An ambiguous request needs a clarification question and no chosen location.")
        return self
