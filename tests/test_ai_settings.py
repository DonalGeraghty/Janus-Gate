import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from app import app
from services import ai_service
from services.ai_errors import (
    AIAuthorizationError,
    AIBillingError,
    AIRateLimitError,
    AIServiceError,
)
from services.ai_validation import AICredentialValidation, BILLING_REQUIRED
from services.firebase import db_state
from services.firebase.users import _save_ai_selection_in_transaction
from services.anthropic_service import AnthropicRateLimitError
from services.mistral_service import MistralRateLimitError


MISTRAL_KEY = "mistral-user-key-1234567890-Ab12"
MISTRAL_SELECTION = {
    "provider": "mistral",
    "model": "mistral-small-2603",
}
ANTHROPIC_KEY = "sk-ant-api03-user-key-1234567890-Cd34"
ANTHROPIC_SELECTION = {
    "provider": "anthropic",
    "model": "claude-sonnet-5",
}
SAMPLE_ANALYSIS = {
    "items": [
        {
            "food": "Eggs",
            "portion": "2 large eggs",
            "calories": 140,
            "protein_g": 12.0,
        }
    ],
    "total_calories": 140,
    "total_protein_g": 12.0,
    "confidence": "medium",
    "assumptions": [],
    "needs_clarification": False,
    "clarification_question": "",
}


class AISettingsApiTests(unittest.TestCase):
    def setUp(self):
        db_state.users_collection_ref = None
        db_state.auth_users_memory.clear()
        db_state.nutrition_entries_memory.clear()
        self.client = app.test_client()

    def register(self):
        response = self.client.post(
            "/api/auth/register",
            json={"email": "user@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 201)
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    def test_ai_routes_require_authentication(self):
        self.assertEqual(self.client.get("/api/user/ai-settings").status_code, 401)
        self.assertEqual(self.client.get("/api/user/minerva-settings").status_code, 401)
        self.assertEqual(
            self.client.put(
                "/api/user/ai-settings",
                json=MISTRAL_SELECTION,
            ).status_code,
            401,
        )
        for provider, api_key in (
            ("mistral", MISTRAL_KEY),
            ("anthropic", ANTHROPIC_KEY),
        ):
            for method in ("get", "put", "delete"):
                response = getattr(self.client, method)(
                    f"/api/user/ai-credentials/{provider}",
                    json={"api_key": api_key} if method == "put" else None,
                )
                self.assertEqual(response.status_code, 401)

    def test_minerva_card_context_defaults_off_and_persists(self):
        headers = self.register()

        initial = self.client.get("/api/user/minerva-settings", headers=headers)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(
            initial.get_json()["settings"],
            {"include_card_context": False},
        )

        updated = self.client.put(
            "/api/user/minerva-settings",
            headers=headers,
            json={"include_card_context": True},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            updated.get_json()["settings"],
            {"include_card_context": True},
        )
        self.assertTrue(
            db_state.auth_users_memory["user@example.com"]["minerva_include_card_context"]
        )

        invalid = self.client.put(
            "/api/user/minerva-settings",
            headers=headers,
            json={"include_card_context": "yes"},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_settings_return_catalog_selection_and_safe_statuses(self):
        headers = self.register()

        def credential_status(_email, provider, _account_id):
            if provider == "openai":
                return True, None, {
                    "configured": True,
                    "last_four": "Ab12",
                    "verified_at": "2026-07-28T18:00:00+00:00",
                }
            return True, None, None

        with patch(
            "app.get_ai_credential_status", side_effect=credential_status
        ):
            response = self.client.get("/api/user/ai-settings", headers=headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["selection"],
            {"provider": "openai", "model": "gpt-5.6-sol"},
        )
        self.assertEqual(
            [provider["id"] for provider in payload["providers"]],
            ["openai", "mistral", "anthropic"],
        )
        self.assertEqual(
            [model["id"] for model in payload["providers"][0]["models"]],
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(
            [model["id"] for model in payload["providers"][1]["models"]],
            [
                "mistral-small-2603",
                "mistral-large-2512",
                "mistral-medium-3-5",
            ],
        )
        self.assertEqual(payload["providers"][2]["name"], "Claude (Anthropic)")
        self.assertEqual(
            [model["id"] for model in payload["providers"][2]["models"]],
            [
                "claude-sonnet-5",
                "claude-opus-5",
                "claude-haiku-4-5-20251001",
            ],
        )
        self.assertTrue(payload["providers"][0]["credential"]["configured"])
        self.assertEqual(
            payload["providers"][1]["credential"],
            {"configured": False},
        )
        self.assertEqual(
            payload["providers"][2]["credential"],
            {"configured": False},
        )
        self.assertNotIn("ciphertext", response.get_data(as_text=True))
        self.assertNotIn(MISTRAL_KEY, response.get_data(as_text=True))

    def test_settings_selection_is_validated_and_persisted(self):
        headers = self.register()
        response = self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json=MISTRAL_SELECTION,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["selection"], MISTRAL_SELECTION)
        self.assertEqual(
            db_state.auth_users_memory["user@example.com"]["ai_provider"],
            "mistral",
        )

        invalid_provider = self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json={"provider": "other", "model": "mistral-small-2603"},
        )
        self.assertEqual(invalid_provider.status_code, 400)
        self.assertEqual(invalid_provider.get_json()["error"], "invalid_provider")

        invalid_model = self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json={"provider": "mistral", "model": "gpt-5.6-sol"},
        )
        self.assertEqual(invalid_model.status_code, 400)
        self.assertEqual(invalid_model.get_json()["error"], "invalid_model")

    def test_provider_key_is_verified_encrypted_v2_and_never_returned(self):
        headers = self.register()
        status = {
            "configured": True,
            "last_four": "Ab12",
            "verified_at": "2026-07-28T18:00:00+00:00",
        }
        with (
            patch("app.validate_api_key", return_value=MISTRAL_KEY) as validate_mock,
            patch("app.encrypt_api_key", return_value="kms-ciphertext") as encrypt_mock,
            patch(
                "app.save_ai_credential",
                return_value=(True, None, status),
            ) as save_mock,
        ):
            response = self.client.put(
                "/api/user/ai-credentials/mistral",
                headers=headers,
                json={"api_key": MISTRAL_KEY},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "mistral")
        self.assertEqual(response.get_json()["credential"], status)
        self.assertNotIn(MISTRAL_KEY, response.get_data(as_text=True))
        self.assertNotIn("kms-ciphertext", response.get_data(as_text=True))
        validate_mock.assert_called_once_with(
            "mistral", MISTRAL_KEY, "user@example.com"
        )
        encrypt_mock.assert_called_once_with(
            MISTRAL_KEY,
            "user@example.com",
            provider="mistral",
            aad_version=2,
        )
        save_mock.assert_called_once_with(
            "user@example.com",
            "mistral",
            "kms-ciphertext",
            "Ab12",
            aad_version=2,
            account_id=db_state.auth_users_memory["user@example.com"]["account_id"],
        )

    def test_anthropic_key_uses_provider_scoped_v2_encryption(self):
        headers = self.register()
        status = {
            "configured": True,
            "last_four": "Cd34",
            "verified_at": "2026-07-29T12:00:00+00:00",
        }
        with (
            patch(
                "app.validate_api_key", return_value=ANTHROPIC_KEY
            ) as validate_mock,
            patch(
                "app.encrypt_api_key",
                return_value="anthropic-ciphertext",
            ) as encrypt_mock,
            patch(
                "app.save_ai_credential",
                return_value=(True, None, status),
            ) as save_mock,
        ):
            response = self.client.put(
                "/api/user/ai-credentials/anthropic",
                headers=headers,
                json={"api_key": ANTHROPIC_KEY},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "anthropic")
        self.assertNotIn(ANTHROPIC_KEY, response.get_data(as_text=True))
        self.assertNotIn("anthropic-ciphertext", response.get_data(as_text=True))
        validate_mock.assert_called_once_with(
            "anthropic", ANTHROPIC_KEY, "user@example.com"
        )
        encrypt_mock.assert_called_once_with(
            ANTHROPIC_KEY,
            "user@example.com",
            provider="anthropic",
            aad_version=2,
        )
        save_mock.assert_called_once_with(
            "user@example.com",
            "anthropic",
            "anthropic-ciphertext",
            "Cd34",
            aad_version=2,
            account_id=db_state.auth_users_memory["user@example.com"]["account_id"],
        )

    def test_billing_limited_key_is_saved_with_a_non_fatal_warning(self):
        headers = self.register()
        status = {
            "configured": True,
            "last_four": "Cd34",
            "verified_at": "2026-07-29T12:00:00+00:00",
        }
        with (
            patch(
                "app.validate_api_key",
                return_value=AICredentialValidation(
                    ANTHROPIC_KEY,
                    BILLING_REQUIRED,
                ),
            ),
            patch(
                "app.encrypt_api_key",
                return_value="anthropic-ciphertext",
            ),
            patch(
                "app.save_ai_credential",
                return_value=(True, None, status),
            ) as save_mock,
        ):
            response = self.client.put(
                "/api/user/ai-credentials/anthropic",
                headers=headers,
                json={"api_key": ANTHROPIC_KEY},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["credential"], status)
        self.assertEqual(
            payload["warning"]["code"],
            "provider_billing_required",
        )
        self.assertNotIn(ANTHROPIC_KEY, response.get_data(as_text=True))
        self.assertNotIn("anthropic-ciphertext", response.get_data(as_text=True))
        save_mock.assert_called_once()

    def test_anthropic_validation_error_uses_provider_display_name(self):
        headers = self.register()
        with patch("app.validate_api_key", side_effect=ValueError("invalid_api_key")):
            response = self.client.put(
                "/api/user/ai-credentials/anthropic",
                headers=headers,
                json={"api_key": "bad"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["message"],
            "A valid Claude (Anthropic) API key is required",
        )

    def test_transient_or_permission_failures_do_not_replace_a_saved_key(self):
        headers = self.register()
        cases = [
            (AIAuthorizationError(), 403, "provider_access_denied"),
            (AIRateLimitError(), 429, "provider_rate_limited"),
            (AIServiceError(), 502, "provider_unavailable"),
        ]
        for validation_error, status_code, error_code in cases:
            with self.subTest(error=error_code):
                with (
                    patch(
                        "app.validate_api_key",
                        side_effect=validation_error,
                    ),
                    patch("app.encrypt_api_key") as encrypt_mock,
                    patch("app.save_ai_credential") as save_mock,
                ):
                    response = self.client.put(
                        "/api/user/ai-credentials/anthropic",
                        headers=headers,
                        json={"api_key": ANTHROPIC_KEY},
                    )

                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.get_json()["error"], error_code)
                encrypt_mock.assert_not_called()
                save_mock.assert_not_called()

    def test_invalid_provider_path_is_rejected_before_provider_call(self):
        headers = self.register()
        with patch("app.validate_api_key") as validate_mock:
            response = self.client.put(
                "/api/user/ai-credentials/not-a-provider",
                headers=headers,
                json={"api_key": MISTRAL_KEY},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_provider")
        validate_mock.assert_not_called()

    def test_non_object_settings_and_credential_payloads_are_rejected(self):
        headers = self.register()
        with patch("app.validate_api_key") as validate_mock:
            credential_response = self.client.put(
                "/api/user/ai-credentials/mistral",
                headers=headers,
                json=[MISTRAL_KEY],
            )
        settings_response = self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json=["mistral", "mistral-small-2603"],
        )

        self.assertEqual(credential_response.status_code, 400)
        self.assertEqual(
            credential_response.get_json()["error"],
            "invalid_request",
        )
        validate_mock.assert_not_called()
        self.assertEqual(settings_response.status_code, 400)
        self.assertEqual(
            settings_response.get_json()["error"],
            "invalid_request",
        )

    def test_provider_status_and_delete_are_scoped_to_requested_provider(self):
        headers = self.register()
        status = {"configured": True, "last_four": "Ab12"}
        with patch(
            "app.get_ai_credential_status",
            return_value=(True, None, status),
        ) as status_mock:
            response = self.client.get(
                "/api/user/ai-credentials/mistral", headers=headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "mistral")
        self.assertEqual(response.get_json()["credential"], status)
        account_id = db_state.auth_users_memory["user@example.com"]["account_id"]
        status_mock.assert_called_once_with(
            "user@example.com",
            "mistral",
            account_id,
        )

        with patch(
            "app.delete_ai_credential", return_value=(True, None)
        ) as delete_mock:
            response = self.client.delete(
                "/api/user/ai-credentials/mistral", headers=headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["provider"], "mistral")
        delete_mock.assert_called_once_with(
            "user@example.com",
            "mistral",
            account_id,
        )

    def test_selected_provider_without_key_never_falls_back(self):
        headers = self.register()
        self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json=MISTRAL_SELECTION,
        )
        with patch(
            "app.get_ai_credential", return_value=(True, None, None)
        ) as credential_mock:
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "provider_key_required")
        self.assertEqual(response.get_json()["provider"], "mistral")
        credential_mock.assert_called_once_with(
            "user@example.com",
            "mistral",
            db_state.auth_users_memory["user@example.com"]["account_id"],
        )

    def test_analysis_routes_through_selected_provider_model_and_v2_aad(self):
        headers = self.register()
        self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json=MISTRAL_SELECTION,
        )
        with (
            patch(
                "app.get_ai_credential",
                return_value=(
                    True,
                    None,
                    {"ciphertext": "encrypted", "aad_version": 2},
                ),
            ),
            patch(
                "app.decrypt_api_key", return_value=MISTRAL_KEY
            ) as decrypt_mock,
            patch("app.analyze_meal", return_value=SAMPLE_ANALYSIS) as analyze_mock,
        ):
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs and toast"},
            )

        self.assertEqual(response.status_code, 200)
        decrypt_mock.assert_called_once_with(
            "encrypted",
            "user@example.com",
            provider="mistral",
            aad_version=2,
        )
        analyze_mock.assert_called_once_with(
            "Two eggs and toast",
            "user@example.com",
            MISTRAL_KEY,
            "mistral",
            "mistral-small-2603",
        )

    def test_analysis_routes_through_selected_anthropic_model_and_v2_aad(self):
        headers = self.register()
        response = self.client.put(
            "/api/user/ai-settings",
            headers=headers,
            json=ANTHROPIC_SELECTION,
        )
        self.assertEqual(response.status_code, 200)

        with (
            patch(
                "app.get_ai_credential",
                return_value=(
                    True,
                    None,
                    {"ciphertext": "encrypted-anthropic", "aad_version": 2},
                ),
            ),
            patch(
                "app.decrypt_api_key", return_value=ANTHROPIC_KEY
            ) as decrypt_mock,
            patch("app.analyze_meal", return_value=SAMPLE_ANALYSIS) as analyze_mock,
        ):
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs and toast"},
            )

        self.assertEqual(response.status_code, 200)
        decrypt_mock.assert_called_once_with(
            "encrypted-anthropic",
            "user@example.com",
            provider="anthropic",
            aad_version=2,
        )
        analyze_mock.assert_called_once_with(
            "Two eggs and toast",
            "user@example.com",
            ANTHROPIC_KEY,
            "anthropic",
            "claude-sonnet-5",
        )

    def test_legacy_openai_credential_without_aad_version_uses_v1(self):
        headers = self.register()
        with (
            patch(
                "app.get_ai_credential",
                return_value=(True, None, {"ciphertext": "legacy-ciphertext"}),
            ),
            patch(
                "app.decrypt_api_key",
                return_value="user-api-key-1234567890",
            ) as decrypt_mock,
            patch("app.analyze_meal", return_value=SAMPLE_ANALYSIS),
        ):
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs"},
            )

        self.assertEqual(response.status_code, 200)
        decrypt_mock.assert_called_once_with(
            "legacy-ciphertext",
            "user@example.com",
            provider="openai",
            aad_version=1,
        )

    def test_provider_failure_returns_provider_aware_error(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    MISTRAL_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch("app.decrypt_api_key", return_value=MISTRAL_KEY),
            patch("app.analyze_meal", side_effect=AIServiceError()),
        ):
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "provider_unavailable")
        self.assertEqual(response.get_json()["provider"], "mistral")

    def test_provider_billing_errors_are_distinct_for_both_ai_routes(self):
        headers = self.register()
        cases = [
            (
                "/api/nutrition/analyze",
                {"message": "Two eggs"},
                "app.analyze_meal",
            ),
            (
                "/api/nutrition/recommend",
                {
                    "current_calories": 1200,
                    "current_protein_g": 80,
                    "target_calories": 2000,
                    "target_protein_g": 140,
                    "meals_remaining": 1,
                    "preferences": "",
                },
                "app.recommend_meals",
            ),
        ]
        for endpoint, payload, operation in cases:
            with self.subTest(endpoint=endpoint):
                with (
                    patch(
                        "app._selected_ai_credential",
                        return_value=(
                            MISTRAL_SELECTION,
                            {"ciphertext": "encrypted", "aad_version": 2},
                            None,
                        ),
                    ),
                    patch("app.decrypt_api_key", return_value=MISTRAL_KEY),
                    patch(operation, side_effect=AIBillingError()),
                ):
                    response = self.client.post(
                        endpoint,
                        headers=headers,
                        json=payload,
                    )

                self.assertEqual(response.status_code, 402)
                self.assertEqual(
                    response.get_json()["error"],
                    "provider_billing_required",
                )
                self.assertEqual(response.get_json()["provider"], "mistral")

    def test_provider_permission_error_is_not_reported_as_an_invalid_key(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    MISTRAL_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch("app.decrypt_api_key", return_value=MISTRAL_KEY),
            patch("app.analyze_meal", side_effect=AIAuthorizationError()),
        ):
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "provider_access_denied")

    def test_mistral_parse_type_error_returns_provider_unavailable(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    MISTRAL_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch("app.decrypt_api_key", return_value=MISTRAL_KEY),
            patch("services.mistral_service.Mistral") as mistral_mock,
        ):
            client = mistral_mock.return_value.__enter__.return_value
            client.chat.parse.side_effect = TypeError("unexpected parsed response")
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "provider_unavailable")
        self.assertEqual(response.get_json()["provider"], "mistral")

    def test_account_deletion_cleans_all_provider_credentials(self):
        headers = self.register()
        account_id = db_state.auth_users_memory["user@example.com"]["account_id"]
        with patch(
            "services.firebase.users.delete_all_ai_credentials",
            return_value=(True, None),
        ) as delete_all:
            response = self.client.delete(
                "/api/auth/account",
                headers=headers,
                json={"password": "password123"},
            )

        self.assertEqual(response.status_code, 200)
        delete_all.assert_called_once_with(
            "user@example.com",
            account_id,
            ANY,
        )


class AIServiceDispatchTests(unittest.TestCase):
    @patch("services.mistral_service.analyze_meal", return_value=SAMPLE_ANALYSIS)
    def test_mistral_model_is_forwarded_by_dispatcher(self, analyze_mock):
        result = ai_service.analyze_meal(
            "Two eggs",
            "user@example.com",
            MISTRAL_KEY,
            "mistral",
            "mistral-large-2512",
        )

        self.assertEqual(result, SAMPLE_ANALYSIS)
        analyze_mock.assert_called_once_with(
            "Two eggs",
            "user@example.com",
            MISTRAL_KEY,
            "mistral-large-2512",
        )

    @patch(
        "services.mistral_service.analyze_meal",
        side_effect=MistralRateLimitError(),
    )
    def test_mistral_errors_are_normalized(self, _analyze_mock):
        with self.assertRaises(AIRateLimitError):
            ai_service.analyze_meal(
                "Two eggs",
                "user@example.com",
                MISTRAL_KEY,
                "mistral",
                "mistral-small-2603",
            )

    @patch("services.anthropic_service.analyze_meal", return_value=SAMPLE_ANALYSIS)
    def test_anthropic_model_is_forwarded_by_dispatcher(self, analyze_mock):
        result = ai_service.analyze_meal(
            "Two eggs",
            "user@example.com",
            ANTHROPIC_KEY,
            "anthropic",
            "claude-opus-5",
        )

        self.assertEqual(result, SAMPLE_ANALYSIS)
        analyze_mock.assert_called_once_with(
            "Two eggs",
            "user@example.com",
            ANTHROPIC_KEY,
            "claude-opus-5",
        )

    @patch(
        "services.anthropic_service.analyze_meal",
        side_effect=AnthropicRateLimitError(),
    )
    def test_anthropic_errors_are_normalized(self, _analyze_mock):
        with self.assertRaises(AIRateLimitError):
            ai_service.analyze_meal(
                "Two eggs",
                "user@example.com",
                ANTHROPIC_KEY,
                "anthropic",
                "claude-sonnet-5",
            )


class AISettingsPersistenceRaceTests(unittest.TestCase):
    def test_old_generation_cannot_mutate_recreated_account_settings(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        transaction = Mock()

        saved, error, selection = _save_ai_selection_in_transaction(
            transaction,
            user_ref,
            "old-account",
            MISTRAL_SELECTION,
        )

        self.assertFalse(saved)
        self.assertEqual(error, "account_mismatch")
        self.assertIsNone(selection)
        transaction.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
