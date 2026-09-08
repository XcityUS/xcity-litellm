from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import pytest
from fastapi import HTTPException

from gateway.providers.byteplus_assets import (
    BytePlusAssetClient,
    BytePlusAssetSettings,
    ProviderFailure,
    ProviderResult,
    ProviderSuccess,
    signed_headers,
)
from gateway.routes.provider_assets import (
    CreateAssetRequest,
    _is_owned_group,
    _owned_group_name,
    create_provider_asset,
    get_provider_asset,
)
from litellm.proxy._types import UserAPIKeyAuth


class StubAssetClient(BytePlusAssetClient):
    def __init__(self, responses: Mapping[str, Mapping[str, object]]) -> None:
        self._responses: Final = responses

    async def call(self, action: str, body: Mapping[str, object]) -> ProviderResult:
        payload: Final = self._responses.get(action)
        if payload is None:
            return ProviderFailure(status_code=500, message=f"Unexpected action: {action}")
        return ProviderSuccess(payload=payload)


def auth(user_id: str = "user-1") -> UserAPIKeyAuth:
    return UserAPIKeyAuth(api_key="test-key", user_id=user_id, models=["*"])


def test_signed_headers_match_known_byteplus_vector() -> None:
    settings: Final = BytePlusAssetSettings(
        access_key="test-ak",
        secret_key="test-sk",
        project_name="default",
        region="ap-southeast-1",
        host="ark.ap-southeast-1.byteplusapi.com",
    )

    headers: Final = signed_headers(
        "ListAssetGroups",
        '{"ProjectName":"default"}',
        settings,
        "20260328T000000Z",
    )

    assert headers["X-Content-Sha256"] == "7e0a39e645f91c0fa13aea4108715f9f85a3f55f8d3e6af22c41b5b9d11fbde3"
    assert headers["Authorization"].endswith(
        "Signature=e0ce75a786c716cb3db1e647236ec2d9cc37e410a7f54cf82cfa2b5ddf7e0b7d"
    )


def test_owned_group_name_fits_byteplus_limit_for_long_user_id() -> None:
    user_id: Final = "user-with-a-very-long-production-identifier-that-cannot-fit-in-an-asset-group-name"

    name: Final = _owned_group_name(user_id, "reviewed-materials-with-an-equally-long-description")

    assert len(name) <= 64
    assert _is_owned_group(name, user_id)


def test_legacy_owned_group_name_remains_recognized() -> None:
    assert _is_owned_group("xcity:user-1:hero", "user-1")


@pytest.mark.asyncio
async def test_create_asset_returns_state_after_owner_check() -> None:
    client: Final = StubAssetClient(
        {
            "GetAssetGroup": {"Result": {"Name": "xcity:user-1:hero"}},
            "CreateAsset": {"Result": {"AssetId": "asset-1"}},
        }
    )
    request: Final = CreateAssetRequest(
        groupId="group-1",
        url="https://media.xcity.ai/media/u/user-1/hero.png",
        name="Hero",
    )

    result: Final = await create_provider_asset(request, auth(), client)

    assert result == {"assetId": "asset-1", "status": "Processing"}


@pytest.mark.asyncio
async def test_get_asset_rejects_another_users_group() -> None:
    client: Final = StubAssetClient(
        {
            "GetAsset": {"Result": {"AssetId": "asset-1", "GroupId": "group-2", "Status": "Active"}},
            "GetAssetGroup": {"Result": {"Name": "xcity:user-2:hero"}},
        }
    )

    with pytest.raises(HTTPException) as caught:
        await get_provider_asset("asset-1", auth(), client)

    assert caught.value.status_code == 403
