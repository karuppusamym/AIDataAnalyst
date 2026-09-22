"""Shared API model configuration, independent of bounded-context schemas."""

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
