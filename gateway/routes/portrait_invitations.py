from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import FormData, UploadFile
from typing_extensions import ReadOnly, TypedDict

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
from gateway.verification.store import Verification, VerificationRepository, VoiceSample, get_store
from gateway.verification.voice import (
    VOICE_CONSENT_VERSION,
    VOICE_MAX_BYTES,
    VOICE_TYPES,
    MediaWorker,
    VoiceStep,
    get_media_worker,
    voice_name,
    voice_step,
    voice_view,
)
from gateway.verification.voice_uploads import reserve_voice_upload, save_voice_upload
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


class VoiceFields(InvitationToken):
    model_config = ConfigDict(frozen=True, extra="ignore")
    language: str = Field(pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
    duration_ms: int = Field(alias="durationMs", ge=2000, le=600_000)


class VoiceSubmitted(TypedDict):
    voiceStep: ReadOnly[VoiceStep]


class VoiceAcknowledged(TypedDict):
    status: ReadOnly[Literal["imported"]]


class ImportVoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: str = Field(pattern=r"^https://", max_length=2048)
    submitted_at: AwareDatetime = Field(alias="submittedAt")


class VoiceUpload(BaseModel):
    model_config = ConfigDict(frozen=True)
    fields: VoiceFields
    content: bytes
    content_type: str


def voice_fields(form: FormData) -> VoiceFields:
    if form.get("consent") != "true" or form.get("consentVersion") != VOICE_CONSENT_VERSION:
        raise HTTPException(400, "VOICE_CONSENT_REQUIRED")
    try:
        return VoiceFields.model_validate(form)
    except ValidationError as exc:
        raise HTTPException(400, "VOICE_INVALID") from exc


async def read_voice_upload(request: Request) -> VoiceUpload:
    async with request.form(max_files=1, max_fields=8) as form:
        fields: Final = voice_fields(form)
        file: Final = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(400, "VOICE_INVALID")
        content_type: Final = VOICE_TYPES.get((file.content_type or "").split(";")[0].strip().lower())
        content: Final = await file.read(VOICE_MAX_BYTES + 1)
    if content_type is None or not 44 <= len(content) <= VOICE_MAX_BYTES:
        raise HTTPException(400, "VOICE_INVALID")
    return VoiceUpload(fields=fields, content=content, content_type=content_type)


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
            {**public_view(item), "groupId": item.group_id, "voice": voice_view(item)}
            for item in await store.list(asset_owner(auth))
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
) -> Mapping[str, object]:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("token_hash", token_hash(body.token))
        return MappingProxyType({**public_view(record), "voiceStep": voice_step(record)})


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


@router.post("/voice")
async def submit_voice(
    request: Request,
    store: VerificationRepository = Depends(get_store),
    media: MediaWorker = Depends(get_media_worker),
) -> VoiceSubmitted:
    upload: Final = await read_voice_upload(request)
    async with reserve_voice_upload(store, token_hash(upload.fields.token)) as reservation:
        stored: Final = await media.upload(
            reservation.record.owner_id, upload.content, upload.content_type, voice_name(reservation.record)
        )
        if stored is None:
            raise HTTPException(503, "VOICE_UNAVAILABLE")
        await save_voice_upload(
            store,
            reservation,
            VoiceSample(
                url=stored.url,
                key=stored.key,
                language=upload.fields.language,
                duration_ms=upload.fields.duration_ms,
                consent_version=VOICE_CONSENT_VERSION,
            ),
        )
    return VoiceSubmitted(voiceStep=VoiceStep(enabled=True, submitted=True))


@router.post("/{invitation_id}/voice/imported")
async def acknowledge_voice(
    invitation_id: str,
    body: ImportVoice,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    store: VerificationRepository = Depends(get_store),
) -> VoiceAcknowledged:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("id", invitation_id)
        if record.owner_id != asset_owner(auth) or record.voice_url is None:
            raise HTTPException(404, "INVITATION_NOT_FOUND")
        if record.voice_url != body.url or record.voice_submitted_at != body.submitted_at:
            raise HTTPException(409, "VOICE_CHANGED")
        await transaction.voice_imported(record.id)
    return VoiceAcknowledged(status="imported")
