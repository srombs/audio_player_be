"""The local magazine issue index, separate from articles and audio."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.newyorker.images import safe_image_url


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_date: date
    label: str
    url: str
    cover_image_url: str | None = None

    @field_validator("cover_image_url")
    @classmethod
    def valid_cover(cls, value: str | None) -> str | None:
        if value is not None and safe_image_url(value) != value:
            raise ValueError("Cover must use an approved public image URL.")
        return value


class IssueIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    source_url: Literal["https://www.newyorker.com/archive"] = "https://www.newyorker.com/archive"
    updated_at: datetime
    issues: list[Issue] = Field(default_factory=list)
    history_complete: bool = False
    # The issue whose Previous link must be read next. It is a public issue URL.
    history_cursor: str | None = None


class IssueList(BaseModel):
    """Magazine metadata for app browsing, without sync implementation details."""

    updated_at: datetime
    history_complete: bool
    issues: list[Issue]
