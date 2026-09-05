"""Pydantic types for XCT Apps (S4-03)."""

from datetime import datetime

from pydantic import BaseModel, Field


class XCTAppCreate(BaseModel):
    app_name: str = Field(..., description="Internal slug, unique. Used as login hint.")
    display_name: str
    description: str | None = None
    icon_url: str | None = None
    redirect_uris: list[str] = Field(default_factory=list)
    default_team_id: str | None = None
    default_scopes: list[str] = Field(default_factory=list)
    capability_scope_id: str | None = None
    rpm_limit: int | None = None
    daily_budget: float | None = None
    is_active: bool = True


class XCTAppPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    icon_url: str | None = None
    redirect_uris: list[str] | None = None
    default_team_id: str | None = None
    default_scopes: list[str] | None = None
    capability_scope_id: str | None = None
    rpm_limit: int | None = None
    daily_budget: float | None = None
    is_active: bool | None = None


class XCTApp(BaseModel):
    """Public read shape — NEVER includes the client secret."""

    app_id: str
    app_name: str
    display_name: str
    description: str | None = None
    icon_url: str | None = None
    oauth_client_id: str
    redirect_uris: list[str] = Field(default_factory=list)
    default_team_id: str | None = None
    default_scopes: list[str] = Field(default_factory=list)
    capability_scope_id: str | None = None
    rpm_limit: int | None = None
    daily_budget: float | None = None
    is_active: bool = True
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None


class XCTAppCreateResponse(XCTApp):
    """One-time response after create / rotate-secret.

    Carries ``client_secret`` — the cleartext, returned exactly once.
    """

    client_secret: str
