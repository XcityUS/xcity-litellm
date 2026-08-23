"""
Billing rules for BytePlus video: charge once, on success, by token.

BytePlus bills only successful generations and LiteLLM has no refund path, so
the create call and every non-terminal poll must stay cost-free, and the
studio's repeated re-reads of a finished job must not bill twice.
"""

import os
import sys

import httpx
import pytest

# Production runs with LITELLM_LOCAL_MODEL_COST_MAP=True, which reads the
# packaged copy of the price map rather than fetching upstream's. Pin it here
# too, or these tests price against a map that has never heard of BytePlus
# video — which is how a whole release shipped without any token prices.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

workspace_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
sys.path.insert(0, workspace_path)

from types import SimpleNamespace

from litellm.cost_calculator import completion_cost
from litellm.llms.byteplus.videos import transformation as byteplus_videos
from litellm.llms.openai.cost_calculation import video_generation_cost
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager

MODEL = "dreamina-seedance-2-5-260628"


def _cfg():
    return ProviderConfigManager.get_provider_video_config(
        model=MODEL, provider=LlmProviders.BYTEPLUS
    )


def _build(cfg, payload, request_data=None):
    return cfg.transform_video_status_retrieve_response(
        raw_response=httpx.Response(200, json=payload),
        logging_obj=None,
        custom_llm_provider="byteplus",
    ) if request_data is None else cfg._build_video_object(
        payload, MODEL, "byteplus", request_data
    )


@pytest.fixture(autouse=True)
def _clear_charge_cache():
    byteplus_videos._CHARGED_VIDEO_IDS.clear()
    yield
    byteplus_videos._CHARGED_VIDEO_IDS.clear()


def _succeeded(video_id="cgt-test-1", tokens=108000, resolution="720p"):
    return {
        "id": video_id,
        "model": MODEL,
        "status": "succeeded",
        "content": {"video_url": "https://example.com/v.mp4"},
        "created_at": 1780577242,
        "duration": 5,
        "resolution": resolution,
        "usage": {"completion_tokens": tokens, "total_tokens": tokens},
    }


class TestChargeOnSuccessOnce:
    def test_create_response_carries_no_usage(self):
        """A submitted task has no result yet — it must not be billable."""
        cfg = _cfg()
        created = {"id": "cgt-test-1", "status": "queued", "created_at": 1780577242}
        obj = cfg._build_video_object(created, MODEL, "byteplus", {"duration": 5})
        assert obj.usage == {}

    def test_running_poll_carries_no_usage(self):
        cfg = _cfg()
        running = {"id": "cgt-test-1", "status": "running", "created_at": 1780577242}
        assert cfg._build_video_object(running, MODEL, "byteplus", None).usage == {}

    def test_failed_poll_carries_no_usage(self):
        cfg = _cfg()
        failed = {
            "id": "cgt-test-1",
            "status": "failed",
            "created_at": 1780577242,
            "duration": 5,
            "error": {"code": "X", "message": "nope"},
        }
        assert cfg._build_video_object(failed, MODEL, "byteplus", None).usage == {}

    def test_first_success_is_billable_and_repeats_are_not(self):
        cfg = _cfg()
        first = cfg._build_video_object(_succeeded(), MODEL, "byteplus", None)
        assert first.usage["completion_tokens"] == 108000
        assert first.usage["video_resolution"] == "720p"
        assert first.usage["duration_seconds"] == 5.0

        # The studio re-reads a finished job (archive retries, history clicks).
        for _ in range(3):
            again = cfg._build_video_object(_succeeded(), MODEL, "byteplus", None)
            assert again.usage == {}, "a re-read of the same video must not bill again"

    def test_distinct_videos_each_bill_once(self):
        cfg = _cfg()
        a = cfg._build_video_object(_succeeded("cgt-a"), MODEL, "byteplus", None)
        b = cfg._build_video_object(_succeeded("cgt-b"), MODEL, "byteplus", None)
        assert a.usage and b.usage

    def test_video_input_is_flagged_for_the_cheaper_rate(self):
        cfg = _cfg()
        request_data = {
            "content": [
                {"type": "text", "text": "x"},
                {"type": "video_url", "video_url": {"url": "https://e/v.mp4"}, "role": "reference_video"},
            ]
        }
        obj = cfg._build_video_object(_succeeded(), MODEL, "byteplus", request_data)
        assert obj.usage["has_video_input"] is True


class TestTokenPricing:
    """Prices come from the token count, not wall-clock seconds."""

    INFO = {
        "output_cost_per_video_token_720p": 12.84 / 1_000_000,
        "output_cost_per_video_token_720p_with_video": 7.68 / 1_000_000,
        "output_cost_per_second_720p": 0.277344,
    }

    def test_token_rate_wins_over_per_second(self):
        cost = video_generation_cost(
            model=MODEL,
            duration_seconds=5.0,
            model_info=self.INFO,
            video_resolution="720p",
            completion_tokens=108000,
        )
        assert cost == pytest.approx(1.38672, rel=1e-6)

    def test_video_input_uses_the_discounted_rate(self):
        cost = video_generation_cost(
            model=MODEL,
            duration_seconds=5.0,
            model_info=self.INFO,
            video_resolution="720p",
            completion_tokens=108000,
            has_video_input=True,
        )
        assert cost == pytest.approx(0.82944, rel=1e-6)

    def test_falls_back_to_per_second_without_a_token_count(self):
        cost = video_generation_cost(
            model=MODEL,
            duration_seconds=5.0,
            model_info=self.INFO,
            video_resolution="720p",
        )
        assert cost == pytest.approx(0.277344 * 5, rel=1e-6)

    def test_aspect_ratio_is_reflected_because_tokens_carry_it(self):
        """1:1 at 720p is fewer pixels, so fewer tokens, so a lower price."""
        square_tokens = int(5 * 720 * 720 * 24 / 1024)
        wide = video_generation_cost(
            model=MODEL, duration_seconds=5.0, model_info=self.INFO,
            video_resolution="720p", completion_tokens=108000,
        )
        square = video_generation_cost(
            model=MODEL, duration_seconds=5.0, model_info=self.INFO,
            video_resolution="720p", completion_tokens=square_tokens,
        )
        assert square < wide


class TestProxyPollCallTypesArePriced:
    """The route the studio actually polls must reach video pricing.

    `GET /videos/{video_id}` reports `avideo_status` (router.py sets the string
    directly; CallTypes has no member for it). Pricing keyed only on the
    retrieve call types let every real poll fall past the video branch and bill
    nothing — while the unit tests above stayed green, because they call the
    pricing helper directly and never exercise the dispatch.
    """

    @pytest.mark.parametrize(
        "call_type",
        ["avideo_status", "video_status", "avideo_retrieve", "video_retrieve"],
    )
    def test_poll_call_type_reaches_video_pricing(self, call_type):
        response = SimpleNamespace(
            usage={
                "completion_tokens": 108000,
                "video_resolution": "720p",
                "duration_seconds": 5.0,
            }
        )
        cost = completion_cost(
            completion_response=response,
            model=MODEL,
            custom_llm_provider="byteplus",
            call_type=call_type,
        )
        assert cost > 0
