"""User-scoped nutrition entry persistence."""

from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from . import db_state
from .account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
    account_id_matches,
)
from .core import normalize_user_email
from ..logging_service import logger


ENTRY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _entries_collection(email_key):
    return db_state.users_collection_ref.document(email_key).collection("nutrition_entries")


def _serialize_entry(entry_id, data):
    eaten_at = data.get("eaten_at")
    created_at = data.get("created_at")
    return {
        "id": entry_id,
        "items": data.get("items", []),
        "total_calories": data.get("total_calories", 0),
        "total_protein_g": data.get("total_protein_g", 0),
        "eaten_at": eaten_at.isoformat() if isinstance(eaten_at, datetime) else eaten_at,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "updated_at": (
            data.get("updated_at").isoformat()
            if isinstance(data.get("updated_at"), datetime)
            else data.get("updated_at")
        ),
        "source_message": data.get("source_message"),
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


def _create_nutrition_entry_in_transaction(
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
    transaction.set(entry_ref, data)
    return True, None, data


def create_nutrition_entry(
    email,
    items,
    eaten_at,
    source_message=None,
    account_id=None,
):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None

    now = datetime.now(timezone.utc)
    data = {
        "items": items,
        "total_calories": sum(item["calories"] for item in items),
        "total_protein_g": round(sum(item["protein_g"] for item in items), 1),
        "eaten_at": eaten_at,
        "created_at": now,
        "source_message": source_message,
    }

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            doc_ref = _entries_collection(email_key).document()
            transaction = db_state.db.transaction()
            created, error, stored_data = firestore.transactional(
                _create_nutrition_entry_in_transaction
            )(
                transaction,
                user_ref,
                doc_ref,
                account_id,
                data,
            )
            if not created:
                return False, error, None
            return True, None, _serialize_entry(doc_ref.id, stored_data)
        except Exception as error:
            logger.error("Firestore nutrition create failed", extra={
                "operation": "create_nutrition_entry",
                "error": str(error),
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

        entry_id = uuid4().hex
        db_state.nutrition_entries_memory.setdefault(
            email_key,
            {},
        )[entry_id] = data
        return True, None, _serialize_entry(entry_id, data)


def _list_nutrition_entries_in_transaction(
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
    entries = [
        _serialize_entry(document.id, document.to_dict() or {})
        for document in documents
    ]
    return True, None, entries


def list_nutrition_entries(
    email,
    date_value=None,
    limit=50,
    account_id=None,
    start_value=None,
    end_value=None,
):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None

    start = start_value
    end = end_value
    if date_value:
        start = datetime.combine(date_value, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            query = _entries_collection(email_key)
            if start:
                query = query.where(filter=FieldFilter("eaten_at", ">=", start))
                query = query.where(filter=FieldFilter("eaten_at", "<", end))
            query = query.order_by("eaten_at", direction=firestore.Query.DESCENDING).limit(limit)
            transaction = db_state.db.transaction()
            return firestore.transactional(
                _list_nutrition_entries_in_transaction
            )(
                transaction,
                user_ref,
                query,
                account_id,
            )
        except Exception as error:
            logger.error("Firestore nutrition list failed", extra={
                "operation": "list_nutrition_entries",
                "error": str(error),
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

        rows = db_state.nutrition_entries_memory.get(email_key, {})
        entries = []
        for entry_id, data in rows.items():
            eaten_at = data["eaten_at"]
            if start and not (start <= eaten_at < end):
                continue
            entries.append(_serialize_entry(entry_id, data))
        entries.sort(key=lambda row: row["eaten_at"], reverse=True)
        return True, None, entries[:limit]


def _delete_nutrition_entry_in_transaction(
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


def delete_nutrition_entry(email, entry_id, account_id=None):
    """Delete one entry belonging to the authenticated user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
        return False, "invalid_entry_id"

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            doc_ref = _entries_collection(email_key).document(entry_id)
            transaction = db_state.db.transaction()
            return firestore.transactional(
                _delete_nutrition_entry_in_transaction
            )(
                transaction,
                user_ref,
                doc_ref,
                account_id,
            )
        except Exception as error:
            logger.error("Firestore nutrition delete failed", extra={
                "operation": "delete_nutrition_entry",
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

        entries = db_state.nutrition_entries_memory.get(email_key, {})
        if entry_id not in entries:
            return False, "not_found"
        del entries[entry_id]
        if not entries:
            db_state.nutrition_entries_memory.pop(email_key, None)
        return True, None


def _update_nutrition_entry_in_transaction(
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
    if not existing.exists:
        return False, "not_found", None
    existing_data = existing.to_dict() or {}
    stored_data = {
        **data,
        "created_at": existing_data.get("created_at", data["created_at"]),
    }
    transaction.set(entry_ref, stored_data)
    return True, None, stored_data


def update_nutrition_entry(
    email,
    entry_id,
    items,
    eaten_at,
    source_message=None,
    account_id=None,
):
    """Replace one user-owned entry and recalculate its totals."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
        return False, "invalid_entry_id", None

    now = datetime.now(timezone.utc)
    data = {
        "items": items,
        "total_calories": sum(item["calories"] for item in items),
        "total_protein_g": round(sum(item["protein_g"] for item in items), 1),
        "eaten_at": eaten_at,
        "created_at": now,
        "updated_at": now,
        "source_message": source_message,
    }

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            doc_ref = _entries_collection(email_key).document(entry_id)
            transaction = db_state.db.transaction()
            updated, error, stored_data = firestore.transactional(
                _update_nutrition_entry_in_transaction
            )(
                transaction,
                user_ref,
                doc_ref,
                account_id,
                data,
            )
            if not updated:
                return False, error, None
            return True, None, _serialize_entry(entry_id, stored_data)
        except Exception as error:
            logger.error("Firestore nutrition update failed", extra={
                "operation": "update_nutrition_entry",
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

        entries = db_state.nutrition_entries_memory.get(email_key, {})
        existing_data = entries.get(entry_id)
        if not existing_data:
            return False, "not_found", None
        data = {
            **data,
            "created_at": existing_data.get("created_at", now),
        }
        entries[entry_id] = data
        return True, None, _serialize_entry(entry_id, data)


def _delete_nutrition_batch_in_transaction(
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


def delete_nutrition_entries(
    email,
    expected_account_id,
    deletion_token,
):
    """Delete every nutrition entry for a user before deleting the user document."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False

    if db_state.users_collection_ref:
        if db_state.db is None:
            return False
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            docs = list(_entries_collection(email_key).stream())
            for offset in range(0, len(docs), 400):
                document_refs = [
                    document.reference
                    for document in docs[offset:offset + 400]
                ]
                transaction = db_state.db.transaction()
                deleted, _ = firestore.transactional(
                    _delete_nutrition_batch_in_transaction
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
            logger.error("Firestore nutrition delete failed", extra={
                "operation": "delete_nutrition_entries",
                "error": str(error),
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
            db_state.nutrition_entries_memory.pop(email_key, None)
    return True
