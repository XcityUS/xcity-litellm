from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Final, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

router: Final = APIRouter(prefix="/v1/drama", tags=["drama"])
JSON_OBJECT: Final = TypeAdapter(dict[str, object])
SourceLanguage = Literal[
    "en-US",
    "zh-CN",
    "ja-JP",
    "ko-KR",
    "es-ES",
    "fr-FR",
    "de-DE",
    "pt-BR",
    "it-IT",
    "ar-SA",
]
Presence = Literal["on_screen", "voice_over", "narrator", "mentioned"]
MAX_SCRIPT_CHARACTERS: Final = 120_000
DEFAULT_MODEL: Final = "deepseek-v4-pro-260425"


class BreakdownRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    script: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARACTERS)
    source_language: SourceLanguage = Field(default="zh-CN", alias="sourceLanguage")


class CharacterDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    description: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    presence: Presence = "on_screen"
    major: bool = False


class SceneDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    evidence: tuple[str, ...] = ()


class DialogueDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    speaker_character_id: str | None = Field(default=None, alias="speakerCharacterId")
    text: str = Field(min_length=1)
    emotion: str | None = None


class ShotDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    camera: str | None = None
    audio: str | None = None
    duration_seconds: int = Field(alias="durationSeconds", ge=2, le=30)
    scene_id: str | None = Field(default=None, alias="sceneId")
    character_ids: tuple[str, ...] = Field(default=(), alias="characterIds")
    dialogues: tuple[DialogueDraft, ...] = ()
    subtitle: str | None = None
    continuity_source_shot_id: str | None = Field(default=None, alias="continuitySourceShotId")


class ScriptAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal[1] = 1
    characters: tuple[CharacterDraft, ...]
    scenes: tuple[SceneDraft, ...]
    shots: tuple[ShotDraft, ...] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        character_ids: Final = {character.id for character in self.characters}
        scene_ids: Final = {scene.id for scene in self.scenes}
        shot_ids: Final = {shot.id for shot in self.shots}
        if len(character_ids) != len(self.characters):
            raise ValueError("Character IDs must be unique")
        if len(scene_ids) != len(self.scenes):
            raise ValueError("Scene IDs must be unique")
        if len(shot_ids) != len(self.shots):
            raise ValueError("Shot IDs must be unique")
        for index, shot in enumerate(self.shots):
            if shot.scene_id is not None and shot.scene_id not in scene_ids:
                raise ValueError(f"Shot {shot.id} references an unknown scene")
            if any(character_id not in character_ids for character_id in shot.character_ids):
                raise ValueError(f"Shot {shot.id} references an unknown character")
            if any(
                dialogue.speaker_character_id is not None and dialogue.speaker_character_id not in character_ids
                for dialogue in shot.dialogues
            ):
                raise ValueError(f"Shot {shot.id} dialogue references an unknown character")
            if shot.continuity_source_shot_id is not None:
                if shot.continuity_source_shot_id not in shot_ids:
                    raise ValueError(f"Shot {shot.id} references an unknown continuity source")
                expected_source = self.shots[index - 1].id if index > 0 else None
                if shot.continuity_source_shot_id != expected_source:
                    raise ValueError(f"Shot {shot.id} continuity source must be the immediately previous shot")
        return self


SYSTEM_PROMPT: Final = """Analyze a short-drama script and return one JSON object only.
Preserve story order, identity, dialogue meaning, source language, and scene continuity.
Return characters, scenes, and practical video shots. Do not force identities together just because names are similar.
Characters require: id, name, aliases, description with concrete appearance cues, evidence quotes, presence as on_screen, voice_over, narrator, or mentioned, and major.
Scenes require: id, name, description, and evidence quotes.
Shots require: id, description, prompt, durationSeconds, sceneId, characterIds, dialogues, subtitle, camera, audio, and optional continuitySourceShotId.
Each dialogue uses speakerCharacterId, text, and emotion. IDs referenced by shots must exist. Use the immediately previous shot ID for continuity.
Choose each duration by narrative rhythm, normally 4-8 seconds and never 30-second blocks. Produce no markdown or commentary.
Schema: {"version":1,"characters":[{"id":"character_1","name":"","aliases":[],"description":"","evidence":[],"presence":"on_screen","major":true}],"scenes":[{"id":"scene_1","name":"","description":"","evidence":[]}],"shots":[{"id":"shot_1","description":"","prompt":"","camera":"","audio":"","durationSeconds":4,"sceneId":"scene_1","characterIds":["character_1"],"dialogues":[{"speakerCharacterId":"character_1","text":"","emotion":""}],"subtitle":"","continuitySourceShotId":null}]}"""


def _model() -> str:
    return os.getenv("SCRIPT_BREAKDOWN_MODEL", DEFAULT_MODEL).split(",", maxsplit=1)[0].strip() or DEFAULT_MODEL


def _proxy_request(request: Request, payload: Mapping[str, object]) -> Request:
    encoded: Final = json.dumps(payload).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": encoded, "more_body": False}

    scope: Final = {**request.scope, "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions"}
    return Request(scope, receive)


def _content(result: object) -> str:
    candidate: Final = result.model_dump() if isinstance(result, BaseModel) else result
    try:
        payload: Final = JSON_OBJECT.validate_python(candidate)
        choices: Final = TypeAdapter(list[object]).validate_python(payload.get("choices"))
        if not choices:
            return ""
        choice: Final = JSON_OBJECT.validate_python(choices[0])
        message: Final = JSON_OBJECT.validate_python(choice.get("message"))
        content: Final = message.get("content")
        return content.strip() if isinstance(content, str) else ""
    except (ValidationError, IndexError):
        return ""


def parse_json_object(content: str) -> Mapping[str, object]:
    stripped: Final = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start: Final = stripped.find("{")
    end: Final = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The model response did not contain a JSON object")
    return JSON_OBJECT.validate_json(stripped[start : end + 1])


@router.post("/script/breakdown")
async def breakdown_script(
    body: BreakdownRequest,
    request: Request,
    response: Response,
    auth: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> Mapping[str, object]:
    from litellm.proxy.proxy_server import chat_completion

    payload: Final = {
        "model": _model(),
        "messages": (
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Source language: {body.source_language}\n\nScript:\n{body.script.strip()}",
            },
        ),
        "temperature": 0.2,
        "max_tokens": 8000,
        "response_format": {"type": "json_object"},
    }
    result: Final = await chat_completion(
        request=_proxy_request(request, payload),
        fastapi_response=response,
        user_api_key_dict=auth,
    )
    content: Final = _content(result)
    if not content:
        raise HTTPException(
            status_code=502, detail={"code": "EMPTY_RESPONSE", "message": "Script analysis returned no content"}
        )
    try:
        analysis: Final = ScriptAnalysis.model_validate(parse_json_object(content))
    except (ValueError, json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(
            status_code=502,
            detail={"code": "INVALID_RESPONSE", "message": "Script analysis returned invalid structured data"},
        ) from error
    return analysis.model_dump(mode="json", by_alias=True)
