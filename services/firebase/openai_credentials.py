"""Firestore persistence for provider-scoped, KMS-encrypted AI credentials."""

from datetime import datetime, timezone

from firebase_admin import firestore

from ..ai_catalog import SUPPORTED_PROVIDERS, is_supported_provider
from . import db_state
from .account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
    account_id_matches,
)
from .core import normalize_user_email
from ..logging_service import logger


def _credential_document(email_key, provider="openai"):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    return (
        db_state.users_collection_ref.document(email_key)
        .collection("private")
        .document(provider)
    )


def _serialize_status(data):
    def iso(value):
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "configured": True,
        "last_four": data.get("last_four"),
        "verified_at": iso(data.get("verified_at")),
        "updated_at": iso(data.get("updated_at")),
    }


def _save_ai_credential_in_transaction(
    transaction,
    user_ref,
    credential_ref,
    expected_account_id,
    data,
):
    """Conditionally save only while the owning account is live."""
    user_document = user_ref.get(transaction=transaction)
    if not user_document.exists:
        return False, "account_not_found", None

    user_data = user_document.to_dict() or {}
    if user_data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting", None
    if not account_id_matches(expected_account_id, user_data):
        return False, "account_mismatch", None

    existing = credential_ref.get(transaction=transaction)
    existing_data = (existing.to_dict() or {}) if existing.exists else {}
    stored_data = {
        **data,
        "created_at": existing_data.get("created_at", data["created_at"]),
    }
    transaction.set(credential_ref, stored_data)
    return True, None, stored_data


def save_ai_credential(
    email,
    provider,
    ciphertext,
    last_four,
    aad_version=2,
    account_id=None,
):
    email_key = normalize_user_email(email)
    if not is_supported_provider(provider):
        return False, "invalid_provider", None
    if (
        not email_key
        or not db_state.users_collection_ref
        or db_state.db is None
    ):
        return False, "database_unavailable", None

    try:
        now = datetime.now(timezone.utc)
        data = {
            "provider": provider,
            "ciphertext": ciphertext,
            "last_four": last_four,
            "encryption_version": 2 if aad_version == 2 else 1,
            "aad_version": aad_version,
            "created_at": now,
            "updated_at": now,
            "verified_at": now,
        }
        user_ref = db_state.users_collection_ref.document(email_key)
        credential_ref = _credential_document(email_key, provider)
        transaction = db_state.db.transaction()
        saved, error, stored_data = firestore.transactional(
            _save_ai_credential_in_transaction
        )(
            transaction,
            user_ref,
            credential_ref,
            account_id,
            data,
        )
        if not saved:
            return False, error, None
        return True, None, _serialize_status(stored_data)
    except Exception as error:
        logger.error("Firestore credential save failed", extra={
            "operation": "save_ai_credential",
            "provider": provider,
            "error": type(error).__name__,
        })
        return False, "database_error", None


def _get_ai_credential_in_transaction(
    transaction,
    user_ref,
    credential_ref,
    expected_account_id,
):
    user_document = user_ref.get(transaction=transaction)
    if not user_document.exists:
        return False, "account_not_found", None
    user_data = user_document.to_dict() or {}
    if user_data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting", None
    if not account_id_matches(expected_account_id, user_data):
        return False, "account_mismatch", None

    credential = credential_ref.get(transaction=transaction)
    return (
        True,
        None,
        (credential.to_dict() or {}) if credential.exists else None,
    )


def get_ai_credential(email, provider, account_id=None):
    email_key = normalize_user_email(email)
    if not is_supported_provider(provider):
        return False, "invalid_provider", None
    if (
        not email_key
        or not db_state.users_collection_ref
        or db_state.db is None
    ):
        return False, "database_unavailable", None

    try:
        user_ref = db_state.users_collection_ref.document(email_key)
        credential_ref = _credential_document(email_key, provider)
        transaction = db_state.db.transaction()
        return firestore.transactional(_get_ai_credential_in_transaction)(
            transaction,
            user_ref,
            credential_ref,
            account_id,
        )
    except Exception as error:
        logger.error("Firestore credential read failed", extra={
            "operation": "get_ai_credential",
            "provider": provider,
            "error": type(error).__name__,
        })
        return False, "database_error", None


def get_ai_credential_status(email, provider, account_id=None):
    ok, error, credential = get_ai_credential(email, provider, account_id)
    if not ok or credential is None:
        return ok, error, None
    return True, None, _serialize_status(credential)


def _delete_ai_credential_in_transaction(
    transaction,
    user_ref,
    credential_ref,
    expected_account_id,
):
    user_document = user_ref.get(transaction=transaction)
    if not user_document.exists:
        return False, "account_not_found"
    user_data = user_document.to_dict() or {}
    if user_data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting"
    if not account_id_matches(expected_account_id, user_data):
        return False, "account_mismatch"
    transaction.delete(credential_ref)
    return True, None


def delete_ai_credential(email, provider, account_id=None):
    email_key = normalize_user_email(email)
    if not is_supported_provider(provider):
        return False, "invalid_provider"
    if not email_key:
        return False, "invalid_email"
    if not db_state.users_collection_ref or db_state.db is None:
        return False, "database_unavailable"

    try:
        user_ref = db_state.users_collection_ref.document(email_key)
        credential_ref = _credential_document(email_key, provider)
        transaction = db_state.db.transaction()
        return firestore.transactional(_delete_ai_credential_in_transaction)(
            transaction,
            user_ref,
            credential_ref,
            account_id,
        )
    except Exception as error:
        logger.error("Firestore credential delete failed", extra={
            "operation": "delete_ai_credential",
            "provider": provider,
            "error": type(error).__name__,
        })
        return False, "database_error"


def _delete_credential_for_account_deletion_in_transaction(
    transaction,
    user_ref,
    credential_ref,
    expected_account_id,
    deletion_token,
):
    user_document = user_ref.get(transaction=transaction)
    if not user_document.exists:
        return True, None
    user_data = user_document.to_dict() or {}
    if (
        not user_data.get(ACCOUNT_DELETION_FIELD)
        or not account_id_matches(expected_account_id, user_data)
        or user_data.get(ACCOUNT_DELETION_TOKEN_FIELD) != deletion_token
    ):
        return False, "account_mismatch"
    transaction.delete(credential_ref)
    return True, None


def delete_ai_credential_for_account_deletion(
    email,
    provider,
    expected_account_id,
    deletion_token,
):
    """Idempotently remove a child credential while its parent is tombstoned."""
    email_key = normalize_user_email(email)
    if not is_supported_provider(provider):
        return False, "invalid_provider"
    if not email_key:
        return False, "invalid_email"
    if not db_state.users_collection_ref:
        return True, None
    if db_state.db is None:
        return False, "database_unavailable"

    try:
        user_ref = db_state.users_collection_ref.document(email_key)
        credential_ref = _credential_document(email_key, provider)
        transaction = db_state.db.transaction()
        return firestore.transactional(
            _delete_credential_for_account_deletion_in_transaction
        )(
            transaction,
            user_ref,
            credential_ref,
            expected_account_id,
            deletion_token,
        )
    except Exception as error:
        logger.error("Firestore credential cleanup failed", extra={
            "operation": "delete_ai_credential_for_account_deletion",
            "provider": provider,
            "error": type(error).__name__,
        })
        return False, "database_error"


def delete_all_ai_credentials(email, expected_account_id, deletion_token):
    for provider in SUPPORTED_PROVIDERS:
        deleted, error = delete_ai_credential_for_account_deletion(
            email,
            provider,
            expected_account_id,
            deletion_token,
        )
        if not deleted:
            return False, error
    return True, None


def save_openai_credential(email, ciphertext, last_four, account_id=None):
    """Compatibility wrapper for legacy callers that produce v1 OpenAI ciphertext."""
    return save_ai_credential(
        email,
        "openai",
        ciphertext,
        last_four,
        aad_version=1,
        account_id=account_id,
    )


def get_openai_credential(email, account_id=None):
    return get_ai_credential(email, "openai", account_id)


def get_openai_credential_status(email, account_id=None):
    return get_ai_credential_status(email, "openai", account_id)


def delete_openai_credential(
    email,
    expected_account_id=None,
    deletion_token=None,
):
    return delete_ai_credential_for_account_deletion(
        email,
        "openai",
        expected_account_id,
        deletion_token,
    )
