"""User-scoped Minerva flashcard persistence and review logging."""

from datetime import datetime, timezone
import re
from uuid import uuid4

from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from core.flashcard_service import schedule_review
from . import db_state
from .account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
    account_id_matches,
)
from .core import normalize_user_email
from ..logging_service import logger


CARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _cards_collection(email_key):
    return db_state.users_collection_ref.document(email_key).collection("flashcards")


def _reviews_collection(email_key):
    return db_state.users_collection_ref.document(email_key).collection("flashcard_reviews")


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _serialize_card(card_id, data):
    return {
        "id": card_id,
        "front": data.get("front", ""),
        "back": data.get("back", ""),
        "tags": data.get("tags", []),
        "source_message": data.get("source_message"),
        "status": data.get("status", "active"),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
        "due_at": _iso(data.get("due_at")),
        "last_reviewed_at": _iso(data.get("last_reviewed_at")),
        "review_count": int(data.get("review_count") or 0),
        "interval_days": int(data.get("interval_days") or 0),
        "ease_factor": float(data.get("ease_factor") or 2.5),
        "lapses": int(data.get("lapses") or 0),
        "client_request_id": data.get("client_request_id"),
    }


def _serialize_review(review_id, data):
    return {
        "id": review_id,
        "card_id": data.get("card_id"),
        "rating": data.get("rating"),
        "reviewed_at": _iso(data.get("reviewed_at")),
        "previous_due_at": _iso(data.get("previous_due_at")),
        "next_due_at": _iso(data.get("next_due_at")),
        "interval_days": int(data.get("interval_days") or 0),
        "client_request_id": data.get("client_request_id"),
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


def _memory_account_error(email_key, expected_account_id):
    user = db_state.auth_users_memory.get(email_key)
    if not user:
        return "account_not_found"
    if user.get(ACCOUNT_DELETION_FIELD):
        return "account_deleting"
    if not account_id_matches(expected_account_id, user):
        return "account_mismatch"
    return None


def _create_card_in_transaction(transaction, user_ref, card_ref, account_id, data):
    error = _live_account_error(user_ref.get(transaction=transaction), account_id)
    if error:
        return False, error, None
    existing = card_ref.get(transaction=transaction)
    if existing.exists:
        return True, None, existing.to_dict() or {}
    transaction.set(card_ref, data)
    return True, None, data


def create_flashcard(
    email,
    front,
    back,
    tags,
    source_message=None,
    account_id=None,
    client_request_id=None,
):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    now = datetime.now(timezone.utc)
    data = {
        "front": front,
        "back": back,
        "tags": tags,
        "source_message": source_message,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "due_at": now,
        "last_reviewed_at": None,
        "review_count": 0,
        "interval_days": 0,
        "ease_factor": 2.5,
        "lapses": 0,
        "client_request_id": client_request_id,
    }
    card_id = (
        f"request_{client_request_id.replace('-', '')}"
        if client_request_id
        else uuid4().hex
    )

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            card_ref = _cards_collection(email_key).document(card_id)
            transaction = db_state.db.transaction()
            created, error, stored = firestore.transactional(
                _create_card_in_transaction
            )(transaction, user_ref, card_ref, account_id, data)
            if not created:
                return False, error, None
            return True, None, _serialize_card(card_ref.id, stored)
        except Exception as error:
            logger.error("Firestore flashcard create failed", extra={
                "operation": "create_flashcard",
                "error": str(error),
            })
            return False, "database_error", None

    with db_state.memory_lock:
        error = _memory_account_error(email_key, account_id)
        if error:
            return False, error, None
        cards = db_state.flashcards_memory.setdefault(email_key, {})
        existing = cards.get(card_id)
        if existing:
            return True, None, _serialize_card(card_id, existing)
        cards[card_id] = data
        return True, None, _serialize_card(card_id, data)


def _list_cards_in_transaction(transaction, user_ref, query, account_id):
    error = _live_account_error(user_ref.get(transaction=transaction), account_id)
    if error:
        return False, error, None
    documents = list(transaction.get(query))
    return True, None, [
        _serialize_card(document.id, document.to_dict() or {})
        for document in documents
    ]


def _filter_and_sort(cards, tag, due_before, limit):
    tag = tag.strip().lower() if isinstance(tag, str) else None
    filtered = []
    for card in cards:
        if card.get("status") != "active":
            continue
        if tag and tag not in card.get("tags", []):
            continue
        if due_before:
            due = card.get("due_at")
            if isinstance(due, str):
                try:
                    due = datetime.fromisoformat(due.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if not isinstance(due, datetime) or due > due_before:
                continue
        filtered.append(card)
    key = "due_at" if due_before else "created_at"
    filtered.sort(key=lambda card: card.get(key) or "", reverse=not due_before)
    return filtered[:limit]


def list_flashcards(email, account_id=None, limit=200, tag=None, due_before=None):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    scan_limit = min(500, max(limit, 1) * 5)

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            query = _cards_collection(email_key).order_by(
                "created_at", direction=firestore.Query.DESCENDING
            ).limit(scan_limit)
            transaction = db_state.db.transaction()
            ok, error, cards = firestore.transactional(
                _list_cards_in_transaction
            )(transaction, user_ref, query, account_id)
            if not ok:
                return False, error, None
            return True, None, _filter_and_sort(cards, tag, due_before, limit)
        except Exception as error:
            logger.error("Firestore flashcard list failed", extra={
                "operation": "list_flashcards",
                "error": str(error),
            })
            return False, "database_error", None

    with db_state.memory_lock:
        error = _memory_account_error(email_key, account_id)
        if error:
            return False, error, None
        cards = [
            _serialize_card(card_id, data)
            for card_id, data in db_state.flashcards_memory.get(email_key, {}).items()
        ]
        return True, None, _filter_and_sort(cards, tag, due_before, limit)


def _update_card_in_transaction(transaction, user_ref, card_ref, account_id, changes):
    error = _live_account_error(user_ref.get(transaction=transaction), account_id)
    if error:
        return False, error, None
    document = card_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found", None
    stored = {**(document.to_dict() or {}), **changes}
    transaction.update(card_ref, changes)
    return True, None, stored


def update_flashcard(email, card_id, front, back, tags, account_id=None):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None
    if not isinstance(card_id, str) or not CARD_ID_PATTERN.fullmatch(card_id):
        return False, "invalid_card_id", None
    changes = {
        "front": front,
        "back": back,
        "tags": tags,
        "updated_at": datetime.now(timezone.utc),
    }

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            card_ref = _cards_collection(email_key).document(card_id)
            transaction = db_state.db.transaction()
            ok, error, stored = firestore.transactional(
                _update_card_in_transaction
            )(transaction, user_ref, card_ref, account_id, changes)
            return (ok, error, _serialize_card(card_id, stored) if stored else None)
        except Exception as error:
            logger.error("Firestore flashcard update failed", extra={
                "operation": "update_flashcard",
                "error": str(error),
            })
            return False, "database_error", None

    with db_state.memory_lock:
        error = _memory_account_error(email_key, account_id)
        if error:
            return False, error, None
        card = db_state.flashcards_memory.get(email_key, {}).get(card_id)
        if not card:
            return False, "not_found", None
        card.update(changes)
        return True, None, _serialize_card(card_id, card)


def _delete_card_in_transaction(transaction, user_ref, card_ref, account_id):
    error = _live_account_error(user_ref.get(transaction=transaction), account_id)
    if error:
        return False, error
    if not card_ref.get(transaction=transaction).exists:
        return False, "not_found"
    transaction.delete(card_ref)
    return True, None


def delete_flashcard(email, card_id, account_id=None):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    if not isinstance(card_id, str) or not CARD_ID_PATTERN.fullmatch(card_id):
        return False, "invalid_card_id"

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            card_ref = _cards_collection(email_key).document(card_id)
            transaction = db_state.db.transaction()
            ok, error = firestore.transactional(_delete_card_in_transaction)(
                transaction, user_ref, card_ref, account_id
            )
            if not ok:
                return False, error
            review_docs = list(_reviews_collection(email_key).where(
                filter=FieldFilter("card_id", "==", card_id)
            ).stream())
            for offset in range(0, len(review_docs), 400):
                batch = db_state.db.batch()
                for document in review_docs[offset:offset + 400]:
                    batch.delete(document.reference)
                batch.commit()
            return True, None
        except Exception as error:
            logger.error("Firestore flashcard delete failed", extra={
                "operation": "delete_flashcard",
                "error": str(error),
            })
            return False, "database_error"

    with db_state.memory_lock:
        error = _memory_account_error(email_key, account_id)
        if error:
            return False, error
        cards = db_state.flashcards_memory.get(email_key, {})
        if card_id not in cards:
            return False, "not_found"
        del cards[card_id]
        reviews = db_state.flashcard_reviews_memory.get(email_key, {})
        for review_id in [key for key, value in reviews.items() if value.get("card_id") == card_id]:
            del reviews[review_id]
        return True, None


def _review_card_in_transaction(
    transaction,
    user_ref,
    card_ref,
    review_ref,
    account_id,
    rating,
    client_request_id,
    now,
):
    error = _live_account_error(user_ref.get(transaction=transaction), account_id)
    if error:
        return False, error, None, None
    card_document = card_ref.get(transaction=transaction)
    if not card_document.exists:
        return False, "not_found", None, None
    card_data = card_document.to_dict() or {}
    existing_review = review_ref.get(transaction=transaction)
    if existing_review.exists:
        return True, None, card_data, existing_review.to_dict() or {}

    schedule = schedule_review(card_data, rating, now)
    changes = {**schedule, "updated_at": now}
    review_data = {
        "card_id": card_ref.id,
        "rating": rating,
        "reviewed_at": now,
        "previous_due_at": card_data.get("due_at"),
        "next_due_at": schedule["due_at"],
        "interval_days": schedule["interval_days"],
        "client_request_id": client_request_id,
    }
    transaction.update(card_ref, changes)
    transaction.set(review_ref, review_data)
    return True, None, {**card_data, **changes}, review_data


def review_flashcard(
    email,
    card_id,
    rating,
    account_id=None,
    client_request_id=None,
):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email", None, None
    if not isinstance(card_id, str) or not CARD_ID_PATTERN.fullmatch(card_id):
        return False, "invalid_card_id", None, None
    now = datetime.now(timezone.utc)
    review_id = (
        f"request_{client_request_id.replace('-', '')}"
        if client_request_id
        else uuid4().hex
    )

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            card_ref = _cards_collection(email_key).document(card_id)
            review_ref = _reviews_collection(email_key).document(review_id)
            transaction = db_state.db.transaction()
            ok, error, card, review = firestore.transactional(
                _review_card_in_transaction
            )(
                transaction,
                user_ref,
                card_ref,
                review_ref,
                account_id,
                rating,
                client_request_id,
                now,
            )
            return (
                ok,
                error,
                _serialize_card(card_id, card) if card else None,
                _serialize_review(review_id, review) if review else None,
            )
        except Exception as error:
            logger.error("Firestore flashcard review failed", extra={
                "operation": "review_flashcard",
                "error": str(error),
            })
            return False, "database_error", None, None

    with db_state.memory_lock:
        error = _memory_account_error(email_key, account_id)
        if error:
            return False, error, None, None
        card = db_state.flashcards_memory.get(email_key, {}).get(card_id)
        if not card:
            return False, "not_found", None, None
        reviews = db_state.flashcard_reviews_memory.setdefault(email_key, {})
        existing = reviews.get(review_id)
        if existing:
            return True, None, _serialize_card(card_id, card), _serialize_review(review_id, existing)
        schedule = schedule_review(card, rating, now)
        review = {
            "card_id": card_id,
            "rating": rating,
            "reviewed_at": now,
            "previous_due_at": card.get("due_at"),
            "next_due_at": schedule["due_at"],
            "interval_days": schedule["interval_days"],
            "client_request_id": client_request_id,
        }
        card.update({**schedule, "updated_at": now})
        reviews[review_id] = review
        return True, None, _serialize_card(card_id, card), _serialize_review(review_id, review)


def _delete_batch_in_transaction(
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


def delete_flashcards(email, expected_account_id, deletion_token):
    email_key = normalize_user_email(email)
    if not email_key:
        return False
    if db_state.users_collection_ref:
        if db_state.db is None:
            return False
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            for collection in (_cards_collection(email_key), _reviews_collection(email_key)):
                documents = list(collection.stream())
                for offset in range(0, len(documents), 400):
                    refs = [document.reference for document in documents[offset:offset + 400]]
                    transaction = db_state.db.transaction()
                    deleted, _ = firestore.transactional(_delete_batch_in_transaction)(
                        transaction,
                        user_ref,
                        refs,
                        expected_account_id,
                        deletion_token,
                    )
                    if not deleted:
                        return False
        except Exception as error:
            logger.error("Firestore flashcard account cleanup failed", extra={
                "operation": "delete_flashcards",
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
            db_state.flashcards_memory.pop(email_key, None)
            db_state.flashcard_reviews_memory.pop(email_key, None)
    return True
