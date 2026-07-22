"""User-scoped nutrition entry persistence."""

from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from . import db_state
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


def create_nutrition_entry(email, items, eaten_at, source_message=None):
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

    if db_state.users_collection_ref:
        try:
            doc_ref = _entries_collection(email_key).document()
            doc_ref.set(data)
            return True, None, _serialize_entry(doc_ref.id, data)
        except Exception as error:
            logger.error("Firestore nutrition create failed", extra={
                "operation": "create_nutrition_entry",
                "error": str(error),
            })
            return False, "database_error", None

    entry_id = uuid4().hex
    db_state.nutrition_entries_memory.setdefault(email_key, {})[entry_id] = data
    return True, None, _serialize_entry(entry_id, data)


def list_nutrition_entries(email, date_value=None, limit=50):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None

    start = None
    end = None
    if date_value:
        start = datetime.combine(date_value, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)

    if db_state.users_collection_ref:
        try:
            query = _entries_collection(email_key)
            if start:
                query = query.where(filter=FieldFilter("eaten_at", ">=", start))
                query = query.where(filter=FieldFilter("eaten_at", "<", end))
            query = query.order_by("eaten_at", direction=firestore.Query.DESCENDING).limit(limit)
            entries = [_serialize_entry(doc.id, doc.to_dict() or {}) for doc in query.stream()]
            return True, None, entries
        except Exception as error:
            logger.error("Firestore nutrition list failed", extra={
                "operation": "list_nutrition_entries",
                "error": str(error),
            })
            return False, "database_error", None

    rows = db_state.nutrition_entries_memory.get(email_key, {})
    entries = []
    for entry_id, data in rows.items():
        eaten_at = data["eaten_at"]
        if start and not (start <= eaten_at < end):
            continue
        entries.append(_serialize_entry(entry_id, data))
    entries.sort(key=lambda row: row["eaten_at"], reverse=True)
    return True, None, entries[:limit]


def delete_nutrition_entry(email, entry_id):
    """Delete one entry belonging to the authenticated user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
        return False, "invalid_entry_id"

    if db_state.users_collection_ref:
        try:
            doc_ref = _entries_collection(email_key).document(entry_id)
            if not doc_ref.get().exists:
                return False, "not_found"
            doc_ref.delete()
            return True, None
        except Exception as error:
            logger.error("Firestore nutrition delete failed", extra={
                "operation": "delete_nutrition_entry",
                "error": type(error).__name__,
            })
            return False, "database_error"

    entries = db_state.nutrition_entries_memory.get(email_key, {})
    if entry_id not in entries:
        return False, "not_found"
    del entries[entry_id]
    if not entries:
        db_state.nutrition_entries_memory.pop(email_key, None)
    return True, None


def update_nutrition_entry(email, entry_id, items, eaten_at, source_message=None):
    """Replace one user-owned entry and recalculate its totals."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
        return False, "invalid_entry_id", None

    now = datetime.now(timezone.utc)
    if db_state.users_collection_ref:
        try:
            doc_ref = _entries_collection(email_key).document(entry_id)
            existing = doc_ref.get()
            if not existing.exists:
                return False, "not_found", None
            existing_data = existing.to_dict() or {}
            data = {
                "items": items,
                "total_calories": sum(item["calories"] for item in items),
                "total_protein_g": round(sum(item["protein_g"] for item in items), 1),
                "eaten_at": eaten_at,
                "created_at": existing_data.get("created_at", now),
                "updated_at": now,
                "source_message": source_message,
            }
            doc_ref.set(data)
            return True, None, _serialize_entry(entry_id, data)
        except Exception as error:
            logger.error("Firestore nutrition update failed", extra={
                "operation": "update_nutrition_entry",
                "error": type(error).__name__,
            })
            return False, "database_error", None

    entries = db_state.nutrition_entries_memory.get(email_key, {})
    existing_data = entries.get(entry_id)
    if not existing_data:
        return False, "not_found", None
    data = {
        "items": items,
        "total_calories": sum(item["calories"] for item in items),
        "total_protein_g": round(sum(item["protein_g"] for item in items), 1),
        "eaten_at": eaten_at,
        "created_at": existing_data.get("created_at", now),
        "updated_at": now,
        "source_message": source_message,
    }
    entries[entry_id] = data
    return True, None, _serialize_entry(entry_id, data)


def delete_nutrition_entries(email):
    """Delete every nutrition entry for a user before deleting the user document."""
    email_key = normalize_user_email(email)
    if not email_key:
        return False

    if db_state.users_collection_ref:
        try:
            docs = list(_entries_collection(email_key).stream())
            for offset in range(0, len(docs), 400):
                batch = db_state.db.batch()
                for doc in docs[offset:offset + 400]:
                    batch.delete(doc.reference)
                batch.commit()
        except Exception as error:
            logger.error("Firestore nutrition delete failed", extra={
                "operation": "delete_nutrition_entries",
                "error": str(error),
            })
            return False

    db_state.nutrition_entries_memory.pop(email_key, None)
    return True
