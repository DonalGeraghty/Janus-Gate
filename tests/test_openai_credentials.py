import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import app
from services.credential_service import decrypt_api_key, encrypt_api_key
from services.firebase import db_state


USER_KEY = "sk-project-user-key-1234567890-Ab12"


class OpenAICredentialApiTests(unittest.TestCase):
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
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    def test_key_endpoints_require_authentication(self):
        self.assertEqual(self.client.get("/api/user/openai-key").status_code, 401)
        self.assertEqual(
            self.client.put("/api/user/openai-key", json={"api_key": USER_KEY}).status_code,
            401,
        )
        self.assertEqual(self.client.delete("/api/user/openai-key").status_code, 401)

    def test_key_is_verified_encrypted_and_never_returned(self):
        headers = self.register()
        status = {
            "configured": True,
            "last_four": "Ab12",
            "verified_at": "2026-07-22T20:00:00+00:00",
            "updated_at": "2026-07-22T20:00:00+00:00",
        }
        with (
            patch("app.validate_api_key", return_value=USER_KEY) as validate_mock,
            patch("app.encrypt_api_key", return_value="kms-ciphertext") as encrypt_mock,
            patch("app.save_openai_credential", return_value=(True, None, status)) as save_mock,
        ):
            response = self.client.put(
                "/api/user/openai-key", headers=headers, json={"api_key": USER_KEY}
            )

        self.assertEqual(response.status_code, 200)
        response_text = response.get_data(as_text=True)
        self.assertNotIn(USER_KEY, response_text)
        self.assertNotIn("kms-ciphertext", response_text)
        self.assertEqual(response.get_json()["credential"]["last_four"], "Ab12")
        validate_mock.assert_called_once_with(USER_KEY, "user@example.com")
        encrypt_mock.assert_called_once_with(USER_KEY, "user@example.com")
        save_mock.assert_called_once_with("user@example.com", "kms-ciphertext", "Ab12")

    def test_invalid_replacement_does_not_overwrite_existing_key(self):
        headers = self.register()
        with (
            patch("app.validate_api_key", side_effect=ValueError("invalid_api_key")),
            patch("app.encrypt_api_key") as encrypt_mock,
            patch("app.save_openai_credential") as save_mock,
        ):
            response = self.client.put(
                "/api/user/openai-key", headers=headers, json={"api_key": "bad"}
            )
        self.assertEqual(response.status_code, 400)
        encrypt_mock.assert_not_called()
        save_mock.assert_not_called()

    def test_status_returns_only_safe_metadata(self):
        headers = self.register()
        with patch(
            "app.get_openai_credential_status",
            return_value=(True, None, {"configured": True, "last_four": "Ab12"}),
        ):
            response = self.client.get("/api/user/openai-key", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["credential"],
            {"configured": True, "last_four": "Ab12"},
        )

    def test_key_can_be_removed(self):
        headers = self.register()
        with patch("app.delete_openai_credential", return_value=(True, None)) as delete_mock:
            response = self.client.delete("/api/user/openai-key", headers=headers)
        self.assertEqual(response.status_code, 200)
        delete_mock.assert_called_once_with("user@example.com")

    def test_account_deletion_invokes_credential_cleanup(self):
        headers = self.register()
        with patch(
            "services.firebase.users.delete_openai_credential", return_value=(True, None)
        ) as delete_mock:
            response = self.client.delete(
                "/api/auth/account",
                headers=headers,
                json={"password": "password123"},
            )
        self.assertEqual(response.status_code, 200)
        delete_mock.assert_called_once_with("user@example.com")


class KmsCredentialServiceTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"OPENAI_KMS_KEY_NAME": "projects/test/locations/europe-west1/keyRings/janus/cryptoKeys/users"},
    )
    @patch("services.credential_service.kms.KeyManagementServiceClient")
    def test_encrypt_and_decrypt_use_user_bound_aad(self, client_class):
        client = client_class.return_value
        client.encrypt.return_value = SimpleNamespace(ciphertext=b"encrypted-key")
        client.decrypt.return_value = SimpleNamespace(plaintext=USER_KEY.encode("utf-8"))

        ciphertext = encrypt_api_key(USER_KEY, "User@Example.com")
        plaintext = decrypt_api_key(ciphertext, "user@example.com")

        self.assertEqual(plaintext, USER_KEY)
        self.assertEqual(ciphertext, base64.b64encode(b"encrypted-key").decode("ascii"))
        encrypt_request = client.encrypt.call_args.kwargs["request"]
        decrypt_request = client.decrypt.call_args.kwargs["request"]
        expected_aad = b"janus-gate:openai-key:user@example.com"
        self.assertEqual(encrypt_request["additional_authenticated_data"], expected_aad)
        self.assertEqual(decrypt_request["additional_authenticated_data"], expected_aad)
        self.assertNotIn(USER_KEY, ciphertext)


if __name__ == "__main__":
    unittest.main()
