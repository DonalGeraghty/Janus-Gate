import unittest

from app import app
from services.firebase import db_state


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        db_state.users_collection_ref = None
        db_state.auth_users_memory.clear()
        self.client = app.test_client()

    def test_user_lifecycle(self):
        response = self.client.post(
            "/api/auth/register",
            json={"email": "User@Example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["user"]["email"], "user@example.com")
        token = response.get_json()["token"]

        response = self.client.post(
            "/api/auth/login",
            json={"email": "user@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)

        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.assertEqual(
            self.client.delete(
                "/api/auth/account",
                headers=headers,
                json={"password": "password123"},
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": "user@example.com", "password": "password123"},
            ).status_code,
            401,
        )

    def test_duplicate_registration_is_rejected(self):
        payload = {"email": "user@example.com", "password": "password123"}
        self.assertEqual(self.client.post("/api/auth/register", json=payload).status_code, 201)
        self.assertEqual(self.client.post("/api/auth/register", json=payload).status_code, 409)

    def test_delete_requires_token_and_correct_password(self):
        payload = {"email": "user@example.com", "password": "password123"}
        token = self.client.post("/api/auth/register", json=payload).get_json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        self.assertEqual(
            self.client.delete("/api/auth/account", json={"password": "password123"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.delete(
                "/api/auth/account", headers=headers, json={"password": "wrong-pass"}
            ).status_code,
            401,
        )

    def test_invalid_json_types_are_rejected(self):
        self.assertEqual(
            self.client.post(
                "/api/auth/register", json={"email": 123, "password": []}
            ).status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
