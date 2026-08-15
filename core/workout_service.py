"""Validation for user-owned workout history."""

import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class WorkoutExerciseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    done: bool | None = None
    name: str | None = Field(default=None, max_length=200)
    result: str | None = Field(default=None, max_length=200)
    weight: str | None = Field(default=None, max_length=32)


class WorkoutHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workout_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    day: str = Field(min_length=1, max_length=32)
    finished_at: datetime
    duration_minutes: int = Field(ge=1, le=1_440)
    completed: int = Field(ge=0, le=1_000)
    total: int = Field(ge=1, le=1_000)
    entries: dict[str, WorkoutExerciseResult] = Field(default_factory=dict)
    note: str = Field(default="", max_length=4_000)
    source_message: str | None = Field(default=None, max_length=2_000)

    @field_validator("workout_id")
    @classmethod
    def validate_workout_id(cls, value):
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("Invalid workout ID")
        return value

    @field_validator("finished_at")
    @classmethod
    def normalize_finished_at(cls, value):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("entries")
    @classmethod
    def validate_entries(cls, value):
        if len(value) > 200 or any(
            not IDENTIFIER_PATTERN.fullmatch(entry_id) for entry_id in value
        ):
            raise ValueError("Invalid workout entries")
        return value

    @model_validator(mode="after")
    def validate_completion(self):
        if self.completed > self.total:
            raise ValueError("Completed cannot exceed total")
        return self
