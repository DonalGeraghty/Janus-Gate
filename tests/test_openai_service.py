import unittest
from unittest.mock import patch

import httpx
import openai

from services.openai_service import (
    KEY_VALIDATION_TIMEOUT_SECONDS,
    OpenAIAuthenticationError,
    OpenAIAuthorizationError,
    OpenAIBillingError,
    OpenAIRateLimitError,
    OpenAIServiceError,
    _raise_mapped_openai_error,
    validate_api_key,
)


API_KEY = "sk-project-user-key-1234567890-Ab12"
EMAIL = "user@example.com"


def _status_error(error_type, status_code, body=None):
    request = httpx.Request("GET", "https://api.openai.com/v1/models")
    response = httpx.Response(status_code, request=request)
    return error_type(
        f"OpenAI status {status_code}",
        response=response,
        body=body or {},
    )


class OpenAICredentialTests(unittest.TestCase):
    @patch("services.openai_service.OpenAI")
    def test_validate_key_lists_models_without_generating_output(
        self,
        openai_mock,
    ):
        validation = validate_api_key(f"  {API_KEY}  ", EMAIL)

        self.assertEqual(validation.api_key, API_KEY)
        self.assertIsNone(validation.warning)
        openai_mock.assert_called_once_with(
            api_key=API_KEY,
            max_retries=0,
            timeout=KEY_VALIDATION_TIMEOUT_SECONDS,
        )
        client = openai_mock.return_value.__enter__.return_value
        client.models.list.assert_called_once_with()
        client.responses.create.assert_not_called()
        self.assertNotIn(EMAIL, str(openai_mock.mock_calls))

    @patch("services.openai_service.OpenAI")
    def test_invalid_key_shapes_are_rejected_without_network(self, openai_mock):
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
        openai_mock.assert_not_called()

    def test_sdk_errors_are_mapped_to_provider_specific_errors(self):
        cases = [
            (
                _status_error(openai.AuthenticationError, 401),
                OpenAIAuthenticationError,
            ),
            (
                _status_error(openai.PermissionDeniedError, 403),
                OpenAIAuthorizationError,
            ),
            (
                _status_error(openai.RateLimitError, 429, {"code": "rate_limit"}),
                OpenAIRateLimitError,
            ),
            (
                _status_error(openai.APIStatusError, 500),
                OpenAIServiceError,
            ),
            (
                openai.APIConnectionError(
                    request=httpx.Request(
                        "GET",
                        "https://api.openai.com/v1/models",
                    )
                ),
                OpenAIServiceError,
            ),
        ]
        for sdk_error, expected_error in cases:
            with self.subTest(error=type(sdk_error).__name__):
                with patch("services.openai_service.OpenAI") as openai_mock:
                    client = openai_mock.return_value.__enter__.return_value
                    client.models.list.side_effect = sdk_error
                    with self.assertRaises(expected_error):
                        validate_api_key(API_KEY, EMAIL)

    @patch("services.openai_service.OpenAI")
    def test_insufficient_quota_still_returns_a_storable_key(self, openai_mock):
        client = openai_mock.return_value.__enter__.return_value
        client.models.list.side_effect = _status_error(
            openai.RateLimitError,
            429,
            {"code": "insufficient_quota"},
        )

        validation = validate_api_key(API_KEY, EMAIL)

        self.assertEqual(validation.api_key, API_KEY)
        self.assertEqual(validation.warning, "provider_billing_required")

    def test_insufficient_quota_is_mapped_for_generation_requests(self):
        with self.assertRaises(OpenAIBillingError):
            _raise_mapped_openai_error(
                _status_error(
                    openai.RateLimitError,
                    429,
                    {"code": "insufficient_quota"},
                )
            )
