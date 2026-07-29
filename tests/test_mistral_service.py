import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from core.nutrition_service import MealRecommendationInput
from services import mistral_service
from services.mistral_service import (
    KEY_VALIDATION_TIMEOUT_MS,
    MistralAuthenticationError,
    MistralAuthorizationError,
    MistralBillingError,
    MistralRateLimitError,
    MistralServiceError,
    analyze_meal,
    recommend_meals,
    validate_api_key,
)
from services.openai_service import (
    MAX_MEAL_MESSAGE_LENGTH,
    MEAL_ANALYSIS_PROMPT,
    MEAL_RECOMMENDATION_PROMPT,
    MealAnalysis,
    MealRecommendation,
)


API_KEY = "mistral-user-key-1234567890-Ab12"
EMAIL = "user@example.com"
MODEL = "mistral-small-2603"

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


def _response_with(parsed):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
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


def _sdk_error(status_code):
    request = httpx.Request("GET", "https://api.mistral.ai/v1/models")
    response = httpx.Response(status_code, request=request)
    return mistral_service.errors.MistralError(
        f"Mistral status {status_code}",
        response,
    )


class MistralCredentialTests(unittest.TestCase):
    @patch("services.mistral_service.Mistral")
    def test_validate_key_trims_and_lists_models_without_sending_email(
        self, mistral_mock
    ):
        validation = validate_api_key(f"  {API_KEY}  ", EMAIL)

        self.assertEqual(validation.api_key, API_KEY)
        self.assertIsNone(validation.warning)
        mistral_mock.assert_called_once_with(
            api_key=API_KEY,
            timeout_ms=KEY_VALIDATION_TIMEOUT_MS,
        )
        client = mistral_mock.return_value.__enter__.return_value
        client.models.list.assert_called_once_with()
        self.assertNotIn(EMAIL, str(mistral_mock.mock_calls))

    @patch("services.mistral_service.Mistral")
    def test_validate_key_accepts_omitted_email(self, mistral_mock):
        self.assertEqual(validate_api_key(API_KEY).api_key, API_KEY)
        self.assertNotIn("email", str(mistral_mock.mock_calls).lower())

    @patch("services.mistral_service.Mistral")
    def test_invalid_key_shapes_are_rejected_without_network(self, mistral_mock):
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
        mistral_mock.assert_not_called()

    def test_sdk_status_codes_are_mapped(self):
        cases = [
            (401, MistralAuthenticationError),
            (403, MistralAuthorizationError),
            (429, MistralRateLimitError),
            (400, MistralServiceError),
            (500, MistralServiceError),
        ]
        for status_code, expected_error in cases:
            with self.subTest(status_code=status_code):
                with patch("services.mistral_service.Mistral") as mistral_mock:
                    client = mistral_mock.return_value.__enter__.return_value
                    client.models.list.side_effect = _sdk_error(status_code)
                    with self.assertRaises(expected_error):
                        validate_api_key(API_KEY, EMAIL)

    @patch("services.mistral_service.Mistral")
    def test_billing_error_still_returns_a_storable_key(self, mistral_mock):
        client = mistral_mock.return_value.__enter__.return_value
        client.models.list.side_effect = _sdk_error(402)

        validation = validate_api_key(API_KEY, EMAIL)

        self.assertEqual(validation.api_key, API_KEY)
        self.assertEqual(validation.warning, "provider_billing_required")

    def test_billing_error_is_mapped_for_generation_requests(self):
        from services.mistral_service import _raise_mapped_mistral_error

        with self.assertRaises(MistralBillingError):
            _raise_mapped_mistral_error(_sdk_error(402))

    def test_network_errors_are_mapped_to_service_error(self):
        for network_error in (
            httpx.ConnectError("connection failed"),
            httpx.ReadTimeout("request timed out"),
        ):
            with self.subTest(error=type(network_error).__name__):
                with patch("services.mistral_service.Mistral") as mistral_mock:
                    client = mistral_mock.return_value.__enter__.return_value
                    client.models.list.side_effect = network_error
                    with self.assertRaises(MistralServiceError):
                        validate_api_key(API_KEY, EMAIL)


class MistralMealAnalysisTests(unittest.TestCase):
    @patch("services.mistral_service.Mistral")
    def test_analysis_uses_structured_output_and_recalculates_totals(
        self, mistral_mock
    ):
        parsed = MealAnalysis.model_validate(SAMPLE_ANALYSIS)
        client = mistral_mock.return_value.__enter__.return_value
        client.chat.parse.return_value = _response_with(parsed)

        result = analyze_meal("  Two eggs and toast  ", EMAIL, API_KEY, MODEL)

        self.assertEqual(result["total_calories"], 340)
        self.assertEqual(result["total_protein_g"], 19.5)
        mistral_mock.assert_called_once_with(api_key=API_KEY)
        request = client.chat.parse.call_args.kwargs
        self.assertEqual(request["model"], MODEL)
        self.assertIs(request["response_format"], MealAnalysis)
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(
            request["messages"],
            [
                {"role": "system", "content": MEAL_ANALYSIS_PROMPT},
                {"role": "user", "content": "Two eggs and toast"},
            ],
        )
        self.assertNotIn(EMAIL, str(request))

    @patch("services.mistral_service.Mistral")
    def test_analysis_validates_message_before_creating_client(self, mistral_mock):
        for message, expected_error in (
            ("", "message_required"),
            ("   ", "message_required"),
            (None, "message_required"),
            ("a" * (MAX_MEAL_MESSAGE_LENGTH + 1), "message_too_long"),
        ):
            with self.subTest(expected_error=expected_error):
                with self.assertRaisesRegex(ValueError, expected_error):
                    analyze_meal(message, EMAIL, API_KEY, MODEL)
        mistral_mock.assert_not_called()

    @patch("services.mistral_service.Mistral")
    def test_analysis_rejects_missing_or_malformed_parsed_output(
        self, mistral_mock
    ):
        client = mistral_mock.return_value.__enter__.return_value
        for parsed in (None, {"items": "not-a-list"}):
            with self.subTest(parsed=parsed):
                client.chat.parse.return_value = _response_with(parsed)
                with self.assertRaises(MistralServiceError):
                    analyze_meal("Two eggs", EMAIL, API_KEY, MODEL)

    def test_chat_sdk_and_network_errors_are_mapped(self):
        with patch("services.mistral_service.Mistral") as mistral_mock:
            client = mistral_mock.return_value.__enter__.return_value
            client.chat.parse.side_effect = _sdk_error(429)
            with self.assertRaises(MistralRateLimitError):
                analyze_meal("Two eggs", EMAIL, API_KEY, MODEL)

        with patch("services.mistral_service.Mistral") as mistral_mock:
            client = mistral_mock.return_value.__enter__.return_value
            client.chat.parse.side_effect = httpx.ConnectError("offline")
            with self.assertRaises(MistralServiceError):
                analyze_meal("Two eggs", EMAIL, API_KEY, MODEL)

    def test_chat_parse_decode_and_type_errors_are_mapped(self):
        parse_errors = (
            json.JSONDecodeError("invalid response", "{", 0),
            TypeError("unexpected parsed response"),
        )
        for parse_error in parse_errors:
            with self.subTest(error=type(parse_error).__name__):
                with patch("services.mistral_service.Mistral") as mistral_mock:
                    client = mistral_mock.return_value.__enter__.return_value
                    client.chat.parse.side_effect = parse_error
                    with self.assertRaises(MistralServiceError):
                        analyze_meal("Two eggs", EMAIL, API_KEY, MODEL)


class MistralMealRecommendationTests(unittest.TestCase):
    @patch("services.mistral_service.Mistral")
    def test_recommendation_uses_context_and_recalculates_all_totals(
        self, mistral_mock
    ):
        parsed = MealRecommendation.model_validate(SAMPLE_RECOMMENDATION)
        client = mistral_mock.return_value.__enter__.return_value
        client.chat.parse.return_value = _response_with(parsed)
        context = _context()

        result = recommend_meals(context, EMAIL, API_KEY, MODEL)

        self.assertEqual(result["meals"][0]["total_calories"], 350)
        self.assertEqual(result["meals"][0]["total_protein_g"], 50.0)
        self.assertEqual(result["plan_total_calories"], 550)
        self.assertEqual(result["plan_total_protein_g"], 71.0)
        self.assertEqual(result["projected_daily_calories"], 1750)
        self.assertEqual(result["projected_daily_protein_g"], 151.0)
        self.assertEqual(result["calorie_budget_remaining"], 800)
        self.assertEqual(result["protein_remaining_g"], 60.0)

        request = client.chat.parse.call_args.kwargs
        self.assertEqual(request["model"], MODEL)
        self.assertIs(request["response_format"], MealRecommendation)
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(
            request["messages"][0],
            {"role": "system", "content": MEAL_RECOMMENDATION_PROMPT},
        )
        prompt_context = json.loads(request["messages"][1]["content"])
        self.assertEqual(prompt_context["calories_eaten_today"], 1200)
        self.assertEqual(prompt_context["remaining_calorie_budget"], 800)
        self.assertEqual(prompt_context["remaining_protein_target_g"], 60.0)
        self.assertEqual(prompt_context["meals_remaining"], 2)
        self.assertEqual(
            prompt_context["dietary_preferences_or_restrictions"],
            "No shellfish",
        )
        self.assertNotIn(EMAIL, str(request))

    @patch("services.mistral_service.Mistral")
    def test_recommendation_requires_the_requested_number_of_meals(
        self, mistral_mock
    ):
        one_meal = {
            **SAMPLE_RECOMMENDATION,
            "meals": SAMPLE_RECOMMENDATION["meals"][:1],
        }
        parsed = MealRecommendation.model_validate(one_meal)
        client = mistral_mock.return_value.__enter__.return_value
        client.chat.parse.return_value = _response_with(parsed)

        with self.assertRaisesRegex(
            MistralServiceError,
            "wrong number of meals",
        ):
            recommend_meals(_context(), EMAIL, API_KEY, MODEL)

    @patch("services.mistral_service.Mistral")
    def test_recommendation_rejects_missing_parsed_output(self, mistral_mock):
        client = mistral_mock.return_value.__enter__.return_value
        client.chat.parse.return_value = _response_with(None)

        with self.assertRaisesRegex(
            MistralServiceError,
            "no structured meal recommendation",
        ):
            recommend_meals(_context(), EMAIL, API_KEY, MODEL)

    @patch("services.mistral_service.Mistral")
    def test_model_is_required_before_creating_client(self, mistral_mock):
        for model in (None, "", "   "):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, "model_required"):
                    recommend_meals(_context(), EMAIL, API_KEY, model)
        mistral_mock.assert_not_called()

    def test_chat_parse_decode_and_type_errors_are_mapped(self):
        parse_errors = (
            json.JSONDecodeError("invalid response", "{", 0),
            TypeError("unexpected parsed response"),
        )
        for parse_error in parse_errors:
            with self.subTest(error=type(parse_error).__name__):
                with patch("services.mistral_service.Mistral") as mistral_mock:
                    client = mistral_mock.return_value.__enter__.return_value
                    client.chat.parse.side_effect = parse_error
                    with self.assertRaises(MistralServiceError):
                        recommend_meals(_context(), EMAIL, API_KEY, MODEL)


if __name__ == "__main__":
    unittest.main()
