from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException

from gateway.verification.store import Verification, VerificationRepository, VoiceSample
from gateway.verification.voice import accept_voice

VOICE_UPLOAD_LEASE_SECONDS: Final = 5 * 60
logger: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceUploadReservation:
    record: Verification
    token: str


@asynccontextmanager
async def reserve_voice_upload(
    store: VerificationRepository, invitation_token: str
) -> AsyncGenerator[VoiceUploadReservation]:
    upload_id: Final = secrets.token_urlsafe(24)
    async with store.transaction() as transaction:
        record: Final = await transaction.one("token_hash", invitation_token)
        accept_voice(record)
        reserved: Final = await transaction.reserve_voice(record.id, upload_id, VOICE_UPLOAD_LEASE_SECONDS)
        if not reserved:
            raise HTTPException(503, "VOICE_UNAVAILABLE")
    try:
        yield VoiceUploadReservation(record, upload_id)
    finally:
        try:
            await store.release_voice(record.id, upload_id)
        except Exception:
            logger.warning("Voice upload reservation cleanup failed for invitation %s", record.id)


async def save_voice_upload(
    store: VerificationRepository, reservation: VoiceUploadReservation, sample: VoiceSample
) -> None:
    async with store.transaction() as transaction:
        record: Final = await transaction.one("id", reservation.record.id)
        accept_voice(record)
        await transaction.voice(record.id, sample, reservation.token)
