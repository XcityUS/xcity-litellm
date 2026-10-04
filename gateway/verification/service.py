from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Final

from fastapi import HTTPException
from pydantic import TypeAdapter

from gateway.providers.byteplus_assets import BytePlusAssetClient, ProviderSuccess
from gateway.verification.store import Verification, VerificationRepository

CONSENT_VERSION: Final = "portrait-video-v1"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def available(record: Verification) -> None:
    if record.cancelled_at is not None:
        raise HTTPException(410, "INVITATION_CANCELLED")
    if record.group_id is None and record.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(410, "INVITATION_EXPIRED")


def public_view(record: Verification) -> dict[str, object]:
    status: Final = (
        "cancelled"
        if record.cancelled_at
        else "verified"
        if record.group_id
        else "expired"
        if record.expires_at <= datetime.now(timezone.utc)
        else "verifying"
        if record.byted_token
        else "pending"
    )
    return {
        "id": record.id,
        "inviterName": record.inviter_name,
        "personName": record.person_name,
        "status": status,
        "expiresAt": record.expires_at.isoformat(),
    }


async def provider_call(client: BytePlusAssetClient, action: str, body: Mapping[str, object]) -> Mapping[str, object]:
    try:
        async with asyncio.timeout(15):
            result: Final = await client.call(action, body)
    except TimeoutError as exc:
        raise HTTPException(502, "VERIFICATION_PROVIDER_UNAVAILABLE") from exc
    if not isinstance(result, ProviderSuccess):
        raise HTTPException(502, "VERIFICATION_PROVIDER_UNAVAILABLE")
    root: Final = result.payload.get("Result")
    return TypeAdapter(dict[str, object]).validate_python(root) if isinstance(root, dict) else result.payload


def field(record: Mapping[str, object], *names: str) -> str:
    return next((value.strip() for name in names if isinstance(value := record.get(name), str) and value.strip()), "")


def localized_h5_link(link: str, locale: str) -> str:
    if re.search(r"[?&]lng=", link):
        return link
    base, marker, fragment = link.partition("#")
    return base + ("&" if "?" in base else "?") + "lng=" + locale + marker + fragment


async def create_session(client: BytePlusAssetClient, callback: str) -> tuple[str, str]:
    payload: Final = await provider_call(client, "CreateVisualValidateSession", {"CallbackURL": callback})
    link: Final = field(payload, "H5Link", "h5Link")
    token: Final = field(payload, "BytedToken", "bytedToken")
    if not link.startswith("https://") or not token:
        raise HTTPException(502, "VERIFICATION_INVALID_RESPONSE")
    return link, token


async def finish(record: Verification, store: VerificationRepository, client: BytePlusAssetClient) -> str:
    available(record)
    if record.group_id:
        return record.group_id
    if not record.byted_token:
        raise HTTPException(409, "VERIFICATION_NOT_STARTED")
    payload: Final = await provider_call(client, "GetVisualValidateResult", {"BytedToken": record.byted_token})
    group: Final = field(payload, "GroupId", "groupId", "Id", "id")
    if not group:
        raise HTTPException(409, "VERIFICATION_INCOMPLETE")
    owner: Final = "xcity:" + hashlib.sha256(record.owner_id.encode()).hexdigest()[:24]
    await provider_call(client, "UpdateAssetGroup", {"Id": group, "Name": owner})
    await store.complete(record.id, group)
    return group
