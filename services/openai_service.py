"""OpenAI-backed meal analysis with a strict structured response."""

import hashlib
import hmac
import json
import os

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from .ai_contract import (
    MAX_MEAL_MESSAGE_LENGTH,
    MAX_MINERVA_MESSAGE_LENGTH,
    MAX_WORKOUT_MESSAGE_LENGTH,
    MEAL_ANALYSIS_PROMPT,
    MEAL_RECOMMENDATION_PROMPT,
    MINERVA_PROMPT,
    WORKOUT_ANALYSIS_PROMPT,
    FoodItem,
    MealAnalysis,
    MealRecommendation,
    MinervaResponse,
    RecommendedMeal,
    WorkoutAnalysis,
    minerva_user_message,
)
from .ai_errors import (
    AIAuthenticationError,
    AIAuthorizationError,
    AIBillingError,
    AIRateLimitError,
    AIServiceError,
)
from .ai_validation import AICredentialValidation, BILLING_REQUIRED


KEY_VALIDATION_TIMEOUT_SECONDS = 5.0


class OpenAIAuthenticationError(AIAuthenticationError):
    pass


class OpenAIAuthorizationError(AIAuthorizationError):
    pass


class OpenAIBillingError(AIBillingError):
    pass


class OpenAIRateLimitError(AIRateLimitError):
    pass


class OpenAIServiceError(AIServiceError):
    pass


def _safety_identifier(email):
    secret = (
        os.environ.get("OPENAI_SAFETY_SALT")
        or os.environ.get("JWT_SECRET_KEY")
        or "dev-only-openai-safety-secret"
    )
    return hmac.new(secret.encode("utf-8"), email.encode("utf-8"), hashlib.sha256).hexdigest()


def _openai_error_code(error):
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if isinstance(code, str):
        return code
    nested_error = body.get("error")
    if isinstance(nested_error, dict) and isinstance(nested_error.get("code"), str):
        return nested_error["code"]
    return None


def _is_billing_error(error):
    return (
        getattr(error, "status_code", None) == 402
        or _openai_error_code(error) == "insufficient_quota"
    )


def _raise_mapped_openai_error(error):
    status_code = getattr(error, "status_code", None)
    if isinstance(error, AuthenticationError) or status_code == 401:
        raise OpenAIAuthenticationError("OpenAI API key is invalid or unauthorized") from error
    if _is_billing_error(error):
        raise OpenAIBillingError("OpenAI billing or credit is required") from error
    if isinstance(error, PermissionDeniedError) or status_code == 403:
        raise OpenAIAuthorizationError("OpenAI API access is denied") from error
    if isinstance(error, RateLimitError) or status_code == 429:
        raise OpenAIRateLimitError("OpenAI rate limit reached") from error
    raise OpenAIServiceError("OpenAI request failed") from error


def _model_name(model=None):
    return model or os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")


def validate_api_key(api_key, email=None, model=None):
    if not isinstance(api_key, str):
        raise ValueError("invalid_api_key")
    api_key = api_key.strip()
    if len(api_key) < 20 or len(api_key) > 512 or any(char.isspace() for char in api_key):
        raise ValueError("invalid_api_key")

    try:
        with OpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=KEY_VALIDATION_TIMEOUT_SECONDS,
        ) as client:
            client.models.list()
    except (
        AuthenticationError,
        PermissionDeniedError,
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        if _is_billing_error(error):
            return AICredentialValidation(api_key, BILLING_REQUIRED)
        _raise_mapped_openai_error(error)
    return AICredentialValidation(api_key)


def analyze_meal(message, email, api_key, model=None):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message_required")
    message = message.strip()
    if len(message) > MAX_MEAL_MESSAGE_LENGTH:
        raise ValueError("message_too_long")

    try:
        response = OpenAI(api_key=api_key).responses.parse(
            model=_model_name(model),
            input=[
                {"role": "system", "content": MEAL_ANALYSIS_PROMPT},
                {"role": "user", "content": message},
            ],
            text_format=MealAnalysis,
            reasoning={"effort": "low"},
            safety_identifier=_safety_identifier(email),
            store=False,
        )
    except (
        AuthenticationError,
        PermissionDeniedError,
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_openai_error(error)

    analysis = response.output_parsed
    if analysis is None:
        raise OpenAIServiceError("OpenAI returned no structured meal analysis")

    result = analysis.model_dump()
    # Totals are deterministic and should not depend on the model doing arithmetic correctly.
    result["total_calories"] = sum(item["calories"] for item in result["items"])
    result["total_protein_g"] = round(
        sum(item["protein_g"] for item in result["items"]), 1
    )
    return result


def analyze_workout(message, email, api_key, model=None):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message_required")
    message = message.strip()
    if len(message) > MAX_WORKOUT_MESSAGE_LENGTH:
        raise ValueError("message_too_long")

    try:
        response = OpenAI(api_key=api_key).responses.parse(
            model=_model_name(model),
            input=[
                {"role": "system", "content": WORKOUT_ANALYSIS_PROMPT},
                {"role": "user", "content": message},
            ],
            text_format=WorkoutAnalysis,
            reasoning={"effort": "low"},
            safety_identifier=_safety_identifier(email),
            store=False,
        )
    except (
        AuthenticationError,
        PermissionDeniedError,
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_openai_error(error)

    analysis = response.output_parsed
    if analysis is None:
        raise OpenAIServiceError("OpenAI returned no structured workout analysis")
    return analysis.model_dump()


def recommend_meals(context, email, api_key, model=None):
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
        response = OpenAI(api_key=api_key).responses.parse(
            model=_model_name(model),
            input=[
                {"role": "system", "content": MEAL_RECOMMENDATION_PROMPT},
                {"role": "user", "content": json.dumps(prompt_context)},
            ],
            text_format=MealRecommendation,
            reasoning={"effort": "low"},
            safety_identifier=_safety_identifier(email),
            store=False,
        )
    except (
        AuthenticationError,
        PermissionDeniedError,
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_openai_error(error)

    recommendation = response.output_parsed
    if recommendation is None:
        raise OpenAIServiceError("OpenAI returned no structured meal recommendation")

    result = recommendation.model_dump()
    if len(result["meals"]) != context.meals_remaining:
        raise OpenAIServiceError("OpenAI returned the wrong number of meals")

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


def respond_minerva(message, email, api_key, model=None, existing_cards=None):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message_required")
    message = message.strip()
    if len(message) > MAX_MINERVA_MESSAGE_LENGTH:
        raise ValueError("message_too_long")
    provider_message = minerva_user_message(message, existing_cards)

    try:
        response = OpenAI(api_key=api_key).responses.parse(
            model=_model_name(model),
            input=[
                {"role": "system", "content": MINERVA_PROMPT},
                {"role": "user", "content": provider_message},
            ],
            text_format=MinervaResponse,
            reasoning={"effort": "low"},
            safety_identifier=_safety_identifier(email),
            store=False,
        )
    except (
        AuthenticationError,
        PermissionDeniedError,
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
    ) as error:
        _raise_mapped_openai_error(error)

    result = response.output_parsed
    if result is None:
        raise OpenAIServiceError("OpenAI returned no Minerva response")
    return result.model_dump()
