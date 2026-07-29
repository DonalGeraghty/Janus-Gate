"""Anthropic-backed meal analysis with strict structured responses."""

import json
import os

from anthropic import (
    Anthropic,
    AnthropicError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from .ai_contract import (
    MAX_MEAL_MESSAGE_LENGTH,
    MEAL_ANALYSIS_PROMPT,
    MEAL_RECOMMENDATION_PROMPT,
    MealAnalysis,
    MealRecommendation,
)
from .ai_errors import AIAuthenticationError, AIRateLimitError, AIServiceError


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
KEY_VALIDATION_TIMEOUT_SECONDS = 5.0
GENERATION_TIMEOUT_SECONDS = 120.0
GENERATION_MAX_RETRIES = 1
# Claude 5 counts adaptive-thinking tokens against these hard output ceilings.
MEAL_ANALYSIS_MAX_TOKENS = 4_096
MEAL_RECOMMENDATION_MAX_TOKENS = 8_192


class AnthropicAuthenticationError(AIAuthenticationError):
    pass


class AnthropicRateLimitError(AIRateLimitError):
    pass


class AnthropicServiceError(AIServiceError):
    pass


def _raise_mapped_anthropic_error(error):
    status_code = getattr(error, "status_code", None)
    if isinstance(error, (AuthenticationError, PermissionDeniedError)) or (
        status_code in (401, 403)
    ):
        raise AnthropicAuthenticationError(
            "Anthropic API key is invalid or unauthorized"
        ) from error
    if isinstance(error, RateLimitError) or status_code == 429:
        raise AnthropicRateLimitError("Anthropic rate limit reached") from error
    raise AnthropicServiceError("Anthropic request failed") from error


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


def _model_name(model=None):
    candidate = model if model is not None else os.environ.get(
        "ANTHROPIC_MODEL",
        DEFAULT_ANTHROPIC_MODEL,
    )
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("model_required")
    return candidate.strip()


def _parsed_response(response, response_type, empty_message):
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        raise AnthropicServiceError("Anthropic refused the request")
    if stop_reason == "max_tokens":
        raise AnthropicServiceError("Anthropic response exceeded the output limit")

    try:
        parsed = response.parsed_output
    except (AttributeError, TypeError) as error:
        raise AnthropicServiceError(empty_message) from error
    if parsed is None:
        raise AnthropicServiceError(empty_message)
    return response_type.model_validate(parsed)


def validate_api_key(api_key, email=None, model=None):
    """Validate a user-owned key without sending user data or generating output."""

    api_key = _normalize_api_key(api_key)
    model = _model_name(model)

    try:
        with Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=KEY_VALIDATION_TIMEOUT_SECONDS,
        ) as client:
            client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": "Hello"}],
            )
    except (
        AnthropicError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_anthropic_error(error)
    return api_key


def analyze_meal(message, email, api_key, model=None):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message_required")
    message = message.strip()
    if len(message) > MAX_MEAL_MESSAGE_LENGTH:
        raise ValueError("message_too_long")
    model = _model_name(model)

    try:
        with Anthropic(
            api_key=api_key,
            max_retries=GENERATION_MAX_RETRIES,
            timeout=GENERATION_TIMEOUT_SECONDS,
        ) as client:
            response = client.messages.parse(
                model=model,
                max_tokens=MEAL_ANALYSIS_MAX_TOKENS,
                system=MEAL_ANALYSIS_PROMPT,
                messages=[{"role": "user", "content": message}],
                output_format=MealAnalysis,
            )
        analysis = _parsed_response(
            response,
            MealAnalysis,
            "Anthropic returned no structured meal analysis",
        )
    except (
        AnthropicError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_anthropic_error(error)

    result = analysis.model_dump()
    # Totals are deterministic and should not depend on model arithmetic.
    result["total_calories"] = sum(item["calories"] for item in result["items"])
    result["total_protein_g"] = round(
        sum(item["protein_g"] for item in result["items"]),
        1,
    )
    return result


def recommend_meals(context, email, api_key, model=None):
    model = _model_name(model)
    prompt_context = {
        "calories_eaten_today": context.current_calories,
        "protein_eaten_today_g": context.current_protein_g,
        "daily_calorie_target": context.target_calories,
        "daily_protein_target_g": context.target_protein_g,
        "remaining_calorie_budget": max(
            0,
            context.target_calories - context.current_calories,
        ),
        "remaining_protein_target_g": round(
            max(0, context.target_protein_g - context.current_protein_g),
            1,
        ),
        "meals_remaining": context.meals_remaining,
        "dietary_preferences_or_restrictions": context.preferences,
    }

    try:
        with Anthropic(
            api_key=api_key,
            max_retries=GENERATION_MAX_RETRIES,
            timeout=GENERATION_TIMEOUT_SECONDS,
        ) as client:
            response = client.messages.parse(
                model=model,
                max_tokens=MEAL_RECOMMENDATION_MAX_TOKENS,
                system=MEAL_RECOMMENDATION_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(prompt_context),
                    }
                ],
                output_format=MealRecommendation,
            )
        recommendation = _parsed_response(
            response,
            MealRecommendation,
            "Anthropic returned no structured meal recommendation",
        )
    except (
        AnthropicError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_anthropic_error(error)

    result = recommendation.model_dump()
    if len(result["meals"]) != context.meals_remaining:
        raise AnthropicServiceError(
            "Anthropic returned the wrong number of meals"
        )

    for meal in result["meals"]:
        meal["total_calories"] = sum(item["calories"] for item in meal["items"])
        meal["total_protein_g"] = round(
            sum(item["protein_g"] for item in meal["items"]),
            1,
        )

    plan_calories = sum(meal["total_calories"] for meal in result["meals"])
    plan_protein = round(
        sum(meal["total_protein_g"] for meal in result["meals"]),
        1,
    )
    result.update({
        "calorie_budget_remaining": max(
            0,
            context.target_calories - context.current_calories,
        ),
        "protein_remaining_g": round(
            max(0, context.target_protein_g - context.current_protein_g),
            1,
        ),
        "plan_total_calories": plan_calories,
        "plan_total_protein_g": plan_protein,
        "projected_daily_calories": context.current_calories + plan_calories,
        "projected_daily_protein_g": round(
            context.current_protein_g + plan_protein,
            1,
        ),
    })
    return result
