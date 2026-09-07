from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, TypeAlias
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

API_VERSION: Final = "2024-01-01"
SERVICE: Final = "ark"
REQUEST_TYPE: Final = "request"
SIGNED_HEADERS: Final = "content-type;host;x-content-sha256;x-date"
JSON_OBJECT: Final = TypeAdapter(dict[str, object])


@dataclass(frozen=True, slots=True)
class BytePlusAssetSettings:
    access_key: str
    secret_key: str
    project_name: str
    region: str
    host: str

    @classmethod
    def from_env(cls) -> BytePlusAssetSettings | None:
        access_key: Final = os.getenv("BYTEPLUS_AK", "").strip()
        secret_key: Final = os.getenv("BYTEPLUS_SK", "").strip()
        if not access_key or not secret_key:
            return None

        region: Final = os.getenv("BYTEPLUS_REGION", "ap-southeast-1").strip() or "ap-southeast-1"
        host: Final = os.getenv("BYTEPLUS_ARK_API_HOST", f"ark.{region}.byteplusapi.com").strip()
        return cls(
            access_key=access_key,
            secret_key=secret_key,
            project_name=os.getenv("ARK_PROJECT_NAME", "default").strip() or "default",
            region=region,
            host=host or f"ark.{region}.byteplusapi.com",
        )


@dataclass(frozen=True, slots=True)
class ProviderSuccess:
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    status_code: int
    message: str


ProviderResult: TypeAlias = ProviderSuccess | ProviderFailure


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode(), usedforsecurity=True).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def signed_headers(
    action: str,
    body_text: str,
    settings: BytePlusAssetSettings,
    x_date: str,
) -> Mapping[str, str]:
    payload_hash: Final = _sha256_hex(body_text)
    date: Final = x_date[:8]
    canonical_query: Final = f"Action={quote(action, safe='')}&Version={quote(API_VERSION, safe='')}"
    canonical_headers: Final = (
        "content-type:application/json\n"
        f"host:{settings.host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request: Final = "\n".join(
        ("POST", "/", canonical_query, canonical_headers, SIGNED_HEADERS, payload_hash)
    )
    credential_scope: Final = f"{date}/{settings.region}/{SERVICE}/{REQUEST_TYPE}"
    string_to_sign: Final = "\n".join(
        ("HMAC-SHA256", x_date, credential_scope, _sha256_hex(canonical_request))
    )
    date_key: Final = _hmac(settings.secret_key.encode(), date)
    region_key: Final = _hmac(date_key, settings.region)
    service_key: Final = _hmac(region_key, SERVICE)
    signing_key: Final = _hmac(service_key, REQUEST_TYPE)
    signature: Final = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization: Final = (
        f"HMAC-SHA256 Credential={settings.access_key}/{credential_scope}, "
        f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
    )
    return {
        "Content-Type": "application/json",
        "Host": settings.host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }


def _record(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    try:
        return JSON_OBJECT.validate_python(value)
    except ValidationError:
        return None


def _provider_error(payload: Mapping[str, object]) -> object | None:
    metadata: Final = _record(payload.get("ResponseMetadata"))
    return metadata.get("Error") if metadata else None


class BytePlusAssetClient:
    def __init__(
        self,
        settings: BytePlusAssetSettings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings: Final = settings or BytePlusAssetSettings.from_env()
        self._http_client: Final = http_client

    @property
    def configured(self) -> bool:
        return self._settings is not None

    @property
    def project_name(self) -> str:
        return self._settings.project_name if self._settings else os.getenv("ARK_PROJECT_NAME", "default")

    async def _post(self, url: str, headers: Mapping[str, str], body_text: str) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(url, headers=headers, content=body_text)

        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.post(url, headers=headers, content=body_text)

    async def call(self, action: str, body: Mapping[str, object]) -> ProviderResult:
        settings: Final = self._settings
        if settings is None:
            return ProviderFailure(status_code=503, message="BytePlus asset library credentials are not configured")

        body_text: Final = json.dumps(
            {**body, "ProjectName": settings.project_name}, separators=(",", ":"), ensure_ascii=False
        )
        x_date: Final = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        query: Final = f"Action={quote(action, safe='')}&Version={quote(API_VERSION, safe='')}"
        url: Final = f"https://{settings.host}/?{query}"
        headers: Final = signed_headers(action, body_text, settings, x_date)

        try:
            response: Final = await self._post(url, headers, body_text)
            payload: Final = JSON_OBJECT.validate_python(response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            return ProviderFailure(status_code=502, message=f"BytePlus asset request failed: {exc}")

        provider_error: Final = _provider_error(payload)
        if response.is_error or provider_error is not None:
            message: Final = json.dumps(provider_error or payload, ensure_ascii=False, default=str)
            return ProviderFailure(status_code=502, message=message)
        return ProviderSuccess(payload=payload)
