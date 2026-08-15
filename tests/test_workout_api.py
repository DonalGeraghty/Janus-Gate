import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import app
from services.firebase import db_state
from services.firebase.account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
)
from services.firebase.workouts import (
    _delete_workout_batch_in_transaction,
    _save_workout_entry_in_transaction,
)


SAMPLE_WORKOUT = {
    "workout_id": "strength-a",
    "title": "Strength A",
    "day": "Monday",
    "finished_at": "2026-08-14T18:57:00Z",
    "duration_minutes": 57,
    "completed": 8,
    "total": 9,
    "entries": {
        "a-goblet": {
            "done": True,
            "weight": "16",
            "result": "12, 12, 11",
        }
    },
    "note": "Good session.",
}

SAMPLE_WORKOUT_ANALYSIS = {
    "title": "Squats and rowing",
    "summary": "A strength session followed by a steady row.",
    "duration_minutes": 45,
    "exercises": [
        {
            "name": "Goblet squat",
            "sets": 3,
            "reps": "10 reps",
            "weight": "20 kg",
            "duration": None,
            "distance": None,
            "notes": None,
        },
        {
            "name": "Rowing",
            "sets": None,
            "reps": None,
            "weight": None,
            "duration": "24 minutes",
            "distance": "5 km",
            "notes": None,
        },
    ],
    "intensity": "moderate",
    "confidence": "high",
    "assumptions": [],
    "needs_clarification": False,
    "clarification_question": "",
}

OPENAI_SELECTION = {"provider": "openai", "model": "gpt-5.6-sol"}


class WorkoutApiTests(unittest.TestCase):
    def setUp(self):
        db_state.users_collection_ref = None
        db_state.db = None
        db_state.auth_users_memory.clear()
        db_state.workout_history_memory.clear()
        self.client = app.test_client()

    def tearDown(self):
        db_state.auth_users_memory.clear()
        db_state.workout_history_memory.clear()

    def register(self, email="user@example.com"):
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "password123"},
        )
        self.assertEqual(response.status_code, 201)
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    def test_workout_history_requires_authentication(self):
        self.assertEqual(self.client.get("/api/workouts").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/workouts/analyze",
                json={"message": "Three sets of squats"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.put(
                "/api/workouts/strength-a-1",
                json=SAMPLE_WORKOUT,
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.delete(
                "/api/workouts/strength-a-1"
            ).status_code,
            401,
        )

    def test_analyze_workout_returns_structure_without_writing(self):
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
            patch(
                "app.analyze_workout",
                return_value=SAMPLE_WORKOUT_ANALYSIS,
            ) as analyze_mock,
        ):
            response = self.client.post(
                "/api/workouts/analyze",
                headers=headers,
                json={
                    "message": (
                        "I did 3 sets of 10 goblet squats at 20 kg then rowed "
                        "5 km in 24 minutes"
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["analysis"]["duration_minutes"], 45)
        self.assertEqual(len(response.get_json()["analysis"]["exercises"]), 2)
        self.assertEqual(db_state.workout_history_memory, {})
        analyze_mock.assert_called_once_with(
            (
                "I did 3 sets of 10 goblet squats at 20 kg then rowed "
                "5 km in 24 minutes"
            ),
            "user@example.com",
            "user-api-key-1234567890",
            "openai",
            "gpt-5.6-sol",
        )

    def test_analyze_workout_requires_message_and_provider_key(self):
        headers = self.register()
        self.assertEqual(
            self.client.post(
                "/api/workouts/analyze",
                headers=headers,
                json={"message": "   "},
            ).status_code,
            400,
        )
        with patch(
            "app._selected_ai_credential",
            return_value=(OPENAI_SELECTION, None, None),
        ):
            response = self.client.post(
                "/api/workouts/analyze",
                headers=headers,
                json={"message": "Three sets of squats"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "provider_key_required")
        self.assertEqual(response.get_json()["provider"], "openai")

    def test_workout_history_create_list_update_and_delete(self):
        headers = self.register()
        created = self.client.put(
            "/api/workouts/strength-a-1",
            headers=headers,
            json=SAMPLE_WORKOUT,
        )
        self.assertEqual(created.status_code, 200)
        entry = created.get_json()["entry"]
        self.assertEqual(entry["id"], "strength-a-1")
        self.assertEqual(entry["finished_at"], "2026-08-14T18:57:00+00:00")
        self.assertIsNotNone(entry["created_at"])

        listed = self.client.get("/api/workouts", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.get_json()["entries"]], [
            "strength-a-1"
        ])

        replacement = {
            **SAMPLE_WORKOUT,
            "duration_minutes": 61,
            "completed": 9,
            "note": "Added the final cooldown.",
        }
        updated = self.client.put(
            "/api/workouts/strength-a-1",
            headers=headers,
            json=replacement,
        )
        self.assertEqual(updated.status_code, 200)
        updated_entry = updated.get_json()["entry"]
        self.assertEqual(updated_entry["duration_minutes"], 61)
        self.assertEqual(updated_entry["completed"], 9)
        self.assertEqual(updated_entry["note"], "Added the final cooldown.")
        self.assertEqual(updated_entry["created_at"], entry["created_at"])

        deleted = self.client.delete(
            "/api/workouts/strength-a-1",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/workouts", headers=headers
            ).get_json()["entries"],
            [],
        )
        self.assertEqual(
            self.client.delete(
                "/api/workouts/strength-a-1", headers=headers
            ).status_code,
            404,
        )

    def test_ai_workout_details_are_persisted(self):
        headers = self.register()
        payload = {
            **SAMPLE_WORKOUT,
            "workout_id": "ai-workout",
            "title": "Squats and rowing",
            "entries": {
                "ai-exercise-1": {
                    "done": True,
                    "name": "Goblet squat",
                    "weight": "20 kg",
                    "result": "3 sets · 10 reps",
                }
            },
            "source_message": "I did squats and rowing.",
        }
        response = self.client.put(
            "/api/workouts/ai-workout-1",
            headers=headers,
            json=payload,
        )

        self.assertEqual(response.status_code, 200)
        entry = response.get_json()["entry"]
        self.assertEqual(entry["source_message"], payload["source_message"])
        self.assertEqual(
            entry["entries"]["ai-exercise-1"]["name"],
            "Goblet squat",
        )

    def test_workout_history_is_isolated_by_account(self):
        first_headers = self.register("first@example.com")
        self.client.put(
            "/api/workouts/strength-a-1",
            headers=first_headers,
            json=SAMPLE_WORKOUT,
        )

        second_headers = self.register("second@example.com")
        self.assertEqual(
            self.client.get(
                "/api/workouts", headers=second_headers
            ).get_json()["entries"],
            [],
        )
        self.assertEqual(
            self.client.delete(
                "/api/workouts/strength-a-1",
                headers=second_headers,
            ).status_code,
            404,
        )
        self.assertEqual(
            len(
                self.client.get(
                    "/api/workouts", headers=first_headers
                ).get_json()["entries"]
            ),
            1,
        )

    def test_invalid_workout_history_is_rejected(self):
        headers = self.register()
        invalid = {
            **SAMPLE_WORKOUT,
            "completed": 10,
            "total": 9,
        }
        self.assertEqual(
            self.client.put(
                "/api/workouts/strength-a-1",
                headers=headers,
                json=invalid,
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.put(
                "/api/workouts/not.valid",
                headers=headers,
                json=SAMPLE_WORKOUT,
            ).status_code,
            400,
        )

    def test_account_deletion_removes_workout_history(self):
        headers = self.register()
        self.client.put(
            "/api/workouts/strength-a-1",
            headers=headers,
            json=SAMPLE_WORKOUT,
        )
        self.assertIn("user@example.com", db_state.workout_history_memory)

        deleted = self.client.delete(
            "/api/auth/account",
            headers=headers,
            json={"password": "password123"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn("user@example.com", db_state.workout_history_memory)


class WorkoutPersistenceRaceTests(unittest.TestCase):
    def test_save_rejects_a_recreated_account_generation(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        entry_ref = Mock()
        transaction = Mock()

        saved, error, entry = _save_workout_entry_in_transaction(
            transaction,
            user_ref,
            entry_ref,
            "old-account",
            {"created_at": "now"},
        )

        self.assertFalse(saved)
        self.assertEqual(error, "account_mismatch")
        self.assertIsNone(entry)
        entry_ref.get.assert_not_called()
        transaction.set.assert_not_called()

    def test_cleanup_requires_the_matching_account_deletion_token(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {
                "account_id": "account-1",
                ACCOUNT_DELETION_FIELD: True,
                ACCOUNT_DELETION_TOKEN_FIELD: "different-token",
            },
        )
        transaction = Mock()
        document_refs = [Mock(), Mock()]

        deleted, error = _delete_workout_batch_in_transaction(
            transaction,
            user_ref,
            document_refs,
            "account-1",
            "expected-token",
        )

        self.assertFalse(deleted)
        self.assertEqual(error, "account_mismatch")
        transaction.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
