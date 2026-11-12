"""Issue table-of-contents entries; audio URLs are resolved separately."""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.issue import Issue
from app.models.audio import AudioSource


class ArticleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    url: str
    section: str | None = None
    rubric: str | None = None
    byline: str | None = None
    description: str | None = None
    image_url: str | None = None
    # An explicit issue-card Listen badge is a hint; absence remains unknown.
    audio_available: bool | None = None
    duration_ms: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def consistent_narration(self) -> Self:
        if self.audio_available is False and self.duration_ms is not None:
            raise ValueError("An article without narration cannot have a duration.")
        return self


class IssueArticles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    issue: Issue
    fetched_at: datetime
    articles: list[ArticleSummary] = Field(default_factory=list)


class Article(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    url: str
    description: str | None = None
    authors: list[str] = Field(default_factory=list)


class ArticleAudio(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    article: Article
    audio: AudioSource | None = None
