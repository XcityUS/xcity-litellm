from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol, cast

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.proxy.db.routing_prisma_wrapper import RoutingPrismaWrapper

TABLE: Final = '"LiteLLM_AssetVerification"'


class Verification(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    owner_id: str
    token_hash: str | None = None
    inviter_name: str = ""
    person_name: str = ""
    callback_url: str
    locale: Literal["zh", "en"] = "en"
    expires_at: datetime
    created_at: datetime
    consented_at: datetime | None = None
    consent_version: str | None = None
    byted_token: str | None = None
    h5_link: str | None = None
    group_id: str | None = None
    cancelled_at: datetime | None = None
    attempts: int = 0
    voice_url: str | None = None
    voice_key: str | None = None
    voice_language: str | None = None
    voice_duration_ms: int | None = None
    voice_consent_version: str | None = None
    voice_consent_at: datetime | None = None
    voice_submissions: int = 0
    voice_submitted_at: datetime | None = None
    voice_imported_at: datetime | None = None
    voice_upload_id: str | None = None
    voice_upload_expires_at: datetime | None = None


class VoiceSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    key: str
    language: str
    duration_ms: int
    consent_version: str


ROWS: Final = TypeAdapter(tuple[Verification, ...])


class VerificationSql(Protocol):
    async def query_raw(self, query: str, *args: object) -> object: ...
    async def execute_raw(self, query: str, *args: object) -> int: ...
    def tx(self, *, timeout: timedelta) -> AbstractAsyncContextManager[VerificationSql]: ...


class VerificationRepository(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[VerificationRepository]: ...
    async def insert(self, record: Verification) -> None: ...
    async def one(self, field: Literal["id", "token_hash", "byted_token"], value: str) -> Verification: ...
    async def list(self, owner: str) -> tuple[Verification, ...]: ...
    async def limit(self, owner: str) -> None: ...
    async def session(self, id: str, token: str, link: str, consent: str) -> None: ...
    async def complete(self, id: str, group: str) -> None: ...
    async def cancel(self, id: str) -> None: ...
    async def reserve_voice(self, id: str, upload_id: str, seconds: int) -> bool: ...
    async def release_voice(self, id: str, upload_id: str) -> None: ...
    async def voice(self, id: str, sample: VoiceSample, upload_id: str) -> None: ...
    async def voice_imported(self, id: str) -> None: ...


class VerificationStore:
    def __init__(self, db: VerificationSql) -> None:
        self.db: Final = db

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[VerificationStore]:
        async with self.db.tx(timeout=timedelta(seconds=45)) as transaction:
            yield VerificationStore(transaction)

    async def insert(self, record: Verification) -> None:
        await self.db.execute_raw(
            f"""INSERT INTO {TABLE}
            (id, owner_id, token_hash, inviter_name, person_name, callback_url, locale,
             expires_at, created_at, byted_token, h5_link)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::timestamptz,$9::timestamptz,$10,$11)""",
            record.id,
            record.owner_id,
            record.token_hash,
            record.inviter_name,
            record.person_name,
            record.callback_url,
            record.locale,
            record.expires_at,
            record.created_at,
            record.byted_token,
            record.h5_link,
        )

    async def one(self, field: Literal["id", "token_hash", "byted_token"], value: str) -> Verification:
        rows: Final = ROWS.validate_python(
            await self.db.query_raw(f"SELECT * FROM {TABLE} WHERE {field}=$1 FOR UPDATE", value)
        )
        if not rows:
            raise HTTPException(404, "INVITATION_NOT_FOUND")
        return rows[0]

    async def list(self, owner: str) -> tuple[Verification, ...]:
        return ROWS.validate_python(
            await self.db.query_raw(
                f"""SELECT * FROM {TABLE} WHERE owner_id=$1 AND token_hash IS NOT NULL
                ORDER BY created_at DESC LIMIT 50""",
                owner,
            )
        )

    async def limit(self, owner: str) -> None:
        await self.db.execute_raw("SELECT pg_advisory_xact_lock(hashtext($1))", f"portrait-invites:{owner}")
        rows: Final = ROWS.validate_python(
            await self.db.query_raw(
                f"""SELECT * FROM {TABLE} WHERE owner_id=$1 AND token_hash IS NOT NULL
                AND created_at > now() - interval '24 hours' LIMIT 20""",
                owner,
            )
        )
        if len(rows) >= 20:
            raise HTTPException(429, "INVITATION_LIMIT_REACHED")

    async def session(self, id: str, token: str, link: str, consent: str) -> None:
        await self.db.execute_raw(
            f"""UPDATE {TABLE} SET byted_token=$2,h5_link=$3,attempts=attempts+1,
                consented_at=COALESCE(consented_at,now()),consent_version=$4 WHERE id=$1""",
            id,
            token,
            link,
            consent,
        )

    async def complete(self, id: str, group: str) -> None:
        await self.db.execute_raw(f"UPDATE {TABLE} SET group_id=$2,h5_link=NULL WHERE id=$1", id, group)

    async def cancel(self, id: str) -> None:
        await self.db.execute_raw(f"UPDATE {TABLE} SET cancelled_at=now(),h5_link=NULL WHERE id=$1", id)

    async def reserve_voice(self, id: str, upload_id: str, seconds: int) -> bool:
        changed: Final = await self.db.execute_raw(
            f"""UPDATE {TABLE} SET voice_upload_id=$2,voice_upload_expires_at=now()+$3::int*interval '1 second'
                WHERE id=$1 AND (voice_upload_id IS NULL OR voice_upload_expires_at IS NULL
                OR voice_upload_expires_at<=now())""",
            id,
            upload_id,
            seconds,
        )
        return changed == 1

    async def release_voice(self, id: str, upload_id: str) -> None:
        await self.db.execute_raw(
            f"UPDATE {TABLE} SET voice_upload_id=NULL,voice_upload_expires_at=NULL WHERE id=$1 AND voice_upload_id=$2",
            id,
            upload_id,
        )

    async def voice(self, id: str, sample: VoiceSample, upload_id: str) -> None:
        changed: Final = await self.db.execute_raw(
            f"""UPDATE {TABLE} SET voice_url=$2,voice_key=$3,voice_language=$4,voice_duration_ms=$5::int,
                voice_consent_version=$6,voice_consent_at=now(),voice_submissions=voice_submissions+1,
                voice_submitted_at=now(),voice_imported_at=NULL,voice_upload_id=NULL,voice_upload_expires_at=NULL
                WHERE id=$1 AND voice_upload_id=$7 AND voice_upload_expires_at>now()""",
            id,
            sample.url,
            sample.key,
            sample.language,
            sample.duration_ms,
            sample.consent_version,
            upload_id,
        )
        if changed != 1:
            raise HTTPException(503, "VOICE_UNAVAILABLE")

    async def voice_imported(self, id: str) -> None:
        await self.db.execute_raw(
            f"UPDATE {TABLE} SET voice_imported_at=now() WHERE id=$1 AND voice_imported_at IS NULL", id
        )


def writer_db() -> VerificationSql | None:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        return None
    db: Final = prisma_client.db
    return cast(VerificationSql, db.writer if isinstance(db, RoutingPrismaWrapper) else db)


def get_store() -> VerificationStore:
    db: Final = writer_db()
    if db is None:
        raise HTTPException(503, "INVITATIONS_UNAVAILABLE")
    return VerificationStore(db)
