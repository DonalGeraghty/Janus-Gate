from datetime import datetime, timedelta, timezone
from uuid import uuid4

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

from ..ai_catalog import (
    default_selection,
    is_supported_model,
    selection_from_user_record,
)
from . import db_state
from .account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_STARTED_AT_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
    ACCOUNT_ID_FIELD,
    account_id_matches,
)
from .core import normalize_user_email
from .nutrition import delete_nutrition_entries
from .flashcards import delete_flashcards
from .openai_credentials import delete_all_ai_credentials
from .push import delete_push_data
from .workouts import delete_workout_entries
from ..logging_service import logger


ACCOUNT_DELETION_STALE_AFTER = timedelta(minutes=10)


def _mark_account_deleting_in_transaction(
    transaction,
    user_ref,
    deletion_token,
    expected_account_id,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found", None

    data = document.to_dict() or {}
    if not account_id_matches(expected_account_id, data):
        return False, "account_mismatch", None
    if data.get(ACCOUNT_DELETION_FIELD):
        existing_token = data.get(ACCOUNT_DELETION_TOKEN_FIELD)
        if existing_token:
            return True, None, existing_token
        transaction.update(user_ref, {
            ACCOUNT_DELETION_TOKEN_FIELD: deletion_token,
            ACCOUNT_DELETION_STARTED_AT_FIELD: firestore.SERVER_TIMESTAMP,
        })
        return True, None, deletion_token

    transaction.update(user_ref, {
        ACCOUNT_DELETION_FIELD: True,
        ACCOUNT_DELETION_TOKEN_FIELD: deletion_token,
        ACCOUNT_DELETION_STARTED_AT_FIELD: firestore.SERVER_TIMESTAMP,
    })
    return True, None, deletion_token


def _delete_marked_account_in_transaction(
    transaction,
    user_ref,
    deletion_token,
    expected_account_id,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return True, None

    data = document.to_dict() or {}
    if (
        not data.get(ACCOUNT_DELETION_FIELD)
        or not account_id_matches(expected_account_id, data)
        or data.get(ACCOUNT_DELETION_TOKEN_FIELD) != deletion_token
    ):
        return False, "deletion_marker_changed"

    transaction.delete(user_ref)
    return True, None


def _run_account_transaction(callback, email_key, *args):
    if db_state.db is None or not db_state.users_collection_ref:
        return False, "database_unavailable"

    try:
        user_ref = db_state.users_collection_ref.document(email_key)
        transaction = db_state.db.transaction()
        return firestore.transactional(callback)(transaction, user_ref, *args)
    except Exception as error:
        logger.error("Firestore account deletion transaction failed", extra={
            "operation": callback.__name__,
            "error": type(error).__name__,
        })
        return False, "database_error"


def _mark_account_deleting(email_key, deletion_token, expected_account_id):
    result = _run_account_transaction(
        _mark_account_deleting_in_transaction,
        email_key,
        deletion_token,
        expected_account_id,
    )
    if len(result) == 2:
        success, error = result
        return success, error, None
    return result


def _delete_marked_account(
    email_key,
    deletion_token,
    expected_account_id,
):
    return _run_account_transaction(
        _delete_marked_account_in_transaction,
        email_key,
        deletion_token,
        expected_account_id,
    )


def _ensure_account_id_in_transaction(
    transaction,
    user_ref,
    candidate_account_id,
    expected_password_hash,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found", None

    data = document.to_dict() or {}
    if data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting", None
    if data.get("password_hash") != expected_password_hash:
        return False, "account_mismatch", None
    account_id = data.get(ACCOUNT_ID_FIELD)
    if account_id:
        return True, None, account_id

    transaction.update(user_ref, {ACCOUNT_ID_FIELD: candidate_account_id})
    return True, None, candidate_account_id


def ensure_user_account_id(email, expected_password_hash):
    """Atomically assign an account generation to a pre-migration user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None

    candidate_account_id = uuid4().hex
    if db_state.users_collection_ref:
        result = _run_account_transaction(
            _ensure_account_id_in_transaction,
            email_key,
            candidate_account_id,
            expected_password_hash,
        )
        if len(result) == 2:
            success, error = result
            return success, error, None
        return result

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found", None
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting", None
        if user.get("password_hash") != expected_password_hash:
            return False, "account_mismatch", None
        account_id = user.get(ACCOUNT_ID_FIELD)
        if not account_id:
            account_id = candidate_account_id
            user[ACCOUNT_ID_FIELD] = account_id
        return True, None, account_id


def create_user_record(email, password_hash, account_id):
    email_key = normalize_user_email(email)
    if not email_key or "@" not in email_key:
        return False, "invalid_email"
    if not isinstance(account_id, str) or not account_id:
        return False, "invalid_account_id"

    if db_state.users_collection_ref:
        try:
            selection = default_selection()
            doc_ref = db_state.users_collection_ref.document(email_key)
            doc_ref.create({
                "email": email_key,
                "password_hash": password_hash,
                ACCOUNT_ID_FIELD: account_id,
                "created_at": firestore.SERVER_TIMESTAMP,
                "ai_provider": selection["provider"],
                "ai_model": selection["model"],
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

    with db_state.memory_lock:
        if email_key in db_state.auth_users_memory:
            return False, "exists"
        selection = default_selection()
        db_state.auth_users_memory[email_key] = {
            "email": email_key,
            "password_hash": password_hash,
            ACCOUNT_ID_FIELD: account_id,
            "ai_provider": selection["provider"],
            "ai_model": selection["model"],
        }
        return True, None


def _load_user_data(email_key):
    if db_state.users_collection_ref:
        try:
            doc = db_state.users_collection_ref.document(email_key).get()
            if doc.exists:
                return doc.to_dict() or {}
        except Exception as e:
            logger.error("Firestore user read failed", extra={
                "operation": "get_user_record",
                "error": str(e),
            })

    with db_state.memory_lock:
        row = db_state.auth_users_memory.get(email_key)
        return dict(row) if row else None


def _auth_record(email_key, data):
    if not data:
        return None
    return {
        "email": data.get("email", email_key),
        "password_hash": data.get("password_hash"),
        ACCOUNT_ID_FIELD: data.get(ACCOUNT_ID_FIELD),
    }


def _deletion_marker_is_stale(data):
    started_at = data.get(ACCOUNT_DELETION_STARTED_AT_FIELD)
    if not isinstance(started_at, datetime):
        return True
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (
        datetime.now(timezone.utc) - started_at
        >= ACCOUNT_DELETION_STALE_AFTER
    )


def _resume_stale_account_deletion(email_key, data):
    if not _deletion_marker_is_stale(data):
        return
    logger.warning("Resuming stale account deletion", extra={
        "operation": "resume_account_deletion",
        "email": email_key,
    })
    deleted, error = delete_user_account(
        email_key,
        data.get(ACCOUNT_ID_FIELD),
    )
    if not deleted:
        logger.error("Stale account deletion retry failed", extra={
            "operation": "resume_account_deletion",
            "email": email_key,
            "error": error,
        })


def get_user_record(email):
    """Fetch a live user authentication record by normalized email."""
    email_key = normalize_user_email(email)
    if not email_key:
        return None

    data = _load_user_data(email_key)
    if data and data.get(ACCOUNT_DELETION_FIELD):
        _resume_stale_account_deletion(email_key, data)
        return None
    return _auth_record(email_key, data)


def get_user_record_for_account_deletion(email):
    """Fetch password data even while an authorized deletion is in progress."""
    email_key = normalize_user_email(email)
    if not email_key:
        return None
    return _auth_record(email_key, _load_user_data(email_key))


def get_ai_selection(email, expected_account_id):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None

    if db_state.users_collection_ref:
        try:
            document = db_state.users_collection_ref.document(email_key).get()
            if not document.exists:
                return False, "not_found", None
            data = document.to_dict() or {}
            if data.get(ACCOUNT_DELETION_FIELD):
                return False, "account_deleting", None
            if not account_id_matches(expected_account_id, data):
                return False, "account_mismatch", None
            return True, None, selection_from_user_record(data)
        except Exception as error:
            logger.error("Firestore AI settings read failed", extra={
                "operation": "get_ai_selection",
                "error": type(error).__name__,
            })
            return False, "database_error", None

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found", None
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting", None
        if not account_id_matches(expected_account_id, user):
            return False, "account_mismatch", None
        return True, None, selection_from_user_record(user)


def _save_ai_selection_in_transaction(
    transaction,
    user_ref,
    expected_account_id,
    selection,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found", None
    data = document.to_dict() or {}
    if data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting", None
    if not account_id_matches(expected_account_id, data):
        return False, "account_mismatch", None

    transaction.update(user_ref, {
        "ai_provider": selection["provider"],
        "ai_model": selection["model"],
        "ai_settings_updated_at": firestore.SERVER_TIMESTAMP,
    })
    return True, None, selection


def save_ai_selection(email, provider, model, expected_account_id):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    if not is_supported_model(provider, model):
        return False, "invalid_selection", None

    selection = {"provider": provider, "model": model}
    if db_state.users_collection_ref:
        result = _run_account_transaction(
            _save_ai_selection_in_transaction,
            email_key,
            expected_account_id,
            selection,
        )
        if len(result) == 2:
            success, error = result
            return success, error, None
        return result

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found", None
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting", None
        if not account_id_matches(expected_account_id, user):
            return False, "account_mismatch", None
        user.update({
            "ai_provider": provider,
            "ai_model": model,
        })
        return True, None, selection


def _clear_user_memory(email_key):
    """Remove the in-memory user record."""
    with db_state.memory_lock:
        db_state.auth_users_memory.pop(email_key, None)
        db_state.nutrition_entries_memory.pop(email_key, None)
        db_state.workout_history_memory.pop(email_key, None)
        db_state.flashcards_memory.pop(email_key, None)
        db_state.flashcard_reviews_memory.pop(email_key, None)
        db_state.push_subscriptions_memory.pop(email_key, None)


def _mark_memory_account_deleting(
    email_key,
    deletion_token,
    expected_account_id,
):
    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found", None
        if not account_id_matches(expected_account_id, user):
            return False, "account_mismatch", None
        if user.get(ACCOUNT_DELETION_FIELD):
            existing_token = user.get(ACCOUNT_DELETION_TOKEN_FIELD)
            if existing_token:
                return True, None, existing_token
        user[ACCOUNT_DELETION_FIELD] = True
        user[ACCOUNT_DELETION_TOKEN_FIELD] = deletion_token
        user[ACCOUNT_DELETION_STARTED_AT_FIELD] = datetime.now(timezone.utc)
        return True, None, deletion_token


def _delete_marked_memory_account(
    email_key,
    deletion_token,
    expected_account_id,
):
    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return True, None
        if (
            not user.get(ACCOUNT_DELETION_FIELD)
            or not account_id_matches(expected_account_id, user)
            or user.get(ACCOUNT_DELETION_TOKEN_FIELD) != deletion_token
        ):
            return False, "deletion_marker_changed"
        _clear_user_memory(email_key)
        return True, None


def delete_user_account(email, expected_account_id):
    """
    Delete the Firestore user document (all fields) and clear in-memory stores for that user.
    Returns (success, error_code).
    """
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"

    requested_deletion_token = uuid4().hex
    firestore_backed = db_state.users_collection_ref is not None
    if firestore_backed:
        marked, _, deletion_token = _mark_account_deleting(
            email_key,
            requested_deletion_token,
            expected_account_id,
        )
    else:
        marked, _, deletion_token = _mark_memory_account_deleting(
            email_key,
            requested_deletion_token,
            expected_account_id,
        )
    if not marked:
        return False, "delete_failed"

    credentials_deleted, _ = delete_all_ai_credentials(
        email_key,
        expected_account_id,
        deletion_token,
    )
    if not credentials_deleted:
        return False, "delete_failed"

    if not delete_push_data(
        email_key,
        expected_account_id,
        deletion_token,
    ):
        return False, "delete_failed"

    if not delete_nutrition_entries(
        email_key,
        expected_account_id,
        deletion_token,
    ):
        return False, "delete_failed"

    if not delete_workout_entries(
        email_key,
        expected_account_id,
        deletion_token,
    ):
        return False, "delete_failed"

    if not delete_flashcards(
        email_key,
        expected_account_id,
        deletion_token,
    ):
        return False, "delete_failed"

    if firestore_backed:
        deleted, error = _delete_marked_account(
            email_key,
            deletion_token,
            expected_account_id,
        )
        if deleted:
            logger.info("User account deleted from Firestore", extra={
                "operation": "delete_user_account",
                "email": email_key,
                "status": "success",
            })
        else:
            logger.error("Firestore user delete failed", extra={
                "operation": "delete_user_account",
                "email": email_key,
                "error": error,
            })
            return False, "delete_failed"
        _clear_user_memory(email_key)
    else:
        deleted, _ = _delete_marked_memory_account(
            email_key,
            deletion_token,
            expected_account_id,
        )
        if not deleted:
            return False, "delete_failed"
    return True, None
