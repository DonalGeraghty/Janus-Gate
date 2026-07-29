"""Validation for user-confirmed nutrition entries."""

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.ai_contract import FoodItem, MAX_MEAL_MESSAGE_LENGTH


class NutritionEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[FoodItem] = Field(min_length=1, max_length=30)
    eaten_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_message: str | None = Field(default=None, max_length=MAX_MEAL_MESSAGE_LENGTH)
    client_request_id: UUID | None = None

    @field_validator("eaten_at")
    @classmethod
    def normalize_eaten_at(cls, value):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class MealRecommendationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    current_calories: int = Field(ge=0, le=100_000)
    current_protein_g: float = Field(ge=0, le=10_000)
    target_calories: int = Field(ge=500, le=10_000)
    target_protein_g: float = Field(ge=10, le=1_000)
    meals_remaining: int = Field(ge=1, le=3)
    preferences: str = Field(default="", max_length=1_000)
