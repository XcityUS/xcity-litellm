"""
Chat completions against generation-only models (video/image) must fail
fast with an actionable BadRequestError instead of reaching the provider
and surfacing an opaque 500 (observed with BytePlus Seedance models).
"""

import pytest

import litellm
from litellm.main import _raise_if_generation_only_model


@pytest.fixture(autouse=True)
def _fake_cost_entries(monkeypatch):
    monkeypatch.setitem(
        litellm.model_cost,
        "byteplus/fake-seedance-video",
        {"litellm_provider": "byteplus", "mode": "video_generation"},
    )
    monkeypatch.setitem(
        litellm.model_cost,
        "byteplus/fake-seedream-image",
        {"litellm_provider": "byteplus", "mode": "image_generation"},
    )


def test_video_model_rejected_via_cost_map():
    with pytest.raises(litellm.BadRequestError, match=r"/v1/videos"):
        _raise_if_generation_only_model(
            model="fake-seedance-video",
            custom_llm_provider="byteplus",
            model_info=None,
        )


def test_image_model_rejected_via_cost_map():
    with pytest.raises(litellm.BadRequestError, match=r"/v1/images/generations"):
        _raise_if_generation_only_model(
            model="fake-seedream-image",
            custom_llm_provider="byteplus",
            model_info=None,
        )


def test_video_model_rejected_via_deployment_model_info():
    """Proxy path: DB model_info carries the mode before any cost-map entry exists."""
    with pytest.raises(litellm.BadRequestError, match=r"video generation"):
        _raise_if_generation_only_model(
            model="some-unpriced-video-model",
            custom_llm_provider="byteplus",
            model_info={"mode": "video_generation"},
        )


def test_chat_model_passes():
    _raise_if_generation_only_model(
        model="deepseek-v4-flash-260425",
        custom_llm_provider="byteplus",
        model_info={"mode": "chat"},
    )


def test_unknown_model_passes():
    _raise_if_generation_only_model(
        model="totally-unknown-model",
        custom_llm_provider=None,
        model_info=None,
    )


def test_completion_entrypoint_rejects_video_model():
    with pytest.raises(litellm.BadRequestError, match=r"/v1/videos"):
        litellm.completion(
            model="byteplus/fake-seedance-video",
            messages=[{"role": "user", "content": "hi"}],
            api_key="sk-test",
        )
