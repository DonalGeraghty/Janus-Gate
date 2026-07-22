from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

from . import db_state
from .core import normalize_user_email
from ..logging_service import logger


def create_user_record(email, password_hash):
    email_key = normalize_user_email(email)
    if not email_key or "@" not in email_key:
        return False, "invalid_email"

    if db_state.users_collection_ref:
        try:
            doc_ref = db_state.users_collection_ref.document(email_key)
            doc_ref.create({
                "email": email_key,
                "password_hash": password_hash,
                "created_at": firestore.SERVER_TIMESTAMP,
            })
            logger.info("User stored in Firestore", extra={
                "operation": "create_user_record",
                "email": email_key,
                "status": "success",
            })
            return True, None
        except AlreadyExists:
            return False, "exists"
        except Exception as e:
            logger.error("Firestore user create failed", extra={
                "operation": "create_user_record",
                "error": str(e),
                "status": "failed",
            })
            return False, "database_error"

    if email_key in db_state.auth_users_memory:
        return False, "exists"
    db_state.auth_users_memory[email_key] = {
        "email": email_key,
        "password_hash": password_hash,
    }
    return True, None


def get_user_record(email):
    email_key = normalize_user_email(email)
    if not email_key:
        return None

    if db_state.users_collection_ref:
        try:
            doc = db_state.users_collection_ref.document(email_key).get()
            if doc.exists:
                data = doc.to_dict() or {}
                return {
                    "email": data.get("email", email_key),
                    "password_hash": data.get("password_hash"),
                }
        except Exception as e:
            logger.error("Firestore user read failed", extra={
                "operation": "get_user_record",
                "error": str(e),
            })

    row = db_state.auth_users_memory.get(email_key)
    if row:
        return {
            "email": row["email"],
            "password_hash": row["password_hash"],
        }
    return None


def _clear_user_memory(email_key):
    """Remove the in-memory user record."""
    db_state.auth_users_memory.pop(email_key, None)


def delete_user_account(email):
    """
    Delete the Firestore user document (all fields) and clear in-memory stores for that user.
    Returns (success, error_code).
    """
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"

    if db_state.users_collection_ref:
        try:
            doc_ref = db_state.users_collection_ref.document(email_key)
            if doc_ref.get().exists:
                doc_ref.delete()
            logger.info("User account deleted from Firestore", extra={
                "operation": "delete_user_account",
                "email": email_key,
                "status": "success",
            })
        except Exception as e:
            logger.error("Firestore user delete failed", extra={
                "operation": "delete_user_account",
                "email": email_key,
                "error": str(e),
            })
            return False, "delete_failed"

    _clear_user_memory(email_key)
    return True, None
