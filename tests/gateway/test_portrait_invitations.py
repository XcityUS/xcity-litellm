from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import TypeAdapter

from gateway.providers.byteplus_assets import BytePlusAssetClient, BytePlusAssetSettings
from gateway.routes.portrait_invitations import (
    CompleteInvitation,
    CreateInvitation,
    StartInvitation,
    cancel_invitation,
    complete_invitation,
    create_invitation,
    list_invitations,
    router,
    start_invitation,
)
from gateway.routes.provider_assets import VerificationResultRequest, resolve_verification_result
from gateway.verification.service import CONSENT_VERSION, token_hash
from gateway.verification.store import Verification, VerificationSql, VerificationStore, get_store
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

TOKEN: Final = "a" * 43
JSON_OBJECT: Final = TypeAdapter(dict[str, object])


def record(**changes: object) -> Verification:
    now: Final = datetime.now(timezone.utc)
    return Verification.model_validate(
        {
            "id": "invitation-1",
            "owner_id": "creator",
            "token_hash": token_hash(TOKEN),
            "inviter_name": "Director",
            "person_name": "Actor",
            "callback_url": "https://studio.example/zh/face-invite",
            "locale": "zh",
            "created_at": now,
            "expires_at": now + timedelta(hours=24),
            **changes,
        }
    )


class MemoryStore:
    def __init__(self, item: Verification) -> None:
        self.rows = (item,)
        self.lock: Final = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[MemoryStore]:
        async with self.lock:
            yield self

    async def insert(self, record: Verification) -> None:
        self.rows = (*self.rows, record)

    async def one(self, field: Literal["id", "token_hash", "byted_token"], value: str) -> Verification:
        item: Final = next((row for row in self.rows if getattr(row, field) == value), None)
        if item is None:
            raise HTTPException(404, "INVITATION_NOT_FOUND")
        return item

    async def list(self, owner: str) -> tuple[Verification, ...]:
        return tuple(row for row in self.rows if row.owner_id == owner)

    async def limit(self, owner: str) -> None:
        if len(await self.list(owner)) >= 20:
            raise HTTPException(429, "INVITATION_LIMIT_REACHED")

    def update(self, id: str, changes: Mapping[str, object]) -> None:
        self.rows = tuple(row.model_copy(update=changes) if row.id == id else row for row in self.rows)

    async def session(self, id: str, token: str, link: str, consent: str) -> None:
        current: Final = await self.one("id", id)
        self.update(
            id,
            {
                "byted_token": token,
                "h5_link": link,
                "consent_version": consent,
                "consented_at": datetime.now(timezone.utc),
                "attempts": current.attempts + 1,
            },
        )

    async def complete(self, id: str, group: str) -> None:
        self.update(id, {"group_id": group, "h5_link": None})

    async def cancel(self, id: str) -> None:
        self.update(id, {"cancelled_at": datetime.now(timezone.utc), "h5_link": None})


class ProviderFixture:
    def __init__(self, *payloads: dict[str, object]) -> None:
        self.responses: Final = iter(payloads)
        self.calls: tuple[tuple[str, dict[str, object]], ...] = ()
        self.client: Final = BytePlusAssetClient(
            settings=BytePlusAssetSettings(
                access_key="fixture-ak",
                secret_key="fixture-sk",
                project_name="test",
                region="ap-southeast-1",
                host="provider.example",
            ),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls = (*self.calls, (request.url.params["Action"], JSON_OBJECT.validate_json(request.content)))
        return httpx.Response(200, json=next(self.responses))


def creator() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id="creator", api_key="test-key", models=["*"])


@pytest.mark.asyncio
async def test_creates_scoped_expiring_invitation_without_starting_a_provider_session() -> None:
    store: Final = MemoryStore(record())
    result: Final = await create_invitation(
        CreateInvitation(
            inviterName="Director", personName="Actor", locale="zh", callbackUrl="https://studio.example/zh/face-invite"
        ),
        creator(),
        store,
    )
    saved: Final = store.rows[-1]
    assert saved.owner_id == "creator"
    assert saved.token_hash == token_hash(str(result["token"]))
    assert saved.token_hash != result["token"]
    assert saved.byted_token is None
    assert saved.expires_at - saved.created_at == timedelta(hours=24)


@pytest.mark.asyncio
async def test_starts_only_after_consent_and_preserves_the_invitation_in_callback() -> None:
    store: Final = MemoryStore(record())
    fixture: Final = ProviderFixture(
        {"Result": {"H5Link": "https://verify.example/?signed=a%2Fb", "BytedToken": "session-1"}}
    )
    result: Final = await start_invitation(
        StartInvitation(token=TOKEN, consent=True, consentVersion=CONSENT_VERSION), store, fixture.client
    )
    assert result == {"h5Link": "https://verify.example/?signed=a%2Fb&lng=zh"}
    assert fixture.calls[0][1]["CallbackURL"] == f"https://studio.example/zh/face-invite?token={TOKEN}"
    saved: Final = store.rows[0]
    assert saved.byted_token == "session-1"
    assert saved.consented_at is not None
    assert saved.consent_version == CONSENT_VERSION


@pytest.mark.asyncio
async def test_concurrent_guest_callbacks_bind_once_to_creator_and_owner_refresh_sees_result() -> None:
    store: Final = MemoryStore(record(byted_token="session-1", consented_at=datetime.now(timezone.utc)))
    fixture: Final = ProviderFixture({"Result": {"GroupId": "group-1"}}, {"Result": {}})
    body: Final = CompleteInvitation(token=TOKEN, bytedToken="session-1")
    results: Final = await asyncio.gather(
        complete_invitation(body, store, fixture.client), complete_invitation(body, store, fixture.client)
    )
    assert results == [{"status": "verified"}, {"status": "verified"}]
    assert len(fixture.calls) == 2
    assert fixture.calls[1][0] == "UpdateAssetGroup"
    assert fixture.calls[1][1]["Name"] == "xcity:" + hashlib.sha256(b"creator").hexdigest()[:24]
    listed: Final = await list_invitations(creator(), store)
    rows: Final = TypeAdapter(tuple[dict[str, object], ...]).validate_python(listed["invitations"])
    assert rows[0]["groupId"] == "group-1"
    assert rows[0]["status"] == "verified"
    assert await store.list("other") == ()


@pytest.mark.asyncio
async def test_other_logged_in_user_cannot_claim_the_invitation_via_legacy_result_endpoint() -> None:
    store: Final = MemoryStore(record(byted_token="session-1"))
    fixture: Final = ProviderFixture()
    with pytest.raises(HTTPException) as caught:
        await resolve_verification_result(
            VerificationResultRequest(bytedToken="session-1"), UserAPIKeyAuth(user_id="other"), fixture.client, store
        )
    assert caught.value.status_code == 403
    assert fixture.calls == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}, 410),
        ({"cancelled_at": datetime.now(timezone.utc)}, 410),
        ({"byted_token": "different"}, 403),
        ({"consented_at": None}, 403),
    ],
)
async def test_rejects_expired_cancelled_mismatched_or_unconsented_completion(
    changes: dict[str, object], code: int
) -> None:
    store: Final = MemoryStore(
        record(byted_token="session-1", consented_at=datetime.now(timezone.utc)).model_copy(update=changes)
    )
    fixture: Final = ProviderFixture()
    with pytest.raises(HTTPException) as caught:
        await complete_invitation(CompleteInvitation(token=TOKEN, bytedToken="session-1"), store, fixture.client)
    assert caught.value.status_code == code
    assert fixture.calls == ()
    assert store.rows[0].group_id is None


@pytest.mark.asyncio
async def test_cannot_mark_verified_without_a_group_confirmed_by_provider() -> None:
    store: Final = MemoryStore(record(byted_token="session-1", consented_at=datetime.now(timezone.utc)))
    fixture: Final = ProviderFixture({"Result": {}})
    with pytest.raises(HTTPException) as caught:
        await complete_invitation(CompleteInvitation(token=TOKEN, bytedToken="session-1"), store, fixture.client)
    assert caught.value.status_code == 409
    assert store.rows[0].group_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("item,code", [(record(owner_id="someone-else"), 404), (record(group_id="group-1"), 409)])
async def test_cancellation_checks_owner_and_completion_state(item: Verification, code: int) -> None:
    store: Final = MemoryStore(item)
    with pytest.raises(HTTPException) as caught:
        await cancel_invitation(item.id, creator(), store)
    assert caught.value.status_code == code
    assert store.rows[0].cancelled_at is None


@pytest.mark.asyncio
async def test_cancelling_a_pending_invitation_prevents_new_sessions() -> None:
    store: Final = MemoryStore(record())
    assert await cancel_invitation("invitation-1", creator(), store) == {"status": "cancelled"}
    with pytest.raises(HTTPException) as caught:
        await start_invitation(
            StartInvitation(token=TOKEN, consent=True, consentVersion=CONSENT_VERSION), store, ProviderFixture().client
        )
    assert caught.value.status_code == 410


@pytest.mark.asyncio
async def test_fresh_session_replaces_old_attempt_and_limits_restarts() -> None:
    store: Final = MemoryStore(record(byted_token="old", h5_link="https://verify.example/old", attempts=2))
    fixture: Final = ProviderFixture({"Result": {"H5Link": "https://verify.example/new", "BytedToken": "new"}})
    body: Final = StartInvitation(token=TOKEN, consent=True, consentVersion=CONSENT_VERSION)
    assert await start_invitation(body, store, fixture.client) == {"h5Link": "https://verify.example/old"}
    assert fixture.calls == ()
    await start_invitation(body.model_copy(update={"restart": True}), store, fixture.client)
    assert store.rows[0].byted_token == "new"
    assert store.rows[0].attempts == 3
    with pytest.raises(HTTPException) as caught:
        await start_invitation(body.model_copy(update={"restart": True}), store, fixture.client)
    assert caught.value.detail == "VERIFICATION_ATTEMPTS_EXHAUSTED"


@pytest.mark.asyncio
async def test_guest_http_route_is_account_free_and_requires_consent() -> None:
    app: Final = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: MemoryStore(record())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/v1/provider-assets/invitations/inspect", json={"token": TOKEN})
        assert response.status_code == 200
        payload: Final = JSON_OBJECT.validate_json(response.content)
        assert payload["inviterName"] == "Director"
        assert "owner_id" not in payload
        assert "token_hash" not in payload
        rejected: Final = await client.post(
            "/v1/provider-assets/invitations/start",
            json={"token": TOKEN, "consent": False, "consentVersion": CONSENT_VERSION},
        )
        assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_invitation_http_lifecycle_with_prisma_postgres() -> None:
    database_url: Final = os.environ.get("PORTRAIT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PORTRAIT_TEST_DATABASE_URL is required for the PostgreSQL regression")

    from prisma import Prisma
    from prisma.types import DatasourceOverride

    owner: Final = f"portrait-regression-{uuid4()}"
    db: Final = Prisma(datasource=DatasourceOverride(url=database_url))
    await db.connect()
    try:
        store: Final = VerificationStore(cast(VerificationSql, db))
        app: Final = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id=owner)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created: Final = await client.post(
                "/v1/provider-assets/invitations",
                json={
                    "inviterName": "Director",
                    "personName": "Actor",
                    "locale": "zh",
                    "callbackUrl": "http://localhost:3000/zh/face-invite",
                },
            )
            assert created.status_code == 200
            payload: Final = JSON_OBJECT.validate_json(created.content)
            token: Final = TypeAdapter(str).validate_python(payload["token"])
            invitation_id: Final = TypeAdapter(str).validate_python(payload["id"])
            saved: Final = await store.one("id", invitation_id)
            assert saved.owner_id == owner
            assert saved.token_hash == token_hash(token)
            assert saved.expires_at.tzinfo is not None
            assert saved.created_at.tzinfo is not None
            assert saved.expires_at - saved.created_at == timedelta(hours=24)

            listed: Final = await client.get("/v1/provider-assets/invitations")
            assert listed.status_code == 200
            rows: Final = TypeAdapter(list[dict[str, object]]).validate_python(
                JSON_OBJECT.validate_json(listed.content)["invitations"]
            )
            assert len(rows) == 1
            assert rows[0]["id"] == invitation_id
            assert rows[0]["status"] == "pending"
            assert "token" not in rows[0]

            inspected: Final = await client.post("/v1/provider-assets/invitations/inspect", json={"token": token})
            assert inspected.status_code == 200
            assert JSON_OBJECT.validate_json(inspected.content)["status"] == "pending"
            cancelled: Final = await client.delete(f"/v1/provider-assets/invitations/{invitation_id}")
            assert cancelled.status_code == 200
            assert (await store.one("id", invitation_id)).cancelled_at is not None
    finally:
        await db.execute_raw('DELETE FROM "LiteLLM_AssetVerification" WHERE owner_id=$1', owner)
        await db.disconnect()
