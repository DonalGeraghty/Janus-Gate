"""User-scoped workout history persistence."""

from datetime import datetime, timezone
import re

from firebase_admin import firestore

from . import db_state
from .account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
    account_id_matches,
)
from .core import normalize_user_email
from ..logging_service import logger


ENTRY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _history_collection(email_key):
    return db_state.users_collection_ref.document(email_key).collection(
        "workout_history"
    )


def _isoformat(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _serialize_entry(entry_id, data):
    return {
        "id": entry_id,
        "workout_id": data.get("workout_id"),
        "title": data.get("title"),
        "day": data.get("day"),
        "finished_at": _isoformat(data.get("finished_at")),
        "duration_minutes": data.get("duration_minutes"),
        "completed": data.get("completed"),
        "total": data.get("total"),
        "entries": data.get("entries", {}),
        "note": data.get("note", ""),
        "source_message": data.get("source_message"),
        "created_at": _isoformat(data.get("created_at")),
        "updated_at": _isoformat(data.get("updated_at")),
    }


def _live_account_error(user_document, expected_account_id):
    if not user_document.exists:
        return "account_not_found"
    user_data = user_document.to_dict() or {}
    if user_data.get(ACCOUNT_DELETION_FIELD):
        return "account_deleting"
    if not account_id_matches(expected_account_id, user_data):
        return "account_mismatch"
    return None


def _save_workout_entry_in_transaction(
    transaction,
    user_ref,
    entry_ref,
    expected_account_id,
    data,
):
    error = _live_account_error(
        user_ref.get(transaction=transaction),
        expected_account_id,
    )
    if error:
        return False, error, None

    existing = entry_ref.get(transaction=transaction)
    existing_data = existing.to_dict() or {} if existing.exists else {}
    stored_data = {
        **data,
        "created_at": existing_data.get("created_at", data["created_at"]),
    }
    transaction.set(entry_ref, stored_data)
    return True, None, stored_data


def save_workout_entry(email, entry_id, workout, account_id=None):
    """Create or replace one workout entry owned by the authenticated user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
        return False, "invalid_entry_id", None

    now = datetime.now(timezone.utc)
    data = {
        **workout,
        "created_at": now,
        "updated_at": now,
    }

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            entry_ref = _history_collection(email_key).document(entry_id)
            transaction = db_state.db.transaction()
            saved, error, stored_data = firestore.transactional(
                _save_workout_entry_in_transaction
            )(
                transaction,
                user_ref,
                entry_ref,
                account_id,
                data,
            )
            if not saved:
                return False, error, None
            return True, None, _serialize_entry(entry_id, stored_data)
        except Exception as error:
            logger.error("Firestore workout save failed", extra={
                "operation": "save_workout_entry",
                "error": type(error).__name__,
            })
            return False, "database_error", None

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "account_not_found", None
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting", None
        if not account_id_matches(account_id, user):
            return False, "account_mismatch", None

        entries = db_state.workout_history_memory.setdefault(email_key, {})
        existing = entries.get(entry_id, {})
        data["created_at"] = existing.get("created_at", now)
        entries[entry_id] = data
        return True, None, _serialize_entry(entry_id, data)


def _list_workout_entries_in_transaction(
    transaction,
    user_ref,
    query,
    expected_account_id,
):
    error = _live_account_error(
        user_ref.get(transaction=transaction),
        expected_account_id,
    )
    if error:
        return False, error, None
    documents = list(transaction.get(query))
    return True, None, [
        _serialize_entry(document.id, document.to_dict() or {})
        for document in documents
    ]


def list_workout_entries(email, account_id=None):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            query = _history_collection(email_key).order_by(
                "finished_at",
                direction=firestore.Query.DESCENDING,
            )
            transaction = db_state.db.transaction()
            return firestore.transactional(
                _list_workout_entries_in_transaction
            )(
                transaction,
                user_ref,
                query,
                account_id,
            )
        except Exception as error:
            logger.error("Firestore workout list failed", extra={
                "operation": "list_workout_entries",
                "error": type(error).__name__,
            })
            return False, "database_error", None

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "account_not_found", None
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting", None
        if not account_id_matches(account_id, user):
            return False, "account_mismatch", None

        entries = [
            _serialize_entry(entry_id, data)
            for entry_id, data in db_state.workout_history_memory.get(
                email_key, {}
            ).items()
        ]
        entries.sort(key=lambda row: row["finished_at"], reverse=True)
        return True, None, entries


def _delete_workout_entry_in_transaction(
    transaction,
    user_ref,
    entry_ref,
    expected_account_id,
):
    error = _live_account_error(
        user_ref.get(transaction=transaction),
        expected_account_id,
    )
    if error:
        return False, error
    entry = entry_ref.get(transaction=transaction)
    if not entry.exists:
        return False, "not_found"
    transaction.delete(entry_ref)
    return True, None


def delete_workout_entry(email, entry_id, account_id=None):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
        return False, "invalid_entry_id"

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            entry_ref = _history_collection(email_key).document(entry_id)
            transaction = db_state.db.transaction()
            return firestore.transactional(
                _delete_workout_entry_in_transaction
            )(
                transaction,
                user_ref,
                entry_ref,
                account_id,
            )
        except Exception as error:
            logger.error("Firestore workout delete failed", extra={
                "operation": "delete_workout_entry",
                "error": type(error).__name__,
            })
            return False, "database_error"

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "account_not_found"
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting"
        if not account_id_matches(account_id, user):
            return False, "account_mismatch"

        entries = db_state.workout_history_memory.get(email_key, {})
        if entry_id not in entries:
            return False, "not_found"
        del entries[entry_id]
        if not entries:
            db_state.workout_history_memory.pop(email_key, None)
        return True, None


def _delete_workout_batch_in_transaction(
    transaction,
    user_ref,
    document_refs,
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
    for document_ref in document_refs:
        transaction.delete(document_ref)
    return True, None


def delete_workout_entries(email, expected_account_id, deletion_token):
    """Delete all workout history before removing the parent user document."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False

    if db_state.users_collection_ref:
        if db_state.db is None:
            return False
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            documents = list(_history_collection(email_key).stream())
            for offset in range(0, len(documents), 400):
                document_refs = [
                    document.reference
                    for document in documents[offset:offset + 400]
                ]
                transaction = db_state.db.transaction()
                deleted, _ = firestore.transactional(
                    _delete_workout_batch_in_transaction
                )(
                    transaction,
                    user_ref,
                    document_refs,
                    expected_account_id,
                    deletion_token,
                )
                if not deleted:
                    return False
        except Exception as error:
            logger.error("Firestore workout cleanup failed", extra={
                "operation": "delete_workout_entries",
                "error": type(error).__name__,
            })
            return False

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if user:
            if (
                not user.get(ACCOUNT_DELETION_FIELD)
                or not account_id_matches(expected_account_id, user)
                or user.get(ACCOUNT_DELETION_TOKEN_FIELD) != deletion_token
            ):
                return False
            db_state.workout_history_memory.pop(email_key, None)
    return True
