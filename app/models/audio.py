"""Narration metadata and verified sources for direct audio playback."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RelatedAudio(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    name: str
    audio_id: str = Field(alias="audioId", min_length=1)
    duration_ms: int = Field(alias="durationInMs", gt=0, strict=True)
    embed_url: str = Field(alias="embedUrl", min_length=1)
    uri: str | None = None


class AudioSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_id: str
    duration_ms: int
    audio_url: str
    content_type: str
    verified_at: datetime
