import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import app
from core.flashcard_service import schedule_review
from services.ai_contract import MinervaResponse
from services.firebase import db_state


OPENAI_SELECTION = {"provider": "openai", "model": "gpt-5.6-sol"}
SAMPLE_DRAFT = {
    "kind": "card_draft",
    "reply": "I prepared a Hindi vocabulary card for review.",
    "cards": [{
        "front": "What does कल mean in Hindi?",
        "back": "Yesterday or tomorrow; context-dependent.",
        "suggested_tags": ["hindi", "vocabulary"],
    }],
}


class FlashcardsApiTests(unittest.TestCase):
    def setUp(self):
        db_state.users_collection_ref = None
        db_state.db = None
        db_state.auth_users_memory.clear()
        db_state.flashcards_memory.clear()
        db_state.flashcard_reviews_memory.clear()
        self.client = app.test_client()

    def register(self, email="user@example.com"):
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "password123"},
        )
        self.assertEqual(response.status_code, 201)
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    def create_card(self, headers, **overrides):
        payload = {
            "front": "What is a closure?",
            "back": "A function bundled with access to its lexical environment.",
            "tags": [" Computing ", "JavaScript", "computing"],
            "source_message": "Create a computing card about closures",
            "client_request_id": "34bb3b48-652e-4dd9-99b5-c44e95c23011",
            **overrides,
        }
        return self.client.post("/api/flashcards", headers=headers, json=payload)

    def test_minerva_and_flashcard_routes_require_authentication(self):
        self.assertEqual(
            self.client.post("/api/minerva/respond", json={"message": "Hello"}).status_code,
            401,
        )
        self.assertEqual(self.client.get("/api/flashcards").status_code, 401)
        self.assertEqual(self.client.get("/api/flashcards/due").status_code, 401)

    def test_minerva_returns_structured_draft_without_saving(self):
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
            patch("app.respond_minerva", return_value=SAMPLE_DRAFT) as respond_mock,
        ):
            response = self.client.post(
                "/api/minerva/respond",
                headers=headers,
                json={"message": "Add a Hindi card for कल"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["response"], SAMPLE_DRAFT)
        self.assertEqual(db_state.flashcards_memory, {})
        respond_mock.assert_called_once_with(
            "Add a Hindi card for कल",
            "user@example.com",
            "user-api-key-1234567890",
            "openai",
            "gpt-5.6-sol",
        )

    def test_minerva_contract_accepts_multiple_card_drafts(self):
        second_draft = {
            "front": "What part of speech is कल?",
            "back": "Adverb.",
            "suggested_tags": ["hindi", "grammar"],
        }

        response = MinervaResponse.model_validate({
            **SAMPLE_DRAFT,
            "reply": "I prepared two Hindi cards for review.",
            "cards": [*SAMPLE_DRAFT["cards"], second_draft],
        })

        self.assertEqual(len(response.cards), 2)
        self.assertEqual(response.cards[1].back, "Adverb.")

    def test_minerva_requires_selected_provider_key(self):
        headers = self.register()
        with patch(
            "app._selected_ai_credential",
            return_value=(OPENAI_SELECTION, None, None),
        ):
            response = self.client.post(
                "/api/minerva/respond",
                headers=headers,
                json={"message": "What is active recall?"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "provider_key_required")

    def test_create_is_idempotent_and_normalizes_tags(self):
        headers = self.register()
        first = self.create_card(headers)
        second = self.create_card(headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.get_json()["card"]["id"], second.get_json()["card"]["id"])
        self.assertEqual(first.get_json()["card"]["tags"], ["computing", "javascript"])
        self.assertEqual(len(db_state.flashcards_memory["user@example.com"]), 1)

    def test_list_review_update_and_delete_card(self):
        headers = self.register()
        created = self.create_card(headers).get_json()["card"]
        card_id = created["id"]

        due = self.client.get("/api/flashcards/due?tag=computing", headers=headers)
        self.assertEqual(due.status_code, 200)
        self.assertEqual([card["id"] for card in due.get_json()["cards"]], [card_id])

        reviewed = self.client.post(
            f"/api/flashcards/{card_id}/reviews",
            headers=headers,
            json={
                "rating": "good",
                "client_request_id": "a46d9181-9dd5-4860-a077-2321715a90e1",
            },
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.get_json()["card"]["review_count"], 1)
        self.assertEqual(reviewed.get_json()["card"]["interval_days"], 1)
        self.assertEqual(
            self.client.get("/api/flashcards/due", headers=headers).get_json()["cards"],
            [],
        )

        updated = self.client.put(
            f"/api/flashcards/{card_id}",
            headers=headers,
            json={
                "front": "Define a JavaScript closure.",
                "back": created["back"],
                "tags": ["computing"],
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["card"]["front"], "Define a JavaScript closure.")

        deleted = self.client.delete(f"/api/flashcards/{card_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/flashcards", headers=headers).get_json()["cards"], [])
        self.assertEqual(db_state.flashcard_reviews_memory.get("user@example.com", {}), {})

    def test_cards_are_isolated_between_accounts(self):
        first_headers = self.register("first@example.com")
        second_headers = self.register("second@example.com")
        self.create_card(first_headers)
        self.assertEqual(
            self.client.get("/api/flashcards", headers=second_headers).get_json()["cards"],
            [],
        )

    def test_account_deletion_clears_cards_and_reviews(self):
        headers = self.register()
        card_id = self.create_card(headers).get_json()["card"]["id"]
        self.client.post(
            f"/api/flashcards/{card_id}/reviews",
            headers=headers,
            json={"rating": "again"},
        )
        deleted = self.client.delete(
            "/api/auth/account",
            headers=headers,
            json={"password": "password123"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(db_state.flashcards_memory, {})
        self.assertEqual(db_state.flashcard_reviews_memory, {})


class FlashcardSchedulerTests(unittest.TestCase):
    def test_scheduler_advances_and_lapses_deterministically(self):
        now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
        new_card = {"review_count": 0, "interval_days": 0, "ease_factor": 2.5}
        good = schedule_review(new_card, "good", now)
        self.assertEqual(good["interval_days"], 1)
        self.assertEqual(good["review_count"], 1)

        again = schedule_review({**new_card, **good}, "again", now)
        self.assertEqual(again["interval_days"], 0)
        self.assertEqual(again["lapses"], 1)
        self.assertEqual((again["due_at"] - now).total_seconds(), 600)


if __name__ == "__main__":
    unittest.main()
