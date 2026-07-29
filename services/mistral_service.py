"""Mistral-backed meal analysis with strict structured responses."""

import json

import httpx
from mistralai.client import Mistral, errors
from pydantic import ValidationError

from .ai_contract import (
    MAX_MEAL_MESSAGE_LENGTH,
    MEAL_ANALYSIS_PROMPT,
    MEAL_RECOMMENDATION_PROMPT,
    MealAnalysis,
    MealRecommendation,
)


class MistralAuthenticationError(RuntimeError):
    pass


class MistralRateLimitError(RuntimeError):
    pass


class MistralServiceError(RuntimeError):
    pass


def _raise_mapped_mistral_error(error):
    status_code = getattr(error, "status_code", None)
    if status_code in (401, 403):
        raise MistralAuthenticationError(
            "Mistral API key is invalid or unauthorized"
        ) from error
    if status_code == 429:
        raise MistralRateLimitError("Mistral rate limit reached") from error
    raise MistralServiceError("Mistral request failed") from error


def _normalize_api_key(api_key):
    if not isinstance(api_key, str):
        raise ValueError("invalid_api_key")
    api_key = api_key.strip()
    if (
        len(api_key) < 20
        or len(api_key) > 512
        or any(char.isspace() for char in api_key)
    ):
        raise ValueError("invalid_api_key")
    return api_key


def _normalize_model(model):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model_required")
    return model.strip()


def _parsed_response(response, response_type, empty_message):
    try:
        parsed = response.choices[0].message.parsed
    except (AttributeError, IndexError, TypeError) as error:
        raise MistralServiceError(empty_message) from error
    if parsed is None:
        raise MistralServiceError(empty_message)
    return response_type.model_validate(parsed)


def validate_api_key(api_key, email=None):
    """Validate a user-owned key without sending user data or spending tokens."""

    api_key = _normalize_api_key(api_key)

    try:
        with Mistral(api_key=api_key) as client:
            client.models.list()
    except (
        errors.MistralError,
        httpx.RequestError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_mistral_error(error)
    return api_key


def analyze_meal(message, email, api_key, model):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message_required")
    message = message.strip()
    if len(message) > MAX_MEAL_MESSAGE_LENGTH:
        raise ValueError("message_too_long")
    model = _normalize_model(model)

    try:
        with Mistral(api_key=api_key) as client:
            response = client.chat.parse(
                model=model,
                messages=[
                    {"role": "system", "content": MEAL_ANALYSIS_PROMPT},
                    {"role": "user", "content": message},
                ],
                response_format=MealAnalysis,
                temperature=0,
            )
        analysis = _parsed_response(
            response,
            MealAnalysis,
            "Mistral returned no structured meal analysis",
        )
    except (
        errors.MistralError,
        httpx.RequestError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_mistral_error(error)

    result = analysis.model_dump()
    # Totals are deterministic and should not depend on model arithmetic.
    result["total_calories"] = sum(item["calories"] for item in result["items"])
    result["total_protein_g"] = round(
        sum(item["protein_g"] for item in result["items"]), 1
    )
    return result


def recommend_meals(context, email, api_key, model):
    model = _normalize_model(model)
    prompt_context = {
        "calories_eaten_today": context.current_calories,
        "protein_eaten_today_g": context.current_protein_g,
        "daily_calorie_target": context.target_calories,
        "daily_protein_target_g": context.target_protein_g,
        "remaining_calorie_budget": max(
            0, context.target_calories - context.current_calories
        ),
        "remaining_protein_target_g": round(
            max(0, context.target_protein_g - context.current_protein_g), 1
        ),
        "meals_remaining": context.meals_remaining,
        "dietary_preferences_or_restrictions": context.preferences,
    }

    try:
        with Mistral(api_key=api_key) as client:
            response = client.chat.parse(
                model=model,
                messages=[
                    {"role": "system", "content": MEAL_RECOMMENDATION_PROMPT},
                    {"role": "user", "content": json.dumps(prompt_context)},
                ],
                response_format=MealRecommendation,
                temperature=0,
            )
        recommendation = _parsed_response(
            response,
            MealRecommendation,
            "Mistral returned no structured meal recommendation",
        )
    except (
        errors.MistralError,
        httpx.RequestError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_mistral_error(error)

    result = recommendation.model_dump()
    if len(result["meals"]) != context.meals_remaining:
        raise MistralServiceError("Mistral returned the wrong number of meals")

    for meal in result["meals"]:
        meal["total_calories"] = sum(item["calories"] for item in meal["items"])
        meal["total_protein_g"] = round(
            sum(item["protein_g"] for item in meal["items"]), 1
        )

    plan_calories = sum(meal["total_calories"] for meal in result["meals"])
    plan_protein = round(
        sum(meal["total_protein_g"] for meal in result["meals"]), 1
    )
    result.update({
        "calorie_budget_remaining": max(
            0, context.target_calories - context.current_calories
        ),
        "protein_remaining_g": round(
            max(0, context.target_protein_g - context.current_protein_g), 1
        ),
        "plan_total_calories": plan_calories,
        "plan_total_protein_g": plan_protein,
        "projected_daily_calories": context.current_calories + plan_calories,
        "projected_daily_protein_g": round(
            context.current_protein_g + plan_protein, 1
        ),
    })
    return result
