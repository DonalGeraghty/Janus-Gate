import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

from app import app
from services.firebase import db_state
from services.firebase.account_state import ACCOUNT_ID_FIELD


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

    def test_old_token_is_rejected_after_delete_and_reregister(self):
        payload = {"email": "user@example.com", "password": "password123"}
        first = self.client.post("/api/auth/register", json=payload)
        old_token = first.get_json()["token"]
        old_account_id = db_state.auth_users_memory[
            "user@example.com"
        ][ACCOUNT_ID_FIELD]
        old_headers = {"Authorization": f"Bearer {old_token}"}

        deleted = self.client.delete(
            "/api/auth/account",
            headers=old_headers,
            json={"password": "password123"},
        )
        self.assertEqual(deleted.status_code, 200)

        second = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(second.status_code, 201)
        new_account_id = db_state.auth_users_memory[
            "user@example.com"
        ][ACCOUNT_ID_FIELD]
        self.assertNotEqual(old_account_id, new_account_id)
        self.assertEqual(
            self.client.get("/api/auth/me", headers=old_headers).status_code,
            401,
        )
        new_headers = {
            "Authorization": f"Bearer {second.get_json()['token']}"
        }
        self.assertEqual(
            self.client.get("/api/auth/me", headers=new_headers).status_code,
            200,
        )

    @patch.dict(os.environ, {"JWT_SECRET_KEY": "test-secret"})
    def test_legacy_token_migrates_only_after_successful_password_login(self):
        self.client.post(
            "/api/auth/register",
            json={"email": "legacy@example.com", "password": "password123"},
        )
        legacy_user = db_state.auth_users_memory["legacy@example.com"]
        legacy_user.pop(ACCOUNT_ID_FIELD)
        now = datetime.now(timezone.utc)
        legacy_token = jwt.encode(
            {
                "sub": "legacy@example.com",
                "iat": now,
                "exp": now + timedelta(days=1),
            },
            "test-secret",
            algorithm="HS256",
        )
        legacy_headers = {"Authorization": f"Bearer {legacy_token}"}

        bad_login = self.client.post(
            "/api/auth/login",
            json={"email": "legacy@example.com", "password": "wrong-pass"},
        )
        self.assertEqual(bad_login.status_code, 401)
        self.assertNotIn(ACCOUNT_ID_FIELD, legacy_user)
        self.assertEqual(
            self.client.get("/api/auth/me", headers=legacy_headers).status_code,
            401,
        )

        migrated = self.client.post(
            "/api/auth/login",
            json={"email": "legacy@example.com", "password": "password123"},
        )
        self.assertEqual(migrated.status_code, 200)
        self.assertIn(ACCOUNT_ID_FIELD, legacy_user)
        self.assertEqual(
            self.client.get("/api/auth/me", headers=legacy_headers).status_code,
            401,
        )
        migrated_headers = {
            "Authorization": f"Bearer {migrated.get_json()['token']}"
        }
        self.assertEqual(
            self.client.get(
                "/api/auth/me",
                headers=migrated_headers,
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
