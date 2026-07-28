import unittest
from unittest.mock import patch

from app import app
from services.firebase import db_state
from services.openai_service import OpenAIRateLimitError


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
            patch("app.get_openai_credential", return_value=(True, None, {"ciphertext": "encrypted"})),
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
            "Two eggs and toast", "user@example.com", "user-api-key-1234567890"
        )

    def test_analyze_requires_user_openai_key(self):
        headers = self.register()
        with patch("app.get_openai_credential", return_value=(True, None, None)):
            response = self.client.post(
                "/api/nutrition/analyze", headers=headers, json={"message": "Two eggs"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "openai_key_required")

    def test_analyze_reports_rate_limit(self):
        headers = self.register()
        with (
            patch("app.get_openai_credential", return_value=(True, None, {"ciphertext": "encrypted"})),
            patch("app.decrypt_api_key", return_value="user-api-key-1234567890"),
            patch("app.analyze_meal", side_effect=OpenAIRateLimitError()),
        ):
            response = self.client.post(
                "/api/nutrition/analyze", headers=headers, json={"message": "Two eggs"}
            )
        self.assertEqual(response.status_code, 429)

    def test_recommend_returns_structured_plan_without_writing(self):
        headers = self.register()
        with (
            patch("app.get_openai_credential", return_value=(True, None, {"ciphertext": "encrypted"})),
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

        with patch("app.get_openai_credential", return_value=(True, None, None)):
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
        self.assertEqual(response.get_json()["error"], "openai_key_required")

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


class OpenAIServiceTests(unittest.TestCase):
    @patch("services.openai_service.OpenAI")
    def test_model_totals_are_recalculated(self, openai_mock):
        from services.openai_service import MealAnalysis, analyze_meal

        parsed = MealAnalysis.model_validate({**SAMPLE_ANALYSIS, "total_calories": 999})
        openai_mock.return_value.responses.parse.return_value.output_parsed = parsed
        result = analyze_meal(
            "Two eggs and toast", "user@example.com", "user-api-key-1234567890"
        )
        self.assertEqual(result["total_calories"], 340)
        openai_mock.assert_called_once_with(api_key="user-api-key-1234567890")
        request = openai_mock.return_value.responses.parse.call_args.kwargs
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
            context, "user@example.com", "user-api-key-1234567890"
        )

        self.assertEqual(result["meals"][0]["total_calories"], 350)
        self.assertEqual(result["meals"][0]["total_protein_g"], 50.0)
        self.assertEqual(result["plan_total_calories"], 550)
        self.assertEqual(result["plan_total_protein_g"], 71.0)
        self.assertEqual(result["projected_daily_calories"], 1750)
        self.assertEqual(result["projected_daily_protein_g"], 151.0)
        request = openai_mock.return_value.responses.parse.call_args.kwargs
        self.assertFalse(request["store"])
        self.assertNotIn("user@example.com", str(request))


if __name__ == "__main__":
    unittest.main()
