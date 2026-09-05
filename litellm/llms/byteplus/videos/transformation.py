import math
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from httpx._types import RequestFiles

import litellm
from litellm.litellm_core_utils.url_utils import encode_url_path_segment
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.base_llm.videos.transformation import BaseVideoConfig
from litellm.llms.custom_httpx.http_handler import (
    AsyncHTTPHandler,
    HTTPHandler,
    _get_httpx_client,
    get_async_httpx_client,
)
from litellm.secret_managers.main import get_secret_str
from litellm.types.router import GenericLiteLLMParams
from litellm.types.videos.main import VideoCreateOptionalRequestParams, VideoObject
from litellm.types.videos.utils import (
    encode_video_id_with_provider,
    extract_original_video_id,
)

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as _LiteLLMLoggingObj

    LiteLLMLoggingObj = _LiteLLMLoggingObj
else:
    LiteLLMLoggingObj = Any


DEFAULT_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
VIDEO_TASKS_ENDPOINT = "contents/generations/tasks"


# Video ids whose successful completion has already been billed, with the
# timestamp of that charge. Ark keeps a task for 7 days, so entries older than
# that can never be polled again.
_CHARGED_VIDEO_IDS: dict[str, float] = {}
_CHARGE_TTL_SECONDS = 7 * 24 * 60 * 60
_CHARGE_CACHE_MAX = 10_000


def _claim_video_charge(video_id: str | None) -> bool:
    """
    True the first time a given video id is seen as successfully completed.

    In-process by design: the gateway runs as a single replica, and the
    alternative (a Redis round trip from the transform layer) would drag proxy
    internals into the provider config. If this service is ever scaled out,
    move this claim into the shared DualCache — otherwise each replica could
    bill the same video once.
    """
    if not video_id:
        return True
    now = time.time()
    if len(_CHARGED_VIDEO_IDS) > _CHARGE_CACHE_MAX:
        for stale, seen_at in list(_CHARGED_VIDEO_IDS.items()):
            if now - seen_at > _CHARGE_TTL_SECONDS:
                _CHARGED_VIDEO_IDS.pop(stale, None)
    seen_at = _CHARGED_VIDEO_IDS.get(video_id)
    if seen_at is not None and now - seen_at <= _CHARGE_TTL_SECONDS:
        return False
    _CHARGED_VIDEO_IDS[video_id] = now
    return True


class BytePlusVideoConfig(BaseVideoConfig):
    """
    BytePlus (ByteDance Ark) video generation (seedance / dreamina models).

    Task-based async API:
    1. POST /contents/generations/tasks  -> {"id": "cgt-..."}
    2. GET  /contents/generations/tasks/{id} -> {status, content: {video_url}, usage, ...}

    Reference: https://docs.byteplus.com/en/docs/ModelArk/1520757
    """

    def __init__(self):
        super().__init__()

    def get_supported_openai_params(self, model: str) -> list:
        return [
            "model",
            "prompt",
            "input_reference",
            "seconds",
            "size",
            "user",
            "extra_headers",
        ]

    def map_openai_params(
        self,
        video_create_optional_params: VideoCreateOptionalRequestParams,
        model: str,
        drop_params: bool,
    ) -> dict:
        """
        Map OpenAI video params to BytePlus:
        - size "1280x720" -> ratio "16:9" (pixels reduced to an aspect ratio)
        - seconds -> duration (int)
        - input_reference (image url) -> kept; assembled into the content array
          in transform_video_create_request
        - BytePlus-specific params (generate_audio, watermark, ratio, ...) pass through
        """
        mapped_params: dict[str, Any] = {}

        if "input_reference" in video_create_optional_params:
            mapped_params["input_reference"] = video_create_optional_params["input_reference"]

        if "size" in video_create_optional_params:
            # OpenAI `size` is pixel dimensions ("1280x720"); BytePlus `ratio` is an
            # aspect ratio ("16:9"). Reduce the dimensions to lowest terms.
            size = video_create_optional_params["size"]
            if isinstance(size, str) and "x" in size:
                try:
                    w, h = (int(p) for p in size.lower().split("x", 1))
                    g = math.gcd(w, h) or 1
                    mapped_params["ratio"] = f"{w // g}:{h // g}"
                except (ValueError, TypeError):
                    pass

        if "seconds" in video_create_optional_params:
            seconds = video_create_optional_params["seconds"]
            if seconds is not None:
                try:
                    mapped_params["duration"] = int(float(seconds)) if isinstance(seconds, str) else int(seconds)
                except (ValueError, TypeError):
                    pass

        # Pass through provider-specific params (generate_audio, watermark, etc.)
        supported_openai_params = self.get_supported_openai_params(model)
        for key, value in video_create_optional_params.items():
            if key not in supported_openai_params:
                mapped_params[key] = value

        return mapped_params

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: str | None = None,
        litellm_params: GenericLiteLLMParams | None = None,
    ) -> dict:
        if litellm_params and litellm_params.api_key:
            api_key = api_key or litellm_params.api_key

        api_key = api_key or litellm.api_key or get_secret_str("BYTEPLUS_API_KEY")

        if api_key is None:
            raise ValueError(
                "BYTEPLUS_API_KEY is required. Set BYTEPLUS_API_KEY environment variable or pass api_key parameter."
            )

        headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        return headers

    def get_complete_url(
        self,
        model: str,
        api_base: str | None,
        litellm_params: dict,
    ) -> str:
        api_base = api_base or get_secret_str("BYTEPLUS_API_BASE") or DEFAULT_BASE_URL
        return api_base.rstrip("/")

    def transform_video_create_request(
        self,
        model: str,
        prompt: str,
        api_base: str,
        video_create_optional_request_params: dict,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> tuple[dict, RequestFiles, str]:
        """
        BytePlus expects:
        {
          "model": "seedance-1-5-pro-251215",
          "content": [
            {"type": "text", "text": "..."},
            {"type": "image_url", "image_url": {"url": "..."}}   # optional
          ],
          "ratio": "16:9", "duration": 5, "generate_audio": true, ...
        }

        `input_reference` accepts:
        - a URL string: single first-frame image (no `role`, output ratio
          follows the image — BytePlus TaskTypeConstraint);
        - a list of URL strings: multi-reference mode, each entry sent with
          `role: "reference_image"` (Seedance 2.0/2.5 accept 1-9, prompts cite
          them as [Image 1], [Image 2], ...);
        - a list of {"url": ..., "role": ...} dicts for explicit roles
          (e.g. "first_frame" / "last_frame").
        """
        params = dict(video_create_optional_request_params)

        content: list[dict[str, Any]] = []
        if prompt:
            content.append({"type": "text", "text": prompt})

        input_reference = params.pop("input_reference", None)
        if isinstance(input_reference, (list, tuple)):
            for item in input_reference:
                if isinstance(item, dict) and item.get("url"):
                    # The role picks the content type: reference_video /
                    # reference_audio become video_url / audio_url items
                    # (multimodal reference mode); every other role is an image.
                    role = item.get("role") or "reference_image"
                    if role == "reference_video":
                        content.append(
                            {
                                "type": "video_url",
                                "video_url": {"url": item["url"]},
                                "role": role,
                            }
                        )
                    elif role == "reference_audio":
                        content.append(
                            {
                                "type": "audio_url",
                                "audio_url": {"url": item["url"]},
                                "role": role,
                            }
                        )
                    else:
                        content.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": item["url"]},
                                "role": role,
                            }
                        )
                elif isinstance(item, str) and item:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": item},
                            "role": "reference_image",
                        }
                    )
        elif input_reference:
            content.append({"type": "image_url", "image_url": {"url": input_reference}})

        request_data: dict[str, Any] = {"model": model, "content": content}
        request_data.update(params)

        files_list: list[tuple[str, Any]] = []
        full_api_base = f"{api_base}/{VIDEO_TASKS_ENDPOINT}"
        return request_data, files_list, full_api_base

    def _map_status(self, status: str | None) -> str:
        """Map BytePlus task status to OpenAI video status."""
        status_map = {
            "queued": "queued",
            "running": "in_progress",
            "succeeded": "completed",
            "failed": "failed",
            "cancelled": "failed",
        }
        return status_map.get((status or "").lower(), "queued")

    def _build_video_object(
        self,
        response_data: dict[str, Any],
        model: str | None,
        custom_llm_provider: str | None,
        request_data: dict | None = None,
    ) -> VideoObject:
        video_data: dict[str, Any] = {
            "id": response_data.get("id", ""),
            "object": "video",
            "status": self._map_status(response_data.get("status")),
            "created_at": int(response_data.get("created_at") or 0),
        }

        content = response_data.get("content") or {}
        if isinstance(content, dict) and content.get("video_url"):
            video_data["output_url"] = content["video_url"]
        if isinstance(content, dict) and content.get("last_frame_url"):
            video_data["last_frame_url"] = content["last_frame_url"]
        if response_data.get("seed") is not None:
            video_data["seed"] = response_data["seed"]

        if response_data.get("updated_at"):
            video_data["completed_at"] = int(response_data["updated_at"])

        if response_data.get("error"):
            err = response_data["error"]
            video_data["error"] = {
                "code": err.get("code", "unknown"),
                "message": err.get("message", "Video generation failed"),
            }

        if response_data.get("ratio"):
            ratio = response_data["ratio"]
            if isinstance(ratio, str) and ":" in ratio:
                video_data["size"] = ratio.replace(":", "x")
        if response_data.get("duration") is not None:
            video_data["seconds"] = str(response_data["duration"])
        elif request_data and request_data.get("duration") is not None:
            video_data["seconds"] = str(request_data["duration"])

        video_obj = VideoObject(**video_data)  # type: ignore[arg-type]

        if custom_llm_provider and video_obj.id:
            video_obj.id = encode_video_id_with_provider(video_obj.id, custom_llm_provider, model)

        # Usage drives cost tracking, so it is attached ONLY for a task that
        # actually succeeded, and only the first time we observe that success.
        #
        # BytePlus bills solely for successful generations and LiteLLM has no
        # refund path, so a create call (which has no result yet) and every
        # queued/running/failed poll must stay cost-free. Our own studio
        # re-reads a finished job several times (archive reconciliation,
        # output_url retries, history clicks); without the dedupe below each of
        # those re-reads would charge the user again — the spend LOG is keyed by
        # request_id and would collapse, but the incremental key/user spend
        # counters would not.
        usage_data: dict[str, Any] = {}
        if video_data["status"] == "completed" and _claim_video_charge(response_data.get("id") or video_obj.id):
            if getattr(video_obj, "seconds", None):
                try:
                    usage_data["duration_seconds"] = float(video_obj.seconds)
                except (ValueError, TypeError):
                    pass
            # The provider's own count is authoritative — it already reflects
            # resolution, aspect ratio, clip length and any video input, which
            # a per-second rate cannot express.
            provider_usage = response_data.get("usage")
            if isinstance(provider_usage, dict):
                for key in ("completion_tokens", "total_tokens", "prompt_tokens"):
                    value = provider_usage.get(key)
                    if isinstance(value, (int, float)):
                        usage_data[key] = int(value)
            resolution = self._resolution_bucket(response_data, request_data)
            if resolution:
                # BytePlus never sets this; without it LiteLLM cannot pick the
                # per-resolution rate and silently falls back to a flat one.
                usage_data["video_resolution"] = resolution
            if self._has_video_input(request_data):
                usage_data["has_video_input"] = True
        video_obj.usage = usage_data

        return video_obj

    @staticmethod
    def _resolution_bucket(response_data: dict[str, Any], request_data: dict | None) -> str | None:
        """Normalize the output resolution to 480p/720p/1080p/4k."""
        raw = response_data.get("resolution")
        if not raw and request_data:
            raw = request_data.get("resolution")
        text = str(raw or "").strip().lower()
        if not text:
            return None
        if text in ("4k", "2160p"):
            return "4k"
        for bucket in ("480p", "720p", "1080p"):
            if text == bucket or text == bucket[:-1]:
                return bucket
        return text or None

    @staticmethod
    def _has_video_input(request_data: dict | None) -> bool:
        """True when the request carried a reference video (cheaper per token)."""
        if not request_data:
            return False
        content = request_data.get("content")
        if not isinstance(content, list):
            return False
        return any(isinstance(item, dict) and item.get("type") == "video_url" for item in content)

    def transform_video_create_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        custom_llm_provider: str | None = None,
        request_data: dict | None = None,
    ) -> VideoObject:
        return self._build_video_object(raw_response.json(), model, custom_llm_provider, request_data)

    def transform_video_status_retrieve_request(
        self,
        video_id: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> tuple[str, dict]:
        original_video_id = extract_original_video_id(video_id)
        encoded = encode_url_path_segment(original_video_id, field_name="video_id")
        url = f"{api_base}/{VIDEO_TASKS_ENDPOINT}/{encoded}"
        return url, {}

    def transform_video_status_retrieve_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        custom_llm_provider: str | None = None,
    ) -> VideoObject:
        return self._build_video_object(raw_response.json(), None, custom_llm_provider)

    def transform_video_content_request(
        self,
        video_id: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
        variant: str | None = None,
    ) -> tuple[str, dict]:
        original_video_id = extract_original_video_id(video_id)
        encoded = encode_url_path_segment(original_video_id, field_name="video_id")
        url = f"{api_base}/{VIDEO_TASKS_ENDPOINT}/{encoded}"
        return url, {}

    @staticmethod
    def _encode_download_url(url: str) -> str:
        """
        Percent-encode non-ASCII characters in Ark's signed download URL.

        Ark returns URLs whose path/query can carry non-ASCII (e.g. a
        prompt-derived object name). Handing those to httpx raises
        `UnicodeEncodeError: 'ascii' codec can't encode characters ...`,
        which surfaced as an opaque 500 on GET /v1/videos/{id}/content —
        the only way to fetch the bytes, since the proxy's OpenAI-shaped
        VideoObject response drops `output_url`. Already-encoded triplets
        are preserved (`%` stays safe), so this is idempotent.
        """
        parts = urlsplit(url)
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc.encode("idna").decode("ascii")
                if any(ord(c) > 127 for c in parts.netloc)
                else parts.netloc,
                quote(parts.path, safe="/%:@!$&'()*+,;="),
                quote(parts.query, safe="/%:@!$&'()*+,;=?"),
                quote(parts.fragment, safe="/%:@!$&'()*+,;=?"),
            )
        )

    def _extract_video_url_from_response(self, response_data: dict[str, Any]) -> str:
        content = response_data.get("content") or {}
        video_url = content.get("video_url") if isinstance(content, dict) else None
        if not video_url:
            status = (response_data.get("status") or "UNKNOWN").lower()
            if status in ("queued", "running"):
                raise ValueError(f"Video is still processing (status: {status}). Please wait and try again.")
            if status == "failed":
                err = response_data.get("error") or {}
                raise ValueError(f"Video generation failed: {err.get('message', 'Unknown error')}")
            raise ValueError("Video URL not found in response. Video may not be ready yet.")
        return video_url

    def transform_video_content_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> bytes:
        video_url = self._extract_video_url_from_response(raw_response.json())
        httpx_client: HTTPHandler = _get_httpx_client()
        video_response = httpx_client.get(self._encode_download_url(video_url))
        video_response.raise_for_status()
        return video_response.content

    async def async_transform_video_content_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> bytes:
        video_url = self._extract_video_url_from_response(raw_response.json())
        async_httpx_client: AsyncHTTPHandler = get_async_httpx_client(
            llm_provider=litellm.LlmProviders.BYTEPLUS,
        )
        video_response = await async_httpx_client.get(self._encode_download_url(video_url))
        video_response.raise_for_status()
        return video_response.content

    def transform_video_delete_request(
        self,
        video_id: str,
        api_base: str,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> tuple[str, dict]:
        original_video_id = extract_original_video_id(video_id)
        encoded = encode_url_path_segment(original_video_id, field_name="video_id")
        url = f"{api_base}/{VIDEO_TASKS_ENDPOINT}/{encoded}"
        return url, {}

    def transform_video_delete_response(
        self,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> VideoObject:
        response_data = raw_response.json()
        return VideoObject(
            id=response_data.get("id", ""),
            object="video",
            status="cancelled",
            created_at=int(response_data.get("created_at") or 0),
        )  # type: ignore[arg-type]

    # ---- unsupported operations ----
    def transform_video_remix_request(self, video_id, prompt, api_base, litellm_params, headers, extra_body=None):
        raise NotImplementedError("Video remix is not supported for BytePlus")

    def transform_video_remix_response(self, raw_response, logging_obj, custom_llm_provider=None):
        raise NotImplementedError("Video remix is not supported for BytePlus")

    def transform_video_list_request(
        self,
        api_base,
        litellm_params,
        headers,
        after=None,
        limit=None,
        order=None,
        extra_query=None,
    ):
        raise NotImplementedError("Video listing is not supported for BytePlus")

    def transform_video_list_response(self, raw_response, logging_obj, custom_llm_provider=None):
        raise NotImplementedError("Video listing is not supported for BytePlus")

    def transform_video_create_character_request(self, name, video, api_base, litellm_params, headers):
        raise NotImplementedError("video create character is not supported for BytePlus")

    def transform_video_create_character_response(self, raw_response, logging_obj):
        raise NotImplementedError("video create character is not supported for BytePlus")

    def transform_video_get_character_request(self, character_id, api_base, litellm_params, headers):
        raise NotImplementedError("video get character is not supported for BytePlus")

    def transform_video_get_character_response(self, raw_response, logging_obj):
        raise NotImplementedError("video get character is not supported for BytePlus")

    def transform_video_edit_request(
        self,
        prompt,
        video_id,
        api_base,
        litellm_params,
        headers,
        extra_body=None,
        prefetched_source_data=None,
    ):
        raise NotImplementedError("video edit is not supported for BytePlus")

    def transform_video_edit_response(self, raw_response, logging_obj, custom_llm_provider=None, request_data=None):
        raise NotImplementedError("video edit is not supported for BytePlus")

    def transform_video_extension_request(
        self,
        prompt,
        video_id,
        seconds,
        api_base,
        litellm_params,
        headers,
        extra_body=None,
    ):
        raise NotImplementedError("video extension is not supported for BytePlus")

    def transform_video_extension_response(self, raw_response, logging_obj, custom_llm_provider=None):
        raise NotImplementedError("video extension is not supported for BytePlus")

    def get_error_class(self, error_message: str, status_code: int, headers: dict | httpx.Headers) -> BaseLLMException:
        raise BaseLLMException(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
