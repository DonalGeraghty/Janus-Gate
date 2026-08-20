"""Validation and deterministic scheduling for Minerva flashcards."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_FRONT_LENGTH = 500
MAX_BACK_LENGTH = 4_000
MAX_TAGS = 8


def normalize_tags(tags):
    normalized = []
    for tag in tags or []:
        value = " ".join(str(tag).strip().lower().split())
        if value and value not in normalized:
            normalized.append(value[:40])
    return normalized[:MAX_TAGS]


class FlashcardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    front: str = Field(min_length=1, max_length=MAX_FRONT_LENGTH)
    back: str = Field(min_length=1, max_length=MAX_BACK_LENGTH)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    source_message: str | None = Field(default=None, max_length=2_000)
    client_request_id: UUID | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value):
        return normalize_tags(value)


class FlashcardUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    front: str = Field(min_length=1, max_length=MAX_FRONT_LENGTH)
    back: str = Field(min_length=1, max_length=MAX_BACK_LENGTH)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value):
        return normalize_tags(value)


class FlashcardReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rating: Literal["again", "hard", "good", "easy"]
    client_request_id: UUID | None = None


def schedule_review(card, rating, now=None):
    """Return the next scheduling state for a four-button review."""
    now = now or datetime.now(timezone.utc)
    previous_interval = max(0, int(card.get("interval_days") or 0))
    review_count = max(0, int(card.get("review_count") or 0))
    lapses = max(0, int(card.get("lapses") or 0))
    ease = min(3.5, max(1.3, float(card.get("ease_factor") or 2.5)))

    if rating == "again":
        interval_days = 0
        next_due = now + timedelta(minutes=10)
        ease = max(1.3, ease - 0.2)
        lapses += 1
    elif rating == "hard":
        interval_days = 1 if previous_interval == 0 else max(1, round(previous_interval * 1.2))
        next_due = now + timedelta(days=interval_days)
        ease = max(1.3, ease - 0.15)
    elif rating == "good":
        if review_count == 0:
            interval_days = 1
        elif previous_interval <= 1:
            interval_days = 3
        else:
            interval_days = max(2, round(previous_interval * ease))
        next_due = now + timedelta(days=interval_days)
    elif rating == "easy":
        interval_days = 4 if review_count == 0 else max(4, round(max(1, previous_interval) * (ease + 1)))
        next_due = now + timedelta(days=interval_days)
        ease = min(3.5, ease + 0.15)
    else:
        raise ValueError("invalid_rating")

    return {
        "last_reviewed_at": now,
        "due_at": next_due,
        "review_count": review_count + 1,
        "interval_days": interval_days,
        "ease_factor": round(ease, 2),
        "lapses": lapses,
    }
