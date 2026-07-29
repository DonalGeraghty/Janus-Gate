import json
import unittest
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import app
from services.firebase import db_state
from services.firebase.account_state import ACCOUNT_DELETION_FIELD
from services.firebase.nutrition import (
    _create_nutrition_entry_in_transaction,
    _delete_nutrition_batch_in_transaction,
    _update_nutrition_entry_in_transaction,
    create_nutrition_entry,
)
from services.openai_service import OpenAIRateLimitError, OpenAIServiceError


SAMPLE_ANALYSIS = {
    "items": [
        {"food": "Scrambled eggs", "portion": "2 large eggs", "calories": 180, "protein_g": 13.0},
        {"food": "Toast", "portion": "2 slices", "calories": 160, "protein_g": 6.0},
    ],
    "total_calories": 340,
    "total_protein_g": 19.0,
    "confidence": "medium",
    "assumptions": ["Standard sliced bread was assumed."],
    "needs_clarification": False,
    "clarification_question": "",
}

SAMPLE_RECOMMENDATION = {
    "summary": "Two protein-focused meals fit the remaining budget.",
    "meals": [
        {
            "name": "Chicken salad",
            "items": [
                {
                    "food": "Chicken breast",
                    "portion": "150 g cooked",
                    "calories": 250,
                    "protein_g": 46.0,
                },
                {
                    "food": "Mixed salad",
                    "portion": "1 large bowl",
                    "calories": 100,
                    "protein_g": 4.0,
                },
            ],
            "rationale": "Lean protein with plenty of vegetables.",
        },
        {
            "name": "Yoghurt and berries",
            "items": [
                {
                    "food": "Greek yoghurt",
                    "portion": "200 g",
                    "calories": 150,
                    "protein_g": 20.0,
                },
                {
                    "food": "Mixed berries",
                    "portion": "100 g",
                    "calories": 50,
                    "protein_g": 1.0,
                },
            ],
            "rationale": "A light high-protein final meal.",
        },
    ],
    "assumptions": ["Low-fat Greek yoghurt was assumed."],
}

OPENAI_SELECTION = {"provider": "openai", "model": "gpt-5.6-sol"}


class NutritionApiTests(unittest.TestCase):
    def setUp(self):
        db_state.users_collection_ref = None
        db_state.auth_users_memory.clear()
        db_state.nutrition_entries_memory.clear()
        self.client = app.test_client()

    def register(self, email="user@example.com"):
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "password123"},
        )
        self.assertEqual(response.status_code, 201)
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    def test_analyze_requires_authentication(self):
        response = self.client.post("/api/nutrition/analyze", json={"message": "Two eggs"})
        self.assertEqual(response.status_code, 401)

    def test_analyze_returns_structured_meal_without_writing(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    OPENAI_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch("app.decrypt_api_key", return_value="user-api-key-1234567890"),
            patch("app.analyze_meal", return_value=SAMPLE_ANALYSIS) as analyze_mock,
        ):
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs and toast"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["analysis"]["total_calories"], 340)
        self.assertEqual(db_state.nutrition_entries_memory, {})
        analyze_mock.assert_called_once_with(
            "Two eggs and toast",
            "user@example.com",
            "user-api-key-1234567890",
            "openai",
            "gpt-5.6-sol",
        )

    def test_analyze_requires_user_openai_key(self):
        headers = self.register()
        with patch(
            "app._selected_ai_credential",
            return_value=(OPENAI_SELECTION, None, None),
        ):
            response = self.client.post(
                "/api/nutrition/analyze", headers=headers, json={"message": "Two eggs"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "provider_key_required")
        self.assertEqual(response.get_json()["provider"], "openai")

    def test_analyze_reports_rate_limit(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    OPENAI_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch("app.decrypt_api_key", return_value="user-api-key-1234567890"),
            patch("app.analyze_meal", side_effect=OpenAIRateLimitError()),
        ):
            response = self.client.post(
                "/api/nutrition/analyze", headers=headers, json={"message": "Two eggs"}
            )
        self.assertEqual(response.status_code, 429)

    def test_openai_parse_error_returns_provider_unavailable(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    OPENAI_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch(
                "app.decrypt_api_key",
                return_value="user-api-key-1234567890",
            ),
            patch("services.openai_service.OpenAI") as openai_mock,
        ):
            openai_mock.return_value.responses.parse.side_effect = (
                json.JSONDecodeError("invalid response", "{", 0)
            )
            response = self.client.post(
                "/api/nutrition/analyze",
                headers=headers,
                json={"message": "Two eggs"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "provider_unavailable")
        self.assertEqual(response.get_json()["provider"], "openai")

    def test_recommend_returns_structured_plan_without_writing(self):
        headers = self.register()
        with (
            patch(
                "app._selected_ai_credential",
                return_value=(
                    OPENAI_SELECTION,
                    {"ciphertext": "encrypted", "aad_version": 2},
                    None,
                ),
            ),
            patch("app.decrypt_api_key", return_value="user-api-key-1234567890"),
            patch("app.recommend_meals", return_value={
                **SAMPLE_RECOMMENDATION,
                "calorie_budget_remaining": 800,
                "protein_remaining_g": 60.0,
                "plan_total_calories": 550,
                "plan_total_protein_g": 71.0,
                "projected_daily_calories": 1750,
                "projected_daily_protein_g": 151.0,
            }) as recommend_mock,
        ):
            response = self.client.post(
                "/api/nutrition/recommend",
                headers=headers,
                json={
                    "current_calories": 1200,
                    "current_protein_g": 80,
                    "target_calories": 2000,
                    "target_protein_g": 140,
                    "meals_remaining": 2,
                    "preferences": "No shellfish",
                },
            )

        self.assertEqual(response.status_code, 200)
        recommendation = response.get_json()["recommendation"]
        self.assertEqual(recommendation["plan_total_calories"], 550)
        self.assertEqual(len(recommendation["meals"]), 2)
        self.assertEqual(db_state.nutrition_entries_memory, {})
        context = recommend_mock.call_args.args[0]
        self.assertEqual(context.current_calories, 1200)
        self.assertEqual(context.preferences, "No shellfish")
        self.assertEqual(
            recommend_mock.call_args.args[1:],
            (
                "user@example.com",
                "user-api-key-1234567890",
                "openai",
                "gpt-5.6-sol",
            ),
        )

    def test_recommend_validates_input_and_requires_key(self):
        headers = self.register()
        response = self.client.post(
            "/api/nutrition/recommend",
            headers=headers,
            json={
                "current_calories": 1200,
                "current_protein_g": 80,
                "target_calories": 200,
                "target_protein_g": 140,
                "meals_remaining": 4,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "invalid_recommendation_request"
        )

        with patch(
            "app._selected_ai_credential",
            return_value=(OPENAI_SELECTION, None, None),
        ):
            response = self.client.post(
                "/api/nutrition/recommend",
                headers=headers,
                json={
                    "current_calories": 1200,
                    "current_protein_g": 80,
                    "target_calories": 2000,
                    "target_protein_g": 140,
                    "meals_remaining": 2,
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "provider_key_required")
        self.assertEqual(response.get_json()["provider"], "openai")

    def test_confirmed_entry_is_saved_and_totals_are_recalculated(self):
        headers = self.register()
        response = self.client.post(
            "/api/nutrition/entries",
            headers=headers,
            json={
                "items": SAMPLE_ANALYSIS["items"],
                "eaten_at": "2026-07-22T12:30:00Z",
                "source_message": "Two eggs and toast",
            },
        )
        self.assertEqual(response.status_code, 201)
        entry = response.get_json()["entry"]
        self.assertEqual(entry["total_calories"], 340)
        self.assertEqual(entry["total_protein_g"], 19.0)

        response = self.client.get("/api/nutrition/entries?date=2026-07-22", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["entries"]), 1)
        response = self.client.get("/api/nutrition/entries?date=2026-07-21", headers=headers)
        self.assertEqual(response.get_json()["entries"], [])

    def test_entries_can_be_listed_by_timezone_aware_range(self):
        headers = self.register()
        for eaten_at in (
            "2026-07-26T22:59:59Z",
            "2026-07-26T23:00:00Z",
            "2026-08-02T22:59:59Z",
            "2026-08-02T23:00:00Z",
        ):
            self.client.post(
                "/api/nutrition/entries",
                headers=headers,
                json={
                    "items": SAMPLE_ANALYSIS["items"],
                    "eaten_at": eaten_at,
                },
            )

        response = self.client.get(
            (
                "/api/nutrition/entries"
                "?start=2026-07-27T00:00:00%2B01:00"
                "&end=2026-08-03T00:00:00%2B01:00"
            ),
            headers=headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(
            [entry["eaten_at"] for entry in body["entries"]],
            [
                "2026-08-02T22:59:59+00:00",
                "2026-07-26T23:00:00+00:00",
            ],
        )
        self.assertEqual(body["pagination"]["limit"], 500)
        self.assertFalse(body["pagination"]["truncated"])
        self.assertEqual(
            body["pagination"]["start"],
            "2026-07-26T23:00:00+00:00",
        )

    def test_entry_range_parameters_are_validated(self):
        headers = self.register()
        invalid_queries = (
            "?start=2026-07-27T00:00:00Z",
            "?end=2026-08-03T00:00:00Z",
            "?start=2026-07-27T00:00:00&end=2026-08-03T00:00:00",
            "?start=2026-08-03T00:00:00Z&end=2026-07-27T00:00:00Z",
            "?start=2026-07-01T00:00:00Z&end=2026-08-01T00:00:00Z",
            (
                "?date=2026-07-27"
                "&start=2026-07-27T00:00:00Z"
                "&end=2026-08-03T00:00:00Z"
            ),
        )

        for query in invalid_queries:
            with self.subTest(query=query):
                response = self.client.get(
                    f"/api/nutrition/entries{query}",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)

    def test_entry_range_reports_when_its_limit_is_truncated(self):
        headers = self.register()
        for hour in (8, 12):
            self.client.post(
                "/api/nutrition/entries",
                headers=headers,
                json={
                    "items": SAMPLE_ANALYSIS["items"],
                    "eaten_at": f"2026-07-27T{hour:02d}:00:00Z",
                },
            )

        response = self.client.get(
            (
                "/api/nutrition/entries"
                "?start=2026-07-27T00:00:00Z"
                "&end=2026-08-03T00:00:00Z"
                "&limit=1"
            ),
            headers=headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(body["pagination"]["limit"], 1)
        self.assertTrue(body["pagination"]["truncated"])

    def test_entries_are_isolated_by_user(self):
        first_headers = self.register("first@example.com")
        self.client.post(
            "/api/nutrition/entries",
            headers=first_headers,
            json={"items": SAMPLE_ANALYSIS["items"]},
        )
        second_headers = self.register("second@example.com")
        response = self.client.get("/api/nutrition/entries", headers=second_headers)
        self.assertEqual(response.get_json()["entries"], [])

    def test_entry_delete_removes_only_the_owners_record(self):
        first_headers = self.register("first@example.com")
        created = self.client.post(
            "/api/nutrition/entries",
            headers=first_headers,
            json={"items": SAMPLE_ANALYSIS["items"]},
        ).get_json()["entry"]

        second_headers = self.register("second@example.com")
        response = self.client.delete(
            f"/api/nutrition/entries/{created['id']}", headers=second_headers
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.delete(
            f"/api/nutrition/entries/{created['id']}", headers=first_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/nutrition/entries", headers=first_headers
            ).get_json()["entries"],
            [],
        )
        self.assertEqual(
            self.client.delete(
                f"/api/nutrition/entries/{created['id']}", headers=first_headers
            ).status_code,
            404,
        )

    def test_entry_update_recalculates_totals_and_checks_ownership(self):
        first_headers = self.register("first@example.com")
        created = self.client.post(
            "/api/nutrition/entries",
            headers=first_headers,
            json={"items": SAMPLE_ANALYSIS["items"]},
        ).get_json()["entry"]
        replacement = {
            "items": [
                {
                    "food": "Greek yoghurt",
                    "portion": "200 g",
                    "calories": 150,
                    "protein_g": 20,
                }
            ],
            "eaten_at": "2026-07-22T09:30:00Z",
        }

        second_headers = self.register("second@example.com")
        self.assertEqual(
            self.client.put(
                f"/api/nutrition/entries/{created['id']}",
                headers=second_headers,
                json=replacement,
            ).status_code,
            404,
        )

        response = self.client.put(
            f"/api/nutrition/entries/{created['id']}",
            headers=first_headers,
            json=replacement,
        )
        self.assertEqual(response.status_code, 200)
        entry = response.get_json()["entry"]
        self.assertEqual(entry["total_calories"], 150)
        self.assertEqual(entry["total_protein_g"], 20.0)
        self.assertEqual(entry["items"][0]["food"], "Greek yoghurt")
        self.assertIsNotNone(entry["updated_at"])

    def test_invalid_entry_and_date_are_rejected(self):
        headers = self.register()
        self.assertEqual(
            self.client.post(
                "/api/nutrition/entries",
                headers=headers,
                json={"items": [{"food": "Egg", "calories": -1}]},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/nutrition/entries?date=22-07-2026", headers=headers
            ).status_code,
            400,
        )

    def test_deleting_account_also_deletes_nutrition_entries(self):
        headers = self.register()
        self.client.post(
            "/api/nutrition/entries",
            headers=headers,
            json={"items": SAMPLE_ANALYSIS["items"]},
        )
        self.assertIn("user@example.com", db_state.nutrition_entries_memory)
        response = self.client.delete(
            "/api/auth/account", headers=headers, json={"password": "password123"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("user@example.com", db_state.nutrition_entries_memory)


class NutritionPersistenceRaceTests(unittest.TestCase):
    def setUp(self):
        db_state.users_collection_ref = None
        db_state.auth_users_memory.clear()
        db_state.nutrition_entries_memory.clear()

    def test_create_rejects_tombstoned_parent(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {
                "account_id": "account-1",
                ACCOUNT_DELETION_FIELD: True,
            },
        )
        entry_ref = Mock()
        transaction = Mock()

        created, error, entry = _create_nutrition_entry_in_transaction(
            transaction,
            user_ref,
            entry_ref,
            "account-1",
            {"items": []},
        )

        self.assertFalse(created)
        self.assertEqual(error, "account_deleting")
        self.assertIsNone(entry)
        transaction.set.assert_not_called()

    def test_update_rejects_recreated_parent_generation(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        entry_ref = Mock()
        transaction = Mock()

        updated, error, entry = _update_nutrition_entry_in_transaction(
            transaction,
            user_ref,
            entry_ref,
            "old-account",
            {"items": [], "created_at": "new"},
        )

        self.assertFalse(updated)
        self.assertEqual(error, "account_mismatch")
        self.assertIsNone(entry)
        entry_ref.get.assert_not_called()
        transaction.set.assert_not_called()

    def test_slow_cleanup_rejects_recreated_parent_generation(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        transaction = Mock()
        document_refs = [Mock(), Mock()]

        deleted, error = _delete_nutrition_batch_in_transaction(
            transaction,
            user_ref,
            document_refs,
            "old-account",
            "old-deletion-token",
        )

        self.assertFalse(deleted)
        self.assertEqual(error, "account_mismatch")
        transaction.delete.assert_not_called()

    def test_memory_create_checks_and_writes_under_shared_lock(self):
        email = "user@example.com"
        db_state.auth_users_memory[email] = {
            "email": email,
            "account_id": "account-1",
        }
        started = Event()
        finished = Event()
        outcome = []

        def create_entry():
            started.set()
            outcome.append(
                create_nutrition_entry(
                    email,
                    SAMPLE_ANALYSIS["items"],
                    datetime.now(timezone.utc),
                    account_id="account-1",
                )
            )
            finished.set()

        with db_state.memory_lock:
            worker = Thread(target=create_entry)
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(finished.wait(timeout=0.1))
            db_state.auth_users_memory[email][
                ACCOUNT_DELETION_FIELD
            ] = True

        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(outcome[0][0])
        self.assertEqual(outcome[0][1], "account_deleting")
        self.assertNotIn(email, db_state.nutrition_entries_memory)


class OpenAIServiceTests(unittest.TestCase):
    @patch("services.openai_service.OpenAI")
    def test_model_totals_are_recalculated(self, openai_mock):
        from services.openai_service import MealAnalysis, analyze_meal

        parsed = MealAnalysis.model_validate({**SAMPLE_ANALYSIS, "total_calories": 999})
        openai_mock.return_value.responses.parse.return_value.output_parsed = parsed
        result = analyze_meal(
            "Two eggs and toast",
            "user@example.com",
            "user-api-key-1234567890",
            "gpt-5.6-terra",
        )
        self.assertEqual(result["total_calories"], 340)
        openai_mock.assert_called_once_with(api_key="user-api-key-1234567890")
        request = openai_mock.return_value.responses.parse.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-terra")
        self.assertFalse(request["store"])
        self.assertNotIn("user@example.com", str(request))

    @patch("services.openai_service.OpenAI")
    def test_recommendation_totals_are_recalculated(self, openai_mock):
        from core.nutrition_service import MealRecommendationInput
        from services.openai_service import MealRecommendation, recommend_meals

        parsed = MealRecommendation.model_validate(SAMPLE_RECOMMENDATION)
        openai_mock.return_value.responses.parse.return_value.output_parsed = parsed
        context = MealRecommendationInput(
            current_calories=1200,
            current_protein_g=80,
            target_calories=2000,
            target_protein_g=140,
            meals_remaining=2,
            preferences="No shellfish",
        )
        result = recommend_meals(
            context,
            "user@example.com",
            "user-api-key-1234567890",
            "gpt-5.6-luna",
        )

        self.assertEqual(result["meals"][0]["total_calories"], 350)
        self.assertEqual(result["meals"][0]["total_protein_g"], 50.0)
        self.assertEqual(result["plan_total_calories"], 550)
        self.assertEqual(result["plan_total_protein_g"], 71.0)
        self.assertEqual(result["projected_daily_calories"], 1750)
        self.assertEqual(result["projected_daily_protein_g"], 151.0)
        request = openai_mock.return_value.responses.parse.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertFalse(request["store"])
        self.assertNotIn("user@example.com", str(request))

    def test_structured_parse_errors_are_mapped_to_service_error(self):
        from core.nutrition_service import MealRecommendationInput
        from services.openai_service import analyze_meal, recommend_meals

        context = MealRecommendationInput(
            current_calories=1200,
            current_protein_g=80,
            target_calories=2000,
            target_protein_g=140,
            meals_remaining=2,
        )
        parse_errors = (
            json.JSONDecodeError("invalid response", "{", 0),
            TypeError("unexpected parsed response"),
        )
        for parse_error in parse_errors:
            with self.subTest(
                operation="analysis",
                error=type(parse_error).__name__,
            ):
                with patch("services.openai_service.OpenAI") as openai_mock:
                    openai_mock.return_value.responses.parse.side_effect = (
                        parse_error
                    )
                    with self.assertRaises(OpenAIServiceError):
                        analyze_meal(
                            "Two eggs",
                            "user@example.com",
                            "user-api-key-1234567890",
                        )
            with self.subTest(
                operation="recommendation",
                error=type(parse_error).__name__,
            ):
                with patch("services.openai_service.OpenAI") as openai_mock:
                    openai_mock.return_value.responses.parse.side_effect = (
                        parse_error
                    )
                    with self.assertRaises(OpenAIServiceError):
                        recommend_meals(
                            context,
                            "user@example.com",
                            "user-api-key-1234567890",
                        )


if __name__ == "__main__":
    unittest.main()
