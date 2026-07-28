"""Encrypt and decrypt user-provided API keys with Google Cloud KMS."""

import base64
import os

from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError
from google.cloud import kms


class CredentialConfigurationError(RuntimeError):
    pass


class CredentialEncryptionError(RuntimeError):
    pass


def _kms_key_name():
    key_name = os.environ.get("AI_KMS_KEY_NAME") or os.environ.get("OPENAI_KMS_KEY_NAME")
    if not key_name:
        raise CredentialConfigurationError("AI_KMS_KEY_NAME is not configured")
    return key_name


def _additional_authenticated_data(email, provider="openai", aad_version=1):
    email_key = email.strip().lower()
    if aad_version in (None, 1):
        if provider != "openai":
            raise ValueError("Legacy credential AAD is only valid for OpenAI")
        return f"janus-gate:openai-key:{email_key}".encode("utf-8")
    if aad_version == 2:
        return f"janus-gate:api-key:v2:{email_key}:{provider}".encode("utf-8")
    raise ValueError("Unsupported credential AAD version")


def encrypt_api_key(api_key, email, provider="openai", aad_version=1):
    try:
        response = kms.KeyManagementServiceClient().encrypt(
            request={
                "name": _kms_key_name(),
                "plaintext": api_key.encode("utf-8"),
                "additional_authenticated_data": _additional_authenticated_data(
                    email, provider, aad_version
                ),
            }
        )
    except (GoogleAPICallError, GoogleAuthError, ValueError) as error:
        raise CredentialEncryptionError("Could not encrypt API key") from error
    return base64.b64encode(response.ciphertext).decode("ascii")


def decrypt_api_key(ciphertext, email, provider="openai", aad_version=1):
    try:
        ciphertext_bytes = base64.b64decode(ciphertext, validate=True)
        response = kms.KeyManagementServiceClient().decrypt(
            request={
                "name": _kms_key_name(),
                "ciphertext": ciphertext_bytes,
                "additional_authenticated_data": _additional_authenticated_data(
                    email, provider, aad_version
                ),
            }
        )
        return response.plaintext.decode("utf-8")
    except (GoogleAPICallError, GoogleAuthError, ValueError, UnicodeDecodeError) as error:
        raise CredentialEncryptionError("Could not decrypt API key") from error
