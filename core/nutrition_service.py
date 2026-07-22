"""Validation for user-confirmed nutrition entries."""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.openai_service import FoodItem, MAX_MEAL_MESSAGE_LENGTH


class NutritionEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[FoodItem] = Field(min_length=1, max_length=30)
    eaten_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_message: str | None = Field(default=None, max_length=MAX_MEAL_MESSAGE_LENGTH)

    @field_validator("eaten_at")
    @classmethod
    def normalize_eaten_at(cls, value):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
