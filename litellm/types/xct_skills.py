"""Pydantic types for xct-native skills (S2-03).

These live alongside the Anthropic skill types in
``litellm.types.llms.anthropic_skills`` — same DB table, different shape.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class XCTSkillToolRef(BaseModel):
    """One MCP-portal tool a skill wants available at run time.

    ``server_id`` names the upstream MCP server (as the portal knows it);
    ``tool`` optionally narrows to one tool on that server. Extra keys are
    stored verbatim so the manifest can grow without a type change here.
    """

    model_config = ConfigDict(extra="allow")

    server_id: str = Field(min_length=1)
    tool: Optional[str] = None
    note: Optional[str] = None


class XCTSkillPricing(BaseModel):
    """Marketplace pricing for one skill, denominated in KWH (credits/100).

    Extra keys are stored verbatim for the same forward-compatibility reason
    as :class:`XCTSkillToolRef`.
    """

    model_config = ConfigDict(extra="allow")

    kwh_per_use: Optional[float] = Field(default=None, ge=0)


class XCTSkillCreate(BaseModel):
    """Payload for ``POST /v1/xct-skills`` (JSON or multipart-form fields)."""

    display_title: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    system_prompt_template: Optional[str] = None
    tool_schema: Optional[Dict[str, Any]] = None
    category: Optional[str] = None
    team_id: Optional[str] = None
    is_public: bool = False
    version: Optional[str] = "1"
    tools: Optional[List[XCTSkillToolRef]] = None
    pricing: Optional[XCTSkillPricing] = None
    xct_metadata: Dict[str, Any] = Field(default_factory=dict)


class XCTSkillPatch(BaseModel):
    """Partial update payload for ``PATCH /v1/xct-skills/{id}``."""

    display_title: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    system_prompt_template: Optional[str] = None
    tool_schema: Optional[Dict[str, Any]] = None
    is_public: Optional[bool] = None
    version: Optional[str] = None
    tools: Optional[List[XCTSkillToolRef]] = None
    pricing: Optional[XCTSkillPricing] = None
    xct_metadata: Optional[Dict[str, Any]] = None


class XCTSkill(BaseModel):
    """Full read shape returned by ``GET /v1/xct-skills/{id}``."""

    skill_id: str
    display_title: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    system_prompt_template: Optional[str] = None
    tool_schema: Optional[Dict[str, Any]] = None
    source: str = "custom"
    version: Optional[str] = None
    is_public: bool = False
    team_id: Optional[str] = None
    user_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Surfaced from ``xct_metadata["tools"]`` / ``xct_metadata["pricing"]`` —
    # the manifest lives inside the metadata JSON column (no DB migration),
    # but consumers read it as first-class fields.
    tools: Optional[List[Dict[str, Any]]] = None
    pricing: Optional[Dict[str, Any]] = None
    xct_metadata: Dict[str, Any] = Field(default_factory=dict)


class XCTSkillListResponse(BaseModel):
    """Cursor-paginated list response for ``GET /v1/xct-skills``."""

    data: List[XCTSkill]
    has_more: bool = False
    next_cursor: Optional[str] = None
