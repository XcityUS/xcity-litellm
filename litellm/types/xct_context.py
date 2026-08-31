"""Pydantic types for the xct context-document store.

Cross-product user context (preferences, org knowledge, notes) shared by
Xcity OS, xct-chat, and xct-home through tokenhub. Mirrors the shapes in
``litellm.types.xct_skills`` — same {data, has_more, next_cursor} paging.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

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
    tags: Optional[List[str]] = None
    team_id: Optional[str] = None
    is_public: bool = False
    xct_metadata: Dict[str, Any] = Field(default_factory=dict)


class XCTContextPatch(BaseModel):
    """Partial update payload for ``PATCH /v1/xct-context/{id}``."""

    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: Optional[bool] = None
    xct_metadata: Optional[Dict[str, Any]] = None


class XCTContextDoc(BaseModel):
    """Full read shape returned by ``GET /v1/xct-context/{id}`` (and create/
    patch responses). ``content`` carries the entire document body.
    """

    context_id: str
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: bool = False
    team_id: Optional[str] = None
    user_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    xct_metadata: Dict[str, Any] = Field(default_factory=dict)


class XCTContextListItem(BaseModel):
    """List-row shape for ``GET /v1/xct-context``.

    Deliberately carries ``content_preview`` (the first 200 chars of the
    document body) INSTEAD of the full ``content`` field, so list pages stay
    small even when documents approach the 256 KB limit. Fetch the single
    document via ``GET /v1/xct-context/{id}`` for the full body.
    """

    context_id: str
    title: Optional[str] = None
    content_preview: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: bool = False
    team_id: Optional[str] = None
    user_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    xct_metadata: Dict[str, Any] = Field(default_factory=dict)


class XCTContextListResponse(BaseModel):
    """Cursor-paginated list response for ``GET /v1/xct-context``."""

    data: List[XCTContextListItem]
    has_more: bool = False
    next_cursor: Optional[str] = None
