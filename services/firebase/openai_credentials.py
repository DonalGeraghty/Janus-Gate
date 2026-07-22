"""Firestore persistence for KMS-encrypted user OpenAI credentials."""

from datetime import datetime, timezone

from . import db_state
from .core import normalize_user_email
from ..logging_service import logger


def _credential_document(email_key):
    return (
        db_state.users_collection_ref.document(email_key)
        .collection("private")
        .document("openai")
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


def save_openai_credential(email, ciphertext, last_four):
    email_key = normalize_user_email(email)
    if not email_key or not db_state.users_collection_ref:
        return False, "database_unavailable", None

    try:
        doc_ref = _credential_document(email_key)
        existing = doc_ref.get()
        now = datetime.now(timezone.utc)
        existing_data = (existing.to_dict() or {}) if existing.exists else {}
        data = {
            "ciphertext": ciphertext,
            "last_four": last_four,
            "encryption_version": 1,
            "created_at": existing_data.get("created_at", now),
            "updated_at": now,
            "verified_at": now,
        }
        doc_ref.set(data)
        return True, None, _serialize_status(data)
    except Exception as error:
        logger.error("Firestore credential save failed", extra={
            "operation": "save_openai_credential",
            "error": type(error).__name__,
        })
        return False, "database_error", None


def get_openai_credential(email):
    email_key = normalize_user_email(email)
    if not email_key or not db_state.users_collection_ref:
        return False, "database_unavailable", None

    try:
        document = _credential_document(email_key).get()
        return True, None, (document.to_dict() or {}) if document.exists else None
    except Exception as error:
        logger.error("Firestore credential read failed", extra={
            "operation": "get_openai_credential",
            "error": type(error).__name__,
        })
        return False, "database_error", None


def get_openai_credential_status(email):
    ok, error, credential = get_openai_credential(email)
    if not ok or credential is None:
        return ok, error, None
    return True, None, _serialize_status(credential)


def delete_openai_credential(email):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    if not db_state.users_collection_ref:
        return True, None

    try:
        _credential_document(email_key).delete()
        return True, None
    except Exception as error:
        logger.error("Firestore credential delete failed", extra={
            "operation": "delete_openai_credential",
            "error": type(error).__name__,
        })
        return False, "database_error"
