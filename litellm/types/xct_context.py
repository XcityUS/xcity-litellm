"""Pydantic types for the xct context-document store.

Cross-product user context (preferences, org knowledge, notes) shared by
Xcity OS, xct-chat, and xct-home through tokenhub. Mirrors the shapes in
``litellm.types.xct_skills`` — same {data, has_more, next_cursor} paging.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Maximum size of a context document body, enforced at the API layer
# (create + patch). 256 KB of UTF-8 encoded content.
MAX_CONTEXT_CONTENT_BYTES = 256 * 1024

# Length of the content preview surfaced on list rows.
CONTENT_PREVIEW_CHARS = 200


class XCTContextCreate(BaseModel):
    """Payload for ``POST /v1/xct-context``."""

    title: str
    content: str
    tags: list[str] | None = None
    team_id: str | None = None
    is_public: bool = False
    xct_metadata: dict[str, Any] = Field(default_factory=dict)


class XCTContextPatch(BaseModel):
    """Partial update payload for ``PATCH /v1/xct-context/{id}``."""

    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    is_public: bool | None = None
    xct_metadata: dict[str, Any] | None = None


class XCTContextDoc(BaseModel):
    """Full read shape returned by ``GET /v1/xct-context/{id}`` (and create/
    patch responses). ``content`` carries the entire document body.
    """

    context_id: str
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    is_public: bool = False
    team_id: str | None = None
    user_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    xct_metadata: dict[str, Any] = Field(default_factory=dict)


class XCTContextListItem(BaseModel):
    """List-row shape for ``GET /v1/xct-context``.

    Deliberately carries ``content_preview`` (the first 200 chars of the
    document body) INSTEAD of the full ``content`` field, so list pages stay
    small even when documents approach the 256 KB limit. Fetch the single
    document via ``GET /v1/xct-context/{id}`` for the full body.
    """

    context_id: str
    title: str | None = None
    content_preview: str | None = None
    tags: list[str] | None = None
    is_public: bool = False
    team_id: str | None = None
    user_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    xct_metadata: dict[str, Any] = Field(default_factory=dict)


class XCTContextListResponse(BaseModel):
    """Cursor-paginated list response for ``GET /v1/xct-context``."""

    data: list[XCTContextListItem]
    has_more: bool = False
    next_cursor: str | None = None
