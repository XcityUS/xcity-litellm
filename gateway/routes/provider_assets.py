from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from gateway.providers.byteplus_assets import (
    BytePlusAssetClient,
    ProviderFailure,
    ProviderSuccess,
)
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

router: Final = APIRouter(prefix="/v1/provider-assets", tags=["provider-assets"])
AssetGroupType = Literal["LivenessFace", "AIGC"]
AssetType = Literal["Image", "Video", "Audio"]
GROUP_PAGE_SIZE: Final = 100
MAX_GROUP_PAGES: Final = 10
MAX_ASSET_NAME_LENGTH: Final = 64
JSON_OBJECT: Final = TypeAdapter(dict[str, object])
JSON_LIST: Final = TypeAdapter(list[object])


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)


class CreateAssetRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: str = Field(alias="groupId", min_length=1)
    url: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=80)
    asset_type: AssetType = Field(default="Image", alias="assetType")


class VerificationSessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    callback_url: str = Field(alias="callbackUrl", min_length=1)


class VerificationResultRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    byted_token: str = Field(alias="bytedToken", min_length=1)


def get_asset_client() -> BytePlusAssetClient:
    return BytePlusAssetClient()


def _record(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    try:
        return JSON_OBJECT.validate_python(value)
    except ValidationError:
        return None


def _object_list(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        return ()
    try:
        return tuple(JSON_LIST.validate_python(value))
    except ValidationError:
        return ()


def _result_root(payload: Mapping[str, object]) -> Mapping[str, object]:
    result: Final = _record(payload.get("Result"))
    return result or payload


def _string_field(record: Mapping[str, object], *names: str) -> str:
    values: Final = (record.get(name) for name in names)
    return next((value.strip() for value in values if isinstance(value, str) and value.strip()), "")


def _provider_asset_name(name: str) -> str:
    normalized: Final = name.strip()
    if len(normalized) <= MAX_ASSET_NAME_LENGTH:
        return normalized
    digest: Final = hashlib.sha256(normalized.encode(), usedforsecurity=True).hexdigest()[:12]
    prefix_length: Final = MAX_ASSET_NAME_LENGTH - len(digest) - 1
    return f"{normalized[:prefix_length]}-{digest}"


async def _payload(client: BytePlusAssetClient, action: str, body: Mapping[str, object]) -> Mapping[str, object]:
    result: Final = await client.call(action, body)
    match result:
        case ProviderSuccess(payload=payload):
            return payload
        case ProviderFailure(status_code=status_code, message=message):
            raise HTTPException(status_code=status_code, detail=message)


def _user_id(auth: UserAPIKeyAuth) -> str:
    user_id: Final = auth.user_id.strip() if isinstance(auth.user_id, str) else ""
    if not user_id:
        raise HTTPException(status_code=403, detail="The API key is not linked to a user")
    return user_id


def _owner_tag(user_id: str) -> str:
    digest: Final = hashlib.sha256(user_id.encode(), usedforsecurity=True).hexdigest()[:24]
    return f"xcity:{digest}"


def _legacy_owner_tag(user_id: str) -> str:
    return f"xcity:{user_id}"


def _group_slug(name: str) -> str:
    normalized: Final = re.sub(r"[^a-z0-9]+", "-", name.lower())
    slug: Final = re.sub(r"^-+|-+$", "", re.sub(r"-+", "-", normalized))[:40]
    return slug.rstrip("-") or f"character-{hashlib.sha256(name.encode(), usedforsecurity=True).hexdigest()[:10]}"


def _owned_group_name(user_id: str, slug: str) -> str:
    tag: Final = _owner_tag(user_id)
    available: Final = 64 - len(tag) - 1
    return f"{tag}:{slug[:available].rstrip('-')}"


def _is_owned_group(name: str, user_id: str) -> bool:
    tags: Final = (_owner_tag(user_id), _legacy_owner_tag(user_id))
    return any(name == tag or name.startswith(f"{tag}:") for tag in tags)


async def _require_owned_group(client: BytePlusAssetClient, group_id: str, user_id: str) -> None:
    payload: Final = await _payload(client, "GetAssetGroup", {"Id": group_id})
    root: Final = _result_root(payload)
    group: Final = _record(root.get("Group")) or _record(root.get("AssetGroup")) or root
    if not _is_owned_group(_string_field(group, "Name", "name"), user_id):
        raise HTTPException(status_code=403, detail="Asset group is outside the caller's namespace")


def _group_records(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    root: Final = _result_root(payload)
    data: Final = _record(root.get("Data"))
    names: Final = ("Groups", "AssetGroups", "AssetGroupList", "GroupList", "Items", "List")
    source: Final = next((_object_list(root.get(name)) for name in names if isinstance(root.get(name), list)), ())
    nested: Final = next(
        (_object_list(data.get(name)) for name in names if data and isinstance(data.get(name), list)),
        (),
    )
    items: Final = source or nested
    return tuple(record for item in items if (record := _record(item)) is not None)


def _asset_records(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    root: Final = _result_root(payload)
    data: Final = _record(root.get("Data"))
    names: Final = ("Assets", "AssetList", "Items", "List")
    source: Final = next((_object_list(root.get(name)) for name in names if isinstance(root.get(name), list)), ())
    nested: Final = next(
        (_object_list(data.get(name)) for name in names if data and isinstance(data.get(name), list)),
        (),
    )
    items: Final = source or nested
    return tuple(record for item in items if (record := _record(item)) is not None)


async def _list_groups(
    client: BytePlusAssetClient,
    group_type: AssetGroupType,
    page: int = 1,
    accumulated: tuple[Mapping[str, object], ...] = (),
) -> tuple[Mapping[str, object], ...]:
    payload: Final = await _payload(
        client,
        "ListAssetGroups",
        {"Filter": {"GroupType": group_type}, "PageNumber": page, "PageSize": GROUP_PAGE_SIZE},
    )
    page_groups: Final = _group_records(payload)
    groups: Final = (*accumulated, *page_groups)
    if len(page_groups) < GROUP_PAGE_SIZE or page >= MAX_GROUP_PAGES:
        return groups
    return await _list_groups(client, group_type, page + 1, groups)


async def _list_assets(
    client: BytePlusAssetClient,
    group_ids: tuple[str, ...],
    group_type: AssetGroupType,
    page: int = 1,
    accumulated: tuple[Mapping[str, object], ...] = (),
) -> tuple[Mapping[str, object], ...]:
    payload: Final = await _payload(
        client,
        "ListAssets",
        {
            "Filter": {"GroupIds": group_ids, "GroupType": group_type},
            "PageNumber": page,
            "PageSize": GROUP_PAGE_SIZE,
            "SortBy": "CreateTime",
            "SortOrder": "Desc",
        },
    )
    page_assets: Final = _asset_records(payload)
    assets: Final = (*accumulated, *page_assets)
    if len(page_assets) < GROUP_PAGE_SIZE or page >= MAX_GROUP_PAGES:
        return assets
    return await _list_assets(client, group_ids, group_type, page + 1, assets)


def _asset_failure_reason(asset: Mapping[str, object]) -> str:
    error: Final = _record(asset.get("Error"))
    return _string_field(asset, "Reason", "Message", "ErrorMessage", "failureReason") or (
        _string_field(error, "Message", "message") if error else ""
    )


def _allowed_asset_url(value: str) -> str:
    parsed: Final = urlparse(value.strip())
    allowed_hosts: Final = tuple(
        host.strip().lower()
        for host in os.getenv("PROVIDER_ASSET_ALLOWED_HOSTS", "media.xcity.ai").split(",")
        if host.strip()
    )
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts:
        raise HTTPException(status_code=400, detail="Asset URL must use HTTPS on an allowed Xcity media host")
    return value.strip()


def _provider_download_url(value: str) -> str:
    allowed: Final = _allowed_asset_url(value)
    parsed: Final = urlparse(allowed)
    if not parsed.path.startswith("/media/"):
        return allowed
    return parsed._replace(path=f"/download/{parsed.path.removeprefix('/media/')}").geturl()


@router.get("/status")
async def provider_asset_status(
    _auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    if not client.configured:
        return {
            "configured": False,
            "ok": False,
            "livenessOk": False,
            "aigcOk": False,
            "projectName": client.project_name,
            "error": "BYTEPLUS_AK / BYTEPLUS_SK are not set on the gateway",
        }

    liveness, aigc = await asyncio.gather(
        client.call(
            "ListAssetGroups",
            {"Filter": {"GroupType": "LivenessFace"}, "PageNumber": 1, "PageSize": 1},
        ),
        client.call(
            "ListAssetGroups",
            {"Filter": {"GroupType": "AIGC"}, "PageNumber": 1, "PageSize": 1},
        ),
    )
    liveness_error: Final = liveness.message if isinstance(liveness, ProviderFailure) else ""
    aigc_error: Final = aigc.message if isinstance(aigc, ProviderFailure) else ""
    errors: Final = tuple(
        message
        for message in (
            f"LivenessFace: {liveness_error}" if liveness_error else "",
            f"AIGC: {aigc_error}" if aigc_error else "",
        )
        if message
    )
    return {
        "configured": True,
        "ok": not errors,
        "livenessOk": not liveness_error,
        "aigcOk": not aigc_error,
        "projectName": client.project_name,
        **({"error": "; ".join(errors)} if errors else {}),
    }


@router.get("/groups")
async def list_provider_asset_groups(
    type: Literal["liveness", "aigc", "all"] = Query(default="liveness"),
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    requested_types: Final[tuple[AssetGroupType, ...]] = (
        ("LivenessFace",) if type == "liveness" else ("AIGC",) if type == "aigc" else ("LivenessFace", "AIGC")
    )
    pages: Final = await asyncio.gather(*(_list_groups(client, group_type) for group_type in requested_types))
    user_id: Final = _user_id(auth)
    groups: Final = tuple(
        {
            "id": _string_field(group, "Id", "ID", "GroupId", "groupId", "AssetGroupId"),
            "name": _string_field(group, "Name", "name"),
            "groupType": "AIGC" if _string_field(group, "GroupType", "groupType") == "AIGC" else group_type,
        }
        for group_type, page in zip(requested_types, pages)
        for group in page
        if _string_field(group, "Id", "ID", "GroupId", "groupId", "AssetGroupId")
        and _is_owned_group(_string_field(group, "Name", "name"), user_id)
    )
    return {"groups": groups}


@router.post("/groups")
async def create_provider_asset_group(
    request: CreateGroupRequest,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    name: Final = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    user_id: Final = _user_id(auth)
    slug: Final = _group_slug(name)
    group_name: Final = _owned_group_name(user_id, slug)
    groups: Final = await _list_groups(client, "AIGC")
    existing: Final = next(
        (
            _string_field(group, "Id", "ID", "GroupId", "groupId", "AssetGroupId")
            for group in groups
            if _string_field(group, "Name", "name") == group_name
        ),
        "",
    )
    if existing:
        return {"groupId": existing, "slug": slug, "created": False}

    payload: Final = await _payload(
        client,
        "CreateAssetGroup",
        {"Name": group_name, "Description": name, "GroupType": "AIGC"},
    )
    group_id: Final = _string_field(_result_root(payload), "Id", "ID", "GroupId", "groupId", "AssetGroupId")
    if not group_id:
        raise HTTPException(status_code=502, detail="BytePlus returned no asset group ID")
    return {"groupId": group_id, "slug": slug, "created": True}


@router.delete("/groups/{group_id}")
async def delete_provider_asset_group(
    group_id: str,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    normalized_group_id: Final = group_id.strip()
    if not normalized_group_id:
        raise HTTPException(status_code=400, detail="Asset group ID is required")
    await _require_owned_group(client, normalized_group_id, _user_id(auth))
    await _payload(client, "DeleteAssetGroup", {"Id": normalized_group_id})
    return {}


@router.get("")
async def list_provider_assets(
    type: Literal["liveness", "aigc", "all"] = Query(default="all"),
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    requested_types: Final[tuple[AssetGroupType, ...]] = (
        ("LivenessFace",) if type == "liveness" else ("AIGC",) if type == "aigc" else ("LivenessFace", "AIGC")
    )
    pages: Final = await asyncio.gather(*(_list_groups(client, group_type) for group_type in requested_types))
    user_id: Final = _user_id(auth)
    owned_groups: Final = tuple(
        (
            _string_field(group, "Id", "ID", "GroupId", "groupId", "AssetGroupId"),
            "AIGC" if _string_field(group, "GroupType", "groupType") == "AIGC" else group_type,
        )
        for group_type, page in zip(requested_types, pages)
        for group in page
        if _string_field(group, "Id", "ID", "GroupId", "groupId", "AssetGroupId")
        and _is_owned_group(_string_field(group, "Name", "name"), user_id)
    )
    group_types: Final = dict(owned_groups)
    group_queries: Final = tuple(
        (group_type, tuple(group_id for group_id, current_type in owned_groups if current_type == group_type))
        for group_type in requested_types
        if any(current_type == group_type for _, current_type in owned_groups)
    )
    if not group_queries:
        return {"assets": ()}

    pages_by_type: Final = await asyncio.gather(
        *(_list_assets(client, group_ids, group_type) for group_type, group_ids in group_queries)
    )
    records: Final = tuple(asset for page in pages_by_type for asset in page)
    assets: Final = tuple(
        {
            "assetId": asset_id,
            "groupId": group_id,
            "groupType": group_types[group_id],
            "name": _string_field(asset, "Name", "name"),
            "previewUrl": _string_field(asset, "URL", "Url", "url", "PreviewUrl", "previewUrl"),
            "assetType": _string_field(asset, "AssetType", "assetType") or "Image",
            "status": _string_field(asset, "Status", "status"),
            "failureReason": _asset_failure_reason(asset),
            "createdAt": _string_field(asset, "CreateTime", "createTime", "CreatedAt", "createdAt"),
            "updatedAt": _string_field(asset, "UpdateTime", "updateTime", "UpdatedAt", "updatedAt"),
        }
        for asset in records
        if (asset_id := _string_field(asset, "Id", "ID", "AssetId", "assetId"))
        and (group_id := _string_field(asset, "GroupId", "groupId", "AssetGroupId")) in group_types
    )
    return {"assets": assets}


@router.post("")
async def create_provider_asset(
    request: CreateAssetRequest,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    await _require_owned_group(client, request.group_id.strip(), _user_id(auth))

    payload: Final = await _payload(
        client,
        "CreateAsset",
        {
            "GroupId": request.group_id.strip(),
            "URL": _provider_download_url(request.url),
            "Name": _provider_asset_name(request.name),
            "AssetType": request.asset_type,
        },
    )
    root: Final = _result_root(payload)
    asset: Final = _record(root.get("Asset")) or _record(root.get("AssetInfo")) or root
    asset_id: Final = _string_field(asset, "Id", "ID", "AssetId", "assetId")
    if not asset_id:
        raise HTTPException(status_code=502, detail="BytePlus returned no asset ID")
    return {"assetId": asset_id, "status": "Processing"}


@router.get("/{asset_id}")
async def get_provider_asset(
    asset_id: str,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    payload: Final = await _payload(client, "GetAsset", {"Id": asset_id.strip()})
    root: Final = _result_root(payload)
    asset: Final = _record(root.get("Asset")) or _record(root.get("AssetInfo")) or root
    group_id: Final = _string_field(asset, "GroupId", "groupId", "AssetGroupId")
    if not group_id:
        raise HTTPException(status_code=502, detail="BytePlus returned no asset group ID")
    await _require_owned_group(client, group_id, _user_id(auth))
    return {
        "assetId": asset_id.strip(),
        "groupId": group_id,
        "status": _string_field(asset, "Status", "status"),
        "previewUrl": _string_field(asset, "URL", "Url", "url", "PreviewUrl", "previewUrl"),
        "failureReason": _string_field(asset, "Reason", "Message", "ErrorMessage", "failureReason"),
    }


@router.post("/verification-sessions")
async def create_verification_session(
    request: VerificationSessionRequest,
    _auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    callback: Final = urlparse(request.callback_url.strip())
    if callback.scheme not in ("http", "https") or not callback.netloc:
        raise HTTPException(status_code=400, detail="A valid callback URL is required")
    payload: Final = await _payload(
        client, "CreateVisualValidateSession", {"CallbackURL": request.callback_url.strip()}
    )
    root: Final = _result_root(payload)
    h5_link: Final = _string_field(root, "H5Link", "h5Link")
    byted_token: Final = _string_field(root, "BytedToken", "bytedToken")
    if not h5_link or not byted_token:
        raise HTTPException(status_code=502, detail="BytePlus returned an incomplete verification session")
    return {"h5Link": h5_link, "bytedToken": byted_token}


@router.post("/verification-results")
async def resolve_verification_result(
    request: VerificationResultRequest,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
    client: BytePlusAssetClient = Depends(get_asset_client),
):
    payload: Final = await _payload(client, "GetVisualValidateResult", {"BytedToken": request.byted_token.strip()})
    root: Final = _result_root(payload)
    group_id: Final = _string_field(root, "GroupId", "groupId", "Id", "id")
    if not group_id:
        raise HTTPException(status_code=502, detail="BytePlus returned no verified group ID")
    await _payload(client, "UpdateAssetGroup", {"Id": group_id, "Name": _owner_tag(_user_id(auth))})
    return {"groupId": group_id}
