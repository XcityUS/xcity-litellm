"""
Support for Tencent Cloud TokenHub `/v1/chat/completions` endpoint.

Calls done in OpenAI/openai.py as Tencent Cloud TokenHub is OpenAI-compatible.

Docs: https://cloud.tencent.com/document/product/1823/130079
"""

from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import AllMessageValues

from ...openai.chat.gpt_transformation import OpenAIGPTConfig


class TencentCloudChatConfig(OpenAIGPTConfig):
    def _get_openai_compatible_provider_info(
        self, api_base: str | None, api_key: str | None
    ) -> tuple[str | None, str | None]:
        api_base = api_base or get_secret_str("TENCENT_CLOUD_API_BASE") or "https://tokenhub.tencentmaas.com/v1"
        dynamic_api_key = api_key or get_secret_str("TENCENT_CLOUD_API_KEY")
        return api_base, dynamic_api_key

    def transform_request(
        self,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        model = model.removeprefix("tencent_cloud/")
        return super().transform_request(
            model=model,
            messages=messages,
            optional_params=optional_params,
            litellm_params=litellm_params,
            headers=headers,
        )

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict:
        if api_key is None:
            raise ValueError(
                "Missing Tencent Cloud API Key - A call is being made to tencent_cloud but no key is set either in the environment variables (TENCENT_CLOUD_API_KEY) or via params"
            )
        headers["Authorization"] = f"Bearer {api_key}"
        headers["Content-Type"] = "application/json"
        return headers
