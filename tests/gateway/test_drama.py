from typing import Final

import pytest
from pydantic import ValidationError

from gateway.routes.drama import BreakdownRequest, ScriptAnalysis, parse_json_object


def test_breakdown_request_rejects_empty_script() -> None:
    with pytest.raises(ValidationError):
        BreakdownRequest.model_validate({"script": "", "sourceLanguage": "zh-CN"})


@pytest.mark.parametrize(
    "language", ["en-US", "zh-CN", "ja-JP", "ko-KR", "es-ES", "fr-FR", "de-DE", "pt-BR", "it-IT", "ar-SA"]
)
def test_breakdown_request_accepts_studio_languages(language: str) -> None:
    request: Final = BreakdownRequest.model_validate({"script": "Scene one", "sourceLanguage": language})
    assert request.source_language == language


def test_analysis_requires_linkable_structured_shots() -> None:
    payload: Final = {
        "version": 1,
        "characters": [
            {
                "id": "character_1",
                "name": "李雪",
                "aliases": [],
                "description": "二十多岁，黑色短发，深蓝外套",
                "evidence": ["李雪推门进入办公室"],
                "presence": "on_screen",
                "major": True,
            }
        ],
        "scenes": [
            {
                "id": "scene_1",
                "name": "办公室",
                "description": "冷色现代办公室",
                "evidence": ["办公室，夜，内"],
            }
        ],
        "shots": [
            {
                "id": "shot_1",
                "description": "中景，李雪推门进入办公室",
                "prompt": "冷色电影光，中景，李雪推门进入",
                "durationSeconds": 5,
                "sceneId": "scene_1",
                "characterIds": ["character_1"],
                "dialogues": [],
            }
        ],
    }
    analysis: Final = ScriptAnalysis.model_validate(payload)
    result: Final = analysis.model_dump(mode="json", by_alias=True)
    assert result["shots"][0]["durationSeconds"] == 5
    assert result["shots"][0]["characterIds"] == ["character_1"]


def test_analysis_rejects_unknown_character_reference() -> None:
    with pytest.raises(ValidationError, match="unknown character"):
        ScriptAnalysis.model_validate(
            {
                "version": 1,
                "characters": [],
                "scenes": [],
                "shots": [
                    {
                        "id": "shot_1",
                        "description": "A visitor enters",
                        "prompt": "Medium shot of a visitor entering",
                        "durationSeconds": 4,
                        "characterIds": ["character_missing"],
                    }
                ],
            }
        )


def test_analysis_rejects_duplicate_shot_ids() -> None:
    shot: Final = {
        "id": "shot_1",
        "description": "A visitor enters",
        "prompt": "Medium shot of a visitor entering",
        "durationSeconds": 4,
    }
    with pytest.raises(ValidationError, match="Shot IDs must be unique"):
        ScriptAnalysis.model_validate({"version": 1, "characters": [], "scenes": [], "shots": [shot, shot]})


def test_json_object_accepts_fenced_model_output() -> None:
    result: Final = parse_json_object('```json\n{"version": 1}\n```')
    assert result == {"version": 1}
