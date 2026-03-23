from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import ParserType


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateTargetRequest(BaseModel):
    parser_type: ParserType
    name: str = Field(min_length=1, max_length=128)
    identifier: str = Field(min_length=1, max_length=512)
    config: dict = Field(default_factory=dict)


class CreateAccountRequest(BaseModel):
    parser_type: ParserType
    label: str = Field(min_length=1, max_length=128)
    hourly_limit: int = Field(default=120, ge=1, le=10000)
    credentials: dict = Field(default_factory=dict)


class LinkAccountRequest(BaseModel):
    target_id: int
    account_id: int
