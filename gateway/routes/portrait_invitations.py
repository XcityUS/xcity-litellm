from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Final, Literal
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from gateway.providers.byteplus_assets import BytePlusAssetClient
from gateway.routes.provider_assets import asset_owner, get_asset_client
from gateway.verification.service import (
    CONSENT_VERSION,
    available,
    create_session,
    finish,
    localized_h5_link,
    public_view,
    token_hash,
)
from gateway.verification.store import Verification, VerificationRepository, get_store
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

router: Final = APIRouter(prefix="/v1/provider-assets/invitations", tags=["provider-assets"])


class CreateInvitation(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")
    inviter_name: str = Field(alias="inviterName", min_length=1, max_length=80)
    person_name: str = Field(alias="personName", min_length=1, max_length=80)
    callback_url: str = Field(alias="callbackUrl", max_length=2048)
    locale: Literal["zh", "en"]


class InvitationToken(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class StartInvitation(InvitationToken):
    consent: Literal[True]
    consent_version: Literal["portrait-video-v1"] = Field(alias="consentVersion")
    restart: bool = False


class CompleteInvitation(InvitationToken):
    byted_token: str = Field(alias="bytedToken", min_length=1, max_length=8192)


@router.post("")
async def create_invitation(
    body: CreateInvitation,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    store: VerificationRepository = Depends(get_store),
) -> dict[str, object]:
    callback: Final = urlparse(body.callback_url)
    if (
        (
            callback.scheme != "https"
            and not (callback.scheme == "http" and callback.hostname in ("localhost", "127.0.0.1"))
        )
        or not callback.hostname
        or callback.query
        or callback.fragment
        or callback.username
    ):
        raise HTTPException(400, "INVALID_CALLBACK")
    token: Final = secrets.token_urlsafe(32)
    now: Final = datetime.now(timezone.utc)
    record: Final = Verification(
        id=str(uuid4()),
        owner_id=asset_owner(auth),
        token_hash=token_hash(token),
        inviter_name=body.inviter_name,
        person_name=body.person_name,
        callback_url=body.callback_url,
        locale=body.locale,
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    async with store.transaction() as transaction:
        await transaction.limit(record.owner_id)
        await transaction.insert(record)
    return {**public_view(record), "token": token}


@router.get("")
async def list_invitations(
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    store: VerificationRepository = Depends(get_store),
) -> dict[str, object]:
    return {
        "invitations": tuple(
            {**public_view(item), "groupId": item.group_id} for item in await store.list(asset_owner(auth))
        )
    }


@router.delete("/{invitation_id}")
async def cancel_invitation(
    invitation_id: str,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    store: VerificationRepository = Depends(get_store),
) -> dict[str, object]:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("id", invitation_id)
        if record.owner_id != asset_owner(auth):
            raise HTTPException(404, "INVITATION_NOT_FOUND")
        if record.group_id:
            raise HTTPException(409, "INVITATION_ALREADY_VERIFIED")
        await transaction.cancel(record.id)
    return {"status": "cancelled"}


@router.post("/inspect")
async def inspect_invitation(
    body: InvitationToken, store: VerificationRepository = Depends(get_store)
) -> dict[str, object]:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("token_hash", token_hash(body.token))
        return public_view(record)


@router.post("/start")
async def start_invitation(
    body: StartInvitation,
    store: VerificationRepository = Depends(get_store),
    client: BytePlusAssetClient = Depends(get_asset_client),
) -> dict[str, str]:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("token_hash", token_hash(body.token))
        available(record)
        if record.group_id:
            raise HTTPException(409, "INVITATION_ALREADY_VERIFIED")
        if record.h5_link and not body.restart:
            return {"h5Link": record.h5_link}
        if record.attempts >= 3:
            raise HTTPException(409, "VERIFICATION_ATTEMPTS_EXHAUSTED")
        callback: Final = record.callback_url + "?" + urlencode({"token": body.token})
        link, byted_token = await create_session(client, callback)
        localized_link: Final = localized_h5_link(link, record.locale)
        await transaction.session(record.id, byted_token, localized_link, CONSENT_VERSION)
        return {"h5Link": localized_link}


@router.post("/complete")
async def complete_invitation(
    body: CompleteInvitation,
    store: VerificationRepository = Depends(get_store),
    client: BytePlusAssetClient = Depends(get_asset_client),
) -> dict[str, str]:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("token_hash", token_hash(body.token))
        if record.byted_token != body.byted_token or record.consented_at is None:
            raise HTTPException(403, "VERIFICATION_SESSION_MISMATCH")
        await finish(record, transaction, client)
        return {"status": "verified"}
