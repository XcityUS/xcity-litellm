"""
A completed BytePlus job must surface its playable URL to clients.

BytePlusVideoConfig sets `output_url` from Ark's `content.video_url`, but
VideoObject had no such field, so pydantic dropped it on serialization: the
proxy's retrieve response carried no URL and clients had to fall back to
GET /v1/videos/{id}/content (which proxies the whole file).
"""

import json
from unittest.mock import MagicMock

from litellm.llms.byteplus.videos.transformation import BytePlusVideoConfig
from litellm.types.videos.main import VideoObject


def _raw(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    return response


def test_video_object_serializes_output_url():
    obj = VideoObject(
        id="video_abc",
        object="video",
        status="completed",
        output_url="https://ark-content.example.com/v/clip.mp4?sig=abc",
    )
    dumped = json.loads(obj.model_dump_json())
    assert dumped["output_url"] == "https://ark-content.example.com/v/clip.mp4?sig=abc"


def test_completed_status_response_carries_output_url():
    config = BytePlusVideoConfig()
    video = config.transform_video_status_retrieve_response(
        raw_response=_raw(
            {
                "id": "cgt-123",
                "status": "succeeded",
                "created_at": 1785072032,
                "updated_at": 1785072065,
                "content": {"video_url": "https://ark-content.example.com/v/clip.mp4?sig=x"},
                "duration": 5,
            }
        ),
        logging_obj=MagicMock(),
        custom_llm_provider="byteplus",
    )
    assert video.status == "completed"
    assert video.output_url == "https://ark-content.example.com/v/clip.mp4?sig=x"
    assert json.loads(video.model_dump_json())["output_url"] is not None


def test_pending_job_has_no_output_url():
    config = BytePlusVideoConfig()
    video = config.transform_video_status_retrieve_response(
        raw_response=_raw({"id": "cgt-123", "status": "running", "created_at": 1785072032}),
        logging_obj=MagicMock(),
        custom_llm_provider="byteplus",
    )
    assert video.status == "in_progress"
    assert video.output_url is None
