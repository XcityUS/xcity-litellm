"""Pydantic types for xct-native skills (S2-03).

These live alongside the Anthropic skill types in
``litellm.types.llms.anthropic_skills`` — same DB table, different shape.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class XCTSkillToolRef(BaseModel):
    """One MCP-portal tool a skill wants available at run time.

    ``server_id`` names the upstream MCP server (as the portal knows it);
    ``tool`` optionally narrows to one tool on that server. Extra keys are
    stored verbatim so the manifest can grow without a type change here.
    """

    model_config = ConfigDict(extra="allow")

    server_id: str = Field(min_length=1)
    tool: str | None = None
    note: str | None = None


class XCTSkillPricing(BaseModel):
    """Marketplace pricing for one skill, denominated in KWH (credits/100).

    Extra keys are stored verbatim for the same forward-compatibility reason
    as :class:`XCTSkillToolRef`.
    """

    model_config = ConfigDict(extra="allow")

    kwh_per_use: float | None = Field(default=None, ge=0)


class XCTSkillCreate(BaseModel):
    """Payload for ``POST /v1/xct-skills`` (JSON or multipart-form fields)."""

    display_title: str
    description: str | None = None
    instructions: str | None = None
    system_prompt_template: str | None = None
    tool_schema: dict[str, Any] | None = None
    category: str | None = None
    team_id: str | None = None
    is_public: bool = False
    version: str | None = "1"
    tools: list[XCTSkillToolRef] | None = None
    pricing: XCTSkillPricing | None = None
    xct_metadata: dict[str, Any] = Field(default_factory=dict)


class XCTSkillPatch(BaseModel):
    """Partial update payload for ``PATCH /v1/xct-skills/{id}``."""

    display_title: str | None = None
    description: str | None = None
    instructions: str | None = None
    system_prompt_template: str | None = None
    tool_schema: dict[str, Any] | None = None
    is_public: bool | None = None
    version: str | None = None
    tools: list[XCTSkillToolRef] | None = None
    pricing: XCTSkillPricing | None = None
    xct_metadata: dict[str, Any] | None = None


class XCTSkill(BaseModel):
    """Full read shape returned by ``GET /v1/xct-skills/{id}``."""

    skill_id: str
    display_title: str | None = None
    description: str | None = None
    instructions: str | None = None
    system_prompt_template: str | None = None
    tool_schema: dict[str, Any] | None = None
    source: str = "custom"
    version: str | None = None
    is_public: bool = False
    team_id: str | None = None
    user_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Surfaced from ``xct_metadata["tools"]`` / ``xct_metadata["pricing"]`` —
    # the manifest lives inside the metadata JSON column (no DB migration),
    # but consumers read it as first-class fields.
    tools: list[dict[str, Any]] | None = None
    pricing: dict[str, Any] | None = None
    xct_metadata: dict[str, Any] = Field(default_factory=dict)


class XCTSkillListResponse(BaseModel):
    """Cursor-paginated list response for ``GET /v1/xct-skills``."""

    data: list[XCTSkill]
    has_more: bool = False
    next_cursor: str | None = None
