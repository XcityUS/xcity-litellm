"""Pydantic types for webhook subscriptions (S6-04)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Known event names that the dispatcher emits. Subscribers may register any
# string; unknown events simply never fire. Kept here for SDK type generation.
KNOWN_WEBHOOK_EVENTS = (
    "capability.invoked",
    "budget.exhausted",
    "agent.healthcheck.failed",
    "mcp.tool.called",
)


class WebhookSubscriptionCreate(BaseModel):
    target_url: str
    events: list[str] = Field(..., min_length=1)
    app_id: str | None = None
    team_id: str | None = None
    filters: dict[str, Any] | None = None
    is_active: bool = True


class WebhookSubscriptionPatch(BaseModel):
    target_url: str | None = None
    events: list[str] | None = None
    filters: dict[str, Any] | None = None
    is_active: bool | None = None


class WebhookSubscription(BaseModel):
    subscription_id: str
    app_id: str | None = None
    team_id: str | None = None
    user_id: str | None = None
    events: list[str] = Field(default_factory=list)
    target_url: str
    filters: dict[str, Any] | None = None
    is_active: bool = True
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0


class WebhookSubscriptionCreateResponse(WebhookSubscription):
    """Response from POST /v1/webhooks.

    Carries the unhashed ``secret`` ONE TIME — the caller must store it
    immediately, the proxy keeps only the bcrypt hash.
    """

    secret: str
