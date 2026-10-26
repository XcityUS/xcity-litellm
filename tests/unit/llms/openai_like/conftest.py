"""Shared fixtures for the JSON-configured (OpenAI-compatible) provider tests."""

import os
import sys
from collections.abc import Callable, Iterator

import pytest

workspace_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
sys.path.insert(0, workspace_path)

import litellm


@pytest.fixture
def register_chat_model() -> Iterator[Callable[[str, bool], None]]:
    """Register a chat model in the cost map, then restore the original map.

    Takes the full cost-map key (e.g. ``"publicai/some-model"``) and whether the
    model is known to support function calling.
    """
    original = litellm.model_cost
    litellm.model_cost = dict(original)

    def register(model_key: str, supports_function_calling: bool) -> None:
        litellm.model_cost[model_key] = {
            "litellm_provider": model_key.split("/")[0],
            "mode": "chat",
            "supports_function_calling": supports_function_calling,
        }

    yield register

    litellm.model_cost = original
