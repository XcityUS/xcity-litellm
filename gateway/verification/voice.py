from __future__ import annotations

import os
import secrets
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Final, Protocol
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import ReadOnly, TypedDict

from gateway.verification.store import Verification, writer_db
from litellm.proxy._types import hash_token

VOICE_CONSENT_VERSION: Final = "portrait-voice-v1"
VOICE_SUBMISSION_LIMIT: Final = 5
VOICE_MAX_BYTES: Final = 15 * 1024 * 1024
VOICE_TYPES: Final = MappingProxyType(
    {
        "audio/mpeg": "audio/mpeg",
        "audio/mp3": "audio/mpeg",
        "audio/wav": "audio/wav",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "audio/mp4": "audio/mp4",
        "audio/x-m4a": "audio/mp4",
        "audio/m4a": "audio/mp4",
    }
)
DEFAULT_MEDIA_WORKER_URL: Final = "https://media.xcity.ai"
UPLOAD_KEY_LIFETIME: Final = timedelta(minutes=2)
VOICE_LABELS: Final = MappingProxyType({"zh": "本人录音", "en": "own recording"})


class StoredMedia(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    url: str = Field(pattern=r"^https://")
    key: str = Field(min_length=1)


class VoiceStep(TypedDict):
    enabled: ReadOnly[bool]
    submitted: ReadOnly[bool]


class VoiceView(TypedDict):
    url: ReadOnly[str]
    language: ReadOnly[str | None]
    durationMs: ReadOnly[int | None]
    submittedAt: ReadOnly[str]
    importedAt: ReadOnly[str | None]


class DeleteRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str


class KeySql(Protocol):
    async def execute_raw(self, query: str, *args: object) -> int: ...


class OwnerKeys(Protocol):
    def issue(self, owner: str) -> AbstractAsyncContextManager[str]: ...


class TemporaryOwnerKeys:
    """The media Worker files uploads under whichever user `/key/info` reports for the bearer.

    An invited person has no account, so their sample is uploaded with a key minted for the
    inviter that can only call `/key/info`, expires within minutes and is deleted after use
    """

    def __init__(self, db: KeySql) -> None:
        self.db: Final = db

    @asynccontextmanager
    async def issue(self, owner: str) -> AsyncGenerator[str]:
        token: Final = "sk-" + secrets.token_urlsafe(32)
        hashed: Final = hash_token(token)
        await self.db.execute_raw(
            """INSERT INTO "LiteLLM_VerificationToken"
            (token, user_id, expires, models, allowed_routes, key_alias, metadata)
            VALUES ($1, $2, ($3::timestamptz AT TIME ZONE 'UTC'), ARRAY[]::text[], ARRAY['/key/info']::text[],
                    'invitation-voice-upload', '{"purpose":"invitation-voice-upload"}'::jsonb)""",
            hashed,
            owner,
            datetime.now(timezone.utc) + UPLOAD_KEY_LIFETIME,
        )
        try:
            yield token
        finally:
            await self.db.execute_raw('DELETE FROM "LiteLLM_VerificationToken" WHERE token=$1', hashed)


class MediaWorker:
    def __init__(self, keys: OwnerKeys, base_url: str, http_client: httpx.AsyncClient | None = None) -> None:
        self._keys: Final = keys
        self._base_url: Final = base_url.rstrip("/")
        self._http_client: Final = http_client

    async def _post(self, path: str, headers: tuple[tuple[str, str], ...], content: bytes) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(self._base_url + path, headers=headers, content=content)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(self._base_url + path, headers=headers, content=content)

    async def upload(self, owner: str, content: bytes, content_type: str, name: str) -> StoredMedia | None:
        try:
            async with self._keys.issue(owner) as key:
                response: Final = await self._post(
                    "/upload",
                    (("Authorization", f"Bearer {key}"), ("Content-Type", content_type), ("X-Asset-Name", quote(name))),
                    content,
                )
            stored: Final = StoredMedia.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError):
            return None
        return stored if response.is_success and stored.key.startswith(f"u/{owner}/") else None

    async def delete(self, owner: str, key: str) -> None:
        try:
            async with self._keys.issue(owner) as bearer:
                await self._post(
                    "/assets/delete",
                    (("Authorization", f"Bearer {bearer}"), ("Content-Type", "application/json")),
                    DeleteRequest(key=key).model_dump_json().encode(),
                )
        except httpx.HTTPError:
            return


def get_media_worker() -> MediaWorker:
    db: Final = writer_db()
    if db is None:
        raise HTTPException(503, "VOICE_UNAVAILABLE")
    return MediaWorker(TemporaryOwnerKeys(db), os.getenv("MEDIA_WORKER_URL", "").strip() or DEFAULT_MEDIA_WORKER_URL)


def voice_name(record: Verification) -> str:
    return f"{record.person_name} · {VOICE_LABELS.get(record.locale, VOICE_LABELS['en'])}"[:80]


def voice_step(record: Verification) -> VoiceStep:
    return VoiceStep(
        enabled=record.cancelled_at is None and record.expires_at > datetime.now(timezone.utc),
        submitted=record.voice_url is not None,
    )


def voice_view(record: Verification) -> VoiceView | None:
    if record.voice_url is None or record.voice_submitted_at is None:
        return None
    return VoiceView(
        url=record.voice_url,
        language=record.voice_language,
        durationMs=record.voice_duration_ms,
        submittedAt=record.voice_submitted_at.isoformat(),
        importedAt=record.voice_imported_at.isoformat() if record.voice_imported_at else None,
    )


def accept_voice(record: Verification) -> None:
    if record.cancelled_at is not None:
        raise HTTPException(410, "INVITATION_CANCELLED")
    if record.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(410, "INVITATION_EXPIRED")
    if record.group_id is None:
        raise HTTPException(409, "VOICE_NEEDS_VERIFICATION")
    if record.voice_submissions >= VOICE_SUBMISSION_LIMIT:
        raise HTTPException(429, "VOICE_LIMIT_REACHED")
