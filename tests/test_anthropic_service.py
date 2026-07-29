import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx

from core.nutrition_service import MealRecommendationInput
from services.ai_contract import (
    MAX_MEAL_MESSAGE_LENGTH,
    MEAL_ANALYSIS_PROMPT,
    MEAL_RECOMMENDATION_PROMPT,
    MealAnalysis,
    MealRecommendation,
)
from services.anthropic_service import (
    GENERATION_MAX_RETRIES,
    GENERATION_TIMEOUT_SECONDS,
    KEY_VALIDATION_TIMEOUT_SECONDS,
    MEAL_ANALYSIS_MAX_TOKENS,
    MEAL_RECOMMENDATION_MAX_TOKENS,
    AnthropicAuthenticationError,
    AnthropicAuthorizationError,
    AnthropicBillingError,
    AnthropicRateLimitError,
    AnthropicServiceError,
    analyze_meal,
    recommend_meals,
    validate_api_key,
    _raise_mapped_anthropic_error,
)


API_KEY = "sk-ant-user-key-1234567890-Ab12"
EMAIL = "user@example.com"
MODEL = "claude-haiku-4-5-20251001"

SAMPLE_ANALYSIS = {
    "items": [
        {
            "food": "Eggs",
            "portion": "2 large",
            "calories": 140,
            "protein_g": 12,
        },
        {
            "food": "Toast",
            "portion": "2 slices",
            "calories": 200,
            "protein_g": 7.5,
        },
    ],
    "total_calories": 999,
    "total_protein_g": 999,
    "confidence": "medium",
    "assumptions": [],
    "needs_clarification": False,
    "clarification_question": "",
}

SAMPLE_RECOMMENDATION = {
    "summary": "Two protein-focused meals.",
    "meals": [
        {
            "name": "Chicken bowl",
            "items": [
                {
                    "food": "Chicken breast",
                    "portion": "150 g",
                    "calories": 250,
                    "protein_g": 45,
                },
                {
                    "food": "Vegetables",
                    "portion": "1 cup",
                    "calories": 100,
                    "protein_g": 5,
                },
            ],
            "rationale": "High protein with vegetables.",
        },
        {
            "name": "Yogurt and fruit",
            "items": [
                {
                    "food": "Greek yogurt",
                    "portion": "200 g",
                    "calories": 150,
                    "protein_g": 20,
                },
                {
                    "food": "Berries",
                    "portion": "100 g",
                    "calories": 50,
                    "protein_g": 1,
                },
            ],
            "rationale": "A light protein-rich meal.",
        },
    ],
    "assumptions": [],
}


def _response_with(parsed, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
    )


def _context():
    return MealRecommendationInput(
        current_calories=1200,
        current_protein_g=80,
        target_calories=2000,
        target_protein_g=140,
        meals_remaining=2,
        preferences="No shellfish",
    )


def _status_error(error_type, status_code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return error_type(
        f"Anthropic status {status_code}",
        response=response,
        body={},
    )


class AnthropicCredentialTests(unittest.TestCase):
    @patch("services.anthropic_service.Anthropic")
    def test_validate_key_lists_models_without_user_data_or_generation(
        self,
        anthropic_mock,
    ):
        validation = validate_api_key(f"  {API_KEY}  ", EMAIL)

        self.assertEqual(validation.api_key, API_KEY)
        self.assertIsNone(validation.warning)
        anthropic_mock.assert_called_once_with(
            api_key=API_KEY,
            max_retries=0,
            timeout=KEY_VALIDATION_TIMEOUT_SECONDS,
        )
        client = anthropic_mock.return_value.__enter__.return_value
        client.models.list.assert_called_once_with(limit=1)
        client.messages.count_tokens.assert_not_called()
        client.messages.create.assert_not_called()
        self.assertNotIn(EMAIL, str(anthropic_mock.mock_calls))

    @patch("services.anthropic_service.Anthropic")
    def test_validate_key_accepts_an_explicit_model(self, anthropic_mock):
        validation = validate_api_key(API_KEY, model=MODEL)

        self.assertEqual(validation.api_key, API_KEY)
        client = anthropic_mock.return_value.__enter__.return_value
        client.models.list.assert_called_once_with(limit=1)

    @patch("services.anthropic_service.Anthropic")
    def test_invalid_key_shapes_are_rejected_without_network(self, anthropic_mock):
        invalid_keys = [
            None,
            "",
            "short",
            "a" * 19,
            "a" * 513,
            "a" * 20 + " " + "b",
        ]
        for api_key in invalid_keys:
            with self.subTest(api_key=repr(api_key)):
                with self.assertRaisesRegex(ValueError, "invalid_api_key"):
                    validate_api_key(api_key, EMAIL)
        anthropic_mock.assert_not_called()

    def test_sdk_errors_are_mapped_to_provider_specific_errors(self):
        cases = [
            (
                _status_error(anthropic.AuthenticationError, 401),
                AnthropicAuthenticationError,
            ),
            (
                _status_error(anthropic.PermissionDeniedError, 403),
                AnthropicAuthorizationError,
            ),
            (
                _status_error(anthropic.RateLimitError, 429),
                AnthropicRateLimitError,
            ),
            (
                _status_error(anthropic.APIStatusError, 500),
                AnthropicServiceError,
            ),
            (
                anthropic.APIConnectionError(
                    request=httpx.Request(
                        "POST",
                        "https://api.anthropic.com/v1/messages",
                    )
                ),
                AnthropicServiceError,
            ),
        ]
        for sdk_error, expected_error in cases:
            with self.subTest(error=type(sdk_error).__name__):
                with patch(
                    "services.anthropic_service.Anthropic"
                ) as anthropic_mock:
                    client = anthropic_mock.return_value.__enter__.return_value
                    client.models.list.side_effect = sdk_error
                    with self.assertRaises(expected_error):
                        validate_api_key(API_KEY, EMAIL)

    @patch("services.anthropic_service.Anthropic")
    def test_billing_error_still_returns_a_storable_key(self, anthropic_mock):
        client = anthropic_mock.return_value.__enter__.return_value
        client.models.list.side_effect = _status_error(
            anthropic.APIStatusError,
            402,
        )

        validation = validate_api_key(API_KEY, EMAIL)

        self.assertEqual(validation.api_key, API_KEY)
        self.assertEqual(validation.warning, "provider_billing_required")

    def test_billing_error_is_mapped_for_generation_requests(self):
        with self.assertRaises(AnthropicBillingError):
            _raise_mapped_anthropic_error(
                _status_error(anthropic.APIStatusError, 402)
            )


class AnthropicMealAnalysisTests(unittest.TestCase):
    @patch("services.anthropic_service.Anthropic")
    def test_analysis_uses_structured_output_and_recalculates_totals(
        self,
        anthropic_mock,
    ):
        parsed = MealAnalysis.model_validate(SAMPLE_ANALYSIS)
        client = anthropic_mock.return_value.__enter__.return_value
        client.messages.parse.return_value = _response_with(parsed)

        result = analyze_meal("  Two eggs and toast  ", EMAIL, API_KEY, MODEL)

        self.assertEqual(result["total_calories"], 340)
        self.assertEqual(result["total_protein_g"], 19.5)
        anthropic_mock.assert_called_once_with(
            api_key=API_KEY,
            max_retries=GENERATION_MAX_RETRIES,
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
        request = client.messages.parse.call_args.kwargs
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(request["max_tokens"], MEAL_ANALYSIS_MAX_TOKENS)
        self.assertEqual(request["system"], MEAL_ANALYSIS_PROMPT)
        self.assertEqual(
            request["messages"],
            [{"role": "user", "content": "Two eggs and toast"}],
        )
        self.assertIs(request["output_format"], MealAnalysis)
        self.assertNotIn("temperature", request)
        self.assertNotIn("top_p", request)
        self.assertNotIn("top_k", request)
        self.assertNotIn(EMAIL, str(request))

    @patch("services.anthropic_service.Anthropic")
    def test_analysis_validates_message_before_creating_client(
        self,
        anthropic_mock,
    ):
        for message, expected_error in (
            ("", "message_required"),
            ("   ", "message_required"),
            (None, "message_required"),
            ("a" * (MAX_MEAL_MESSAGE_LENGTH + 1), "message_too_long"),
        ):
            with self.subTest(expected_error=expected_error):
                with self.assertRaisesRegex(ValueError, expected_error):
                    analyze_meal(message, EMAIL, API_KEY, MODEL)
        anthropic_mock.assert_not_called()

    @patch("services.anthropic_service.Anthropic")
    def test_analysis_rejects_refusal_truncation_and_missing_output(
        self,
        anthropic_mock,
    ):
        client = anthropic_mock.return_value.__enter__.return_value
        cases = [
            (_response_with(None, "refusal"), "refused"),
            (_response_with(None, "max_tokens"), "output limit"),
            (_response_with(None), "no structured meal analysis"),
        ]
        for response, expected_message in cases:
            with self.subTest(stop_reason=response.stop_reason):
                client.messages.parse.return_value = response
                with self.assertRaisesRegex(
                    AnthropicServiceError,
                    expected_message,
                ):
                    analyze_meal("Two eggs", EMAIL, API_KEY, MODEL)

    def test_analysis_maps_sdk_and_parse_errors(self):
        errors = (
            _status_error(anthropic.RateLimitError, 429),
            json.JSONDecodeError("invalid response", "{", 0),
            TypeError("unexpected parsed response"),
        )
        for parse_error in errors:
            with self.subTest(error=type(parse_error).__name__):
                with patch(
                    "services.anthropic_service.Anthropic"
                ) as anthropic_mock:
                    client = anthropic_mock.return_value.__enter__.return_value
                    client.messages.parse.side_effect = parse_error
                    expected_error = (
                        AnthropicRateLimitError
                        if isinstance(parse_error, anthropic.RateLimitError)
                        else AnthropicServiceError
                    )
                    with self.assertRaises(expected_error):
                        analyze_meal("Two eggs", EMAIL, API_KEY, MODEL)


class AnthropicMealRecommendationTests(unittest.TestCase):
    @patch("services.anthropic_service.Anthropic")
    def test_recommendation_uses_context_and_recalculates_all_totals(
        self,
        anthropic_mock,
    ):
        parsed = MealRecommendation.model_validate(SAMPLE_RECOMMENDATION)
        client = anthropic_mock.return_value.__enter__.return_value
        client.messages.parse.return_value = _response_with(parsed)

        result = recommend_meals(_context(), EMAIL, API_KEY, MODEL)

        self.assertEqual(result["meals"][0]["total_calories"], 350)
        self.assertEqual(result["meals"][0]["total_protein_g"], 50.0)
        self.assertEqual(result["plan_total_calories"], 550)
        self.assertEqual(result["plan_total_protein_g"], 71.0)
        self.assertEqual(result["projected_daily_calories"], 1750)
        self.assertEqual(result["projected_daily_protein_g"], 151.0)
        self.assertEqual(result["calorie_budget_remaining"], 800)
        self.assertEqual(result["protein_remaining_g"], 60.0)

        anthropic_mock.assert_called_once_with(
            api_key=API_KEY,
            max_retries=GENERATION_MAX_RETRIES,
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
        request = client.messages.parse.call_args.kwargs
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(
            request["max_tokens"],
            MEAL_RECOMMENDATION_MAX_TOKENS,
        )
        self.assertEqual(request["system"], MEAL_RECOMMENDATION_PROMPT)
        self.assertIs(request["output_format"], MealRecommendation)
        self.assertNotIn("temperature", request)
        self.assertNotIn("top_p", request)
        self.assertNotIn("top_k", request)
        prompt_context = json.loads(request["messages"][0]["content"])
        self.assertEqual(prompt_context["calories_eaten_today"], 1200)
        self.assertEqual(prompt_context["remaining_calorie_budget"], 800)
        self.assertEqual(prompt_context["remaining_protein_target_g"], 60.0)
        self.assertEqual(prompt_context["meals_remaining"], 2)
        self.assertEqual(
            prompt_context["dietary_preferences_or_restrictions"],
            "No shellfish",
        )
        self.assertNotIn(EMAIL, str(request))

    @patch("services.anthropic_service.Anthropic")
    def test_recommendation_requires_the_requested_number_of_meals(
        self,
        anthropic_mock,
    ):
        one_meal = {
            **SAMPLE_RECOMMENDATION,
            "meals": SAMPLE_RECOMMENDATION["meals"][:1],
        }
        client = anthropic_mock.return_value.__enter__.return_value
        client.messages.parse.return_value = _response_with(
            MealRecommendation.model_validate(one_meal)
        )

        with self.assertRaisesRegex(
            AnthropicServiceError,
            "wrong number of meals",
        ):
            recommend_meals(_context(), EMAIL, API_KEY, MODEL)

    @patch("services.anthropic_service.Anthropic")
    def test_recommendation_rejects_missing_parsed_output(
        self,
        anthropic_mock,
    ):
        client = anthropic_mock.return_value.__enter__.return_value
        client.messages.parse.return_value = _response_with(None)

        with self.assertRaisesRegex(
            AnthropicServiceError,
            "no structured meal recommendation",
        ):
            recommend_meals(_context(), EMAIL, API_KEY, MODEL)


if __name__ == "__main__":
    unittest.main()
