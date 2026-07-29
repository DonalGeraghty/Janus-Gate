import base64
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

from app import app
from services.credential_service import decrypt_api_key, encrypt_api_key
from services.firebase import db_state
from services.firebase.openai_credentials import (
    ACCOUNT_DELETION_FIELD,
    _delete_credential_for_account_deletion_in_transaction,
    _get_ai_credential_in_transaction,
    _save_ai_credential_in_transaction,
    delete_all_ai_credentials,
)
from services.firebase.account_state import (
    ACCOUNT_DELETION_STARTED_AT_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
)
from services.firebase.users import get_user_record


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
            patch("app.save_ai_credential", return_value=(True, None, status)) as save_mock,
        ):
            response = self.client.put(
                "/api/user/openai-key", headers=headers, json={"api_key": USER_KEY}
            )

        self.assertEqual(response.status_code, 200)
        response_text = response.get_data(as_text=True)
        self.assertNotIn(USER_KEY, response_text)
        self.assertNotIn("kms-ciphertext", response_text)
        self.assertEqual(response.get_json()["credential"]["last_four"], "Ab12")
        validate_mock.assert_called_once_with(
            "openai", USER_KEY, "user@example.com"
        )
        encrypt_mock.assert_called_once_with(
            USER_KEY,
            "user@example.com",
            provider="openai",
            aad_version=2,
        )
        save_mock.assert_called_once_with(
            "user@example.com",
            "openai",
            "kms-ciphertext",
            "Ab12",
            aad_version=2,
            account_id=db_state.auth_users_memory["user@example.com"]["account_id"],
        )

    def test_invalid_replacement_does_not_overwrite_existing_key(self):
        headers = self.register()
        with (
            patch("app.validate_api_key", side_effect=ValueError("invalid_api_key")),
            patch("app.encrypt_api_key") as encrypt_mock,
            patch("app.save_ai_credential") as save_mock,
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
            "app.get_ai_credential_status",
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
        with patch("app.delete_ai_credential", return_value=(True, None)) as delete_mock:
            response = self.client.delete("/api/user/openai-key", headers=headers)
        self.assertEqual(response.status_code, 200)
        delete_mock.assert_called_once_with(
            "user@example.com",
            "openai",
            db_state.auth_users_memory["user@example.com"]["account_id"],
        )

    def test_account_deletion_invokes_credential_cleanup(self):
        headers = self.register()
        account_id = db_state.auth_users_memory["user@example.com"]["account_id"]
        with patch(
            "services.firebase.users.delete_all_ai_credentials",
            return_value=(True, None),
        ) as delete_mock:
            response = self.client.delete(
                "/api/auth/account",
                headers=headers,
                json={"password": "password123"},
            )
        self.assertEqual(response.status_code, 200)
        delete_mock.assert_called_once_with(
            "user@example.com",
            account_id,
            ANY,
        )

    def test_failed_account_cleanup_remains_resumable(self):
        headers = self.register()

        def fail_after_tombstone(email, account_id, deletion_token):
            self.assertEqual(email, "user@example.com")
            self.assertEqual(
                account_id,
                db_state.auth_users_memory[email]["account_id"],
            )
            self.assertTrue(deletion_token)
            self.assertIsNone(get_user_record(email))
            return False, "database_error"

        with patch(
            "services.firebase.users.delete_all_ai_credentials",
            side_effect=fail_after_tombstone,
        ):
            response = self.client.delete(
                "/api/auth/account",
                headers=headers,
                json={"password": "password123"},
            )

        self.assertEqual(response.status_code, 500)
        user = db_state.auth_users_memory["user@example.com"]
        self.assertTrue(user[ACCOUNT_DELETION_FIELD])
        self.assertEqual(
            self.client.get("/api/auth/me", headers=headers).status_code,
            401,
        )

        with patch(
            "services.firebase.users.delete_all_ai_credentials",
            return_value=(True, None),
        ):
            retry = self.client.delete(
                "/api/auth/account",
                headers=headers,
                json={"password": "password123"},
            )
        self.assertEqual(retry.status_code, 200)
        self.assertNotIn("user@example.com", db_state.auth_users_memory)

    def test_stale_account_deletion_is_resumed_automatically(self):
        self.register()
        user = db_state.auth_users_memory["user@example.com"]
        user[ACCOUNT_DELETION_FIELD] = True
        user[ACCOUNT_DELETION_TOKEN_FIELD] = "stale-token"
        user[ACCOUNT_DELETION_STARTED_AT_FIELD] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )

        with patch(
            "services.firebase.users.delete_all_ai_credentials",
            return_value=(True, None),
        ):
            self.assertIsNone(get_user_record("user@example.com"))

        self.assertNotIn("user@example.com", db_state.auth_users_memory)

    @patch(
        "services.firebase.openai_credentials.delete_ai_credential_for_account_deletion",
        return_value=(True, None),
    )
    def test_all_provider_credentials_are_deleted(self, delete_mock):
        deleted, error = delete_all_ai_credentials(
            "user@example.com",
            "account-id",
            "deletion-token",
        )

        self.assertTrue(deleted)
        self.assertIsNone(error)
        self.assertEqual(
            delete_mock.call_args_list,
            [
                call(
                    "user@example.com",
                    "openai",
                    "account-id",
                    "deletion-token",
                ),
                call(
                    "user@example.com",
                    "mistral",
                    "account-id",
                    "deletion-token",
                ),
                call(
                    "user@example.com",
                    "anthropic",
                    "account-id",
                    "deletion-token",
                ),
            ],
        )


class CredentialPersistenceRaceTests(unittest.TestCase):
    def test_slow_put_cannot_write_after_account_is_tombstoned(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {ACCOUNT_DELETION_FIELD: True},
        )
        credential_ref = Mock()
        transaction = Mock()

        saved, error, status = _save_ai_credential_in_transaction(
            transaction,
            user_ref,
            credential_ref,
            "old-account",
            {"ciphertext": "encrypted", "created_at": "new"},
        )

        self.assertFalse(saved)
        self.assertEqual(error, "account_deleting")
        self.assertIsNone(status)
        credential_ref.get.assert_not_called()
        transaction.set.assert_not_called()

    def test_credential_write_requires_an_existing_parent_account(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=False,
            to_dict=lambda: {},
        )
        credential_ref = Mock()
        transaction = Mock()

        saved, error, status = _save_ai_credential_in_transaction(
            transaction,
            user_ref,
            credential_ref,
            "old-account",
            {"ciphertext": "encrypted", "created_at": "new"},
        )

        self.assertFalse(saved)
        self.assertEqual(error, "account_not_found")
        self.assertIsNone(status)
        credential_ref.get.assert_not_called()
        transaction.set.assert_not_called()

    def test_old_account_generation_cannot_write_to_recreated_parent(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        credential_ref = Mock()
        transaction = Mock()

        saved, error, status = _save_ai_credential_in_transaction(
            transaction,
            user_ref,
            credential_ref,
            "old-account",
            {"ciphertext": "encrypted", "created_at": "new"},
        )

        self.assertFalse(saved)
        self.assertEqual(error, "account_mismatch")
        self.assertIsNone(status)
        credential_ref.get.assert_not_called()
        transaction.set.assert_not_called()

    def test_slow_cleanup_cannot_delete_recreated_account_credential(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        credential_ref = Mock()
        transaction = Mock()

        deleted, error = (
            _delete_credential_for_account_deletion_in_transaction(
                transaction,
                user_ref,
                credential_ref,
                "old-account",
                "old-deletion-token",
            )
        )

        self.assertFalse(deleted)
        self.assertEqual(error, "account_mismatch")
        transaction.delete.assert_not_called()

    def test_old_account_generation_cannot_read_recreated_credential(self):
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True,
            to_dict=lambda: {"account_id": "new-account"},
        )
        credential_ref = Mock()
        transaction = Mock()

        ok, error, credential = _get_ai_credential_in_transaction(
            transaction,
            user_ref,
            credential_ref,
            "old-account",
        )

        self.assertFalse(ok)
        self.assertEqual(error, "account_mismatch")
        self.assertIsNone(credential)
        credential_ref.get.assert_not_called()


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

    @patch.dict(
        os.environ,
        {"AI_KMS_KEY_NAME": "projects/test/locations/europe-west1/keyRings/janus/cryptoKeys/users"},
    )
    @patch("services.credential_service.kms.KeyManagementServiceClient")
    def test_v2_aad_is_bound_to_user_and_provider(self, client_class):
        client = client_class.return_value
        client.encrypt.return_value = SimpleNamespace(ciphertext=b"encrypted-key")
        client.decrypt.return_value = SimpleNamespace(plaintext=USER_KEY.encode("utf-8"))

        ciphertext = encrypt_api_key(
            USER_KEY, "User@Example.com", provider="mistral", aad_version=2
        )
        plaintext = decrypt_api_key(
            ciphertext,
            "user@example.com",
            provider="mistral",
            aad_version=2,
        )

        self.assertEqual(plaintext, USER_KEY)
        expected_aad = b"janus-gate:api-key:v2:user@example.com:mistral"
        self.assertEqual(
            client.encrypt.call_args.kwargs["request"]["additional_authenticated_data"],
            expected_aad,
        )
        self.assertEqual(
            client.decrypt.call_args.kwargs["request"]["additional_authenticated_data"],
            expected_aad,
        )

    @patch.dict(
        os.environ,
        {"AI_KMS_KEY_NAME": "projects/test/locations/europe-west1/keyRings/janus/cryptoKeys/users"},
    )
    @patch("services.credential_service.kms.KeyManagementServiceClient")
    def test_v2_aad_supports_anthropic_provider(self, client_class):
        client = client_class.return_value
        client.encrypt.return_value = SimpleNamespace(ciphertext=b"encrypted-key")
        client.decrypt.return_value = SimpleNamespace(plaintext=USER_KEY.encode("utf-8"))

        ciphertext = encrypt_api_key(
            USER_KEY,
            "User@Example.com",
            provider="anthropic",
            aad_version=2,
        )
        plaintext = decrypt_api_key(
            ciphertext,
            "user@example.com",
            provider="anthropic",
            aad_version=2,
        )

        self.assertEqual(plaintext, USER_KEY)
        expected_aad = b"janus-gate:api-key:v2:user@example.com:anthropic"
        self.assertEqual(
            client.encrypt.call_args.kwargs["request"]["additional_authenticated_data"],
            expected_aad,
        )
        self.assertEqual(
            client.decrypt.call_args.kwargs["request"]["additional_authenticated_data"],
            expected_aad,
        )


if __name__ == "__main__":
    unittest.main()
