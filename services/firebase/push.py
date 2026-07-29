"""Account-scoped Web Push settings and subscription persistence."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from firebase_admin import firestore

from . import db_state
from .account_state import (
    ACCOUNT_DELETION_FIELD,
    ACCOUNT_DELETION_TOKEN_FIELD,
    account_id_matches,
)
from .core import normalize_user_email
from ..logging_service import logger


ENABLED_FIELD = "push_reminder_enabled"
TIME_FIELD = "push_reminder_time"
TIMEZONE_FIELD = "push_reminder_timezone"
LAST_SENT_FIELD = "push_reminder_last_sent_date"
UPDATED_AT_FIELD = "push_reminder_updated_at"
CLAIM_DATE_FIELD = "push_reminder_claim_date"
CLAIM_AT_FIELD = "push_reminder_claimed_at"
CLAIM_TTL = timedelta(minutes=10)


def _subscription_id(endpoint):
    return sha256(endpoint.encode("utf-8")).hexdigest()


def _subscriptions_collection(email_key):
    return (
        db_state.users_collection_ref
        .document(email_key)
        .collection("push_subscriptions")
    )


def _settings_from_data(data):
    return {
        "enabled": bool(data.get(ENABLED_FIELD, False)),
        "local_time": data.get(TIME_FIELD) or "20:00",
        "timezone": data.get(TIMEZONE_FIELD) or "UTC",
    }


def get_push_settings(email, account_id):
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
            if not account_id_matches(account_id, data):
                return False, "account_mismatch", None
            return True, None, _settings_from_data(data)
        except Exception as error:
            logger.error("Push settings read failed", extra={
                "operation": "get_push_settings",
                "error": type(error).__name__,
            })
            return False, "database_error", None

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found", None
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting", None
        if not account_id_matches(account_id, user):
            return False, "account_mismatch", None
        return True, None, _settings_from_data(user)


def _save_settings_in_transaction(
    transaction,
    user_ref,
    expected_account_id,
    settings,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found"
    data = document.to_dict() or {}
    if data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting"
    if not account_id_matches(expected_account_id, data):
        return False, "account_mismatch"
    transaction.update(user_ref, {
        ENABLED_FIELD: settings["enabled"],
        TIME_FIELD: settings["local_time"],
        TIMEZONE_FIELD: settings["timezone"],
        UPDATED_AT_FIELD: firestore.SERVER_TIMESTAMP,
    })
    return True, None


def save_push_settings(email, account_id, settings):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            transaction = db_state.db.transaction()
            return firestore.transactional(_save_settings_in_transaction)(
                transaction,
                user_ref,
                account_id,
                settings,
            )
        except Exception as error:
            logger.error("Push settings write failed", extra={
                "operation": "save_push_settings",
                "error": type(error).__name__,
            })
            return False, "database_error"

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found"
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting"
        if not account_id_matches(account_id, user):
            return False, "account_mismatch"
        user.update({
            ENABLED_FIELD: settings["enabled"],
            TIME_FIELD: settings["local_time"],
            TIMEZONE_FIELD: settings["timezone"],
            UPDATED_AT_FIELD: datetime.now(timezone.utc),
        })
        return True, None


def _save_subscription_in_transaction(
    transaction,
    user_ref,
    subscription_ref,
    expected_account_id,
    subscription,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found"
    data = document.to_dict() or {}
    if data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting"
    if not account_id_matches(expected_account_id, data):
        return False, "account_mismatch"
    transaction.set(subscription_ref, {
        **subscription,
        "account_id": expected_account_id,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    return True, None


def save_push_subscription(email, account_id, subscription):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    subscription_id = _subscription_id(subscription["endpoint"])

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            subscription_ref = _subscriptions_collection(email_key).document(
                subscription_id
            )
            transaction = db_state.db.transaction()
            return firestore.transactional(_save_subscription_in_transaction)(
                transaction,
                user_ref,
                subscription_ref,
                account_id,
                subscription,
            )
        except Exception as error:
            logger.error("Push subscription write failed", extra={
                "operation": "save_push_subscription",
                "error": type(error).__name__,
            })
            return False, "database_error"

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user:
            return False, "not_found"
        if user.get(ACCOUNT_DELETION_FIELD):
            return False, "account_deleting"
        if not account_id_matches(account_id, user):
            return False, "account_mismatch"
        db_state.push_subscriptions_memory.setdefault(email_key, {})[
            subscription_id
        ] = {
            **subscription,
            "account_id": account_id,
            "updated_at": datetime.now(timezone.utc),
        }
        return True, None


def _delete_subscription_in_transaction(
    transaction,
    user_ref,
    subscription_ref,
    expected_account_id,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False, "not_found"
    data = document.to_dict() or {}
    if data.get(ACCOUNT_DELETION_FIELD):
        return False, "account_deleting"
    if not account_id_matches(expected_account_id, data):
        return False, "account_mismatch"
    transaction.delete(subscription_ref)
    return True, None


def delete_push_subscription(email, account_id, endpoint):
    email_key = normalize_user_email(email)
    if not email_key:
        return False, "invalid_email"
    subscription_id = _subscription_id(endpoint)

    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            subscription_ref = _subscriptions_collection(email_key).document(
                subscription_id
            )
            transaction = db_state.db.transaction()
            return firestore.transactional(_delete_subscription_in_transaction)(
                transaction,
                user_ref,
                subscription_ref,
                account_id,
            )
        except Exception as error:
            logger.error("Push subscription delete failed", extra={
                "operation": "delete_push_subscription",
                "error": type(error).__name__,
            })
            return False, "database_error"

    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if not user or not account_id_matches(account_id, user):
            return False, "account_mismatch"
        subscriptions = db_state.push_subscriptions_memory.get(email_key, {})
        subscriptions.pop(subscription_id, None)
        if not subscriptions:
            db_state.push_subscriptions_memory.pop(email_key, None)
        return True, None


def list_due_push_reminders(now=None):
    now = now or datetime.now(timezone.utc)
    candidates = []
    if db_state.users_collection_ref:
        try:
            documents = db_state.users_collection_ref.where(
                ENABLED_FIELD, "==", True
            ).stream()
            rows = [
                (document.id, document.to_dict() or {})
                for document in documents
            ]
        except Exception as error:
            logger.error("Push reminder scan failed", extra={
                "operation": "list_due_push_reminders",
                "error": type(error).__name__,
            })
            return []
    else:
        with db_state.memory_lock:
            rows = [
                (email, dict(data))
                for email, data in db_state.auth_users_memory.items()
                if data.get(ENABLED_FIELD)
            ]

    for email_key, data in rows:
        if data.get(ACCOUNT_DELETION_FIELD):
            continue
        try:
            local_now = now.astimezone(ZoneInfo(data.get(TIMEZONE_FIELD) or "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        local_date = local_now.date().isoformat()
        local_time = local_now.strftime("%H:%M")
        if local_time < (data.get(TIME_FIELD) or "20:00"):
            continue
        if data.get(LAST_SENT_FIELD) == local_date:
            continue
        account_id = data.get("account_id")
        if not account_id:
            continue
        if db_state.users_collection_ref:
            subscriptions = [
                document.to_dict() or {}
                for document in _subscriptions_collection(email_key).stream()
            ]
        else:
            with db_state.memory_lock:
                subscriptions = list(
                    db_state.push_subscriptions_memory
                    .get(email_key, {})
                    .values()
                )
        subscriptions = [
            item for item in subscriptions
            if item.get("account_id") == account_id
        ]
        if subscriptions:
            candidates.append({
                "email": email_key,
                "account_id": account_id,
                "local_date": local_date,
                "subscriptions": subscriptions,
            })
    return candidates


def _claim_reminder_in_transaction(
    transaction,
    user_ref,
    account_id,
    local_date,
    now,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False
    data = document.to_dict() or {}
    if (
        data.get(ACCOUNT_DELETION_FIELD)
        or not account_id_matches(account_id, data)
        or not data.get(ENABLED_FIELD)
        or data.get(LAST_SENT_FIELD) == local_date
    ):
        return False
    claimed_at = data.get(CLAIM_AT_FIELD)
    if (
        data.get(CLAIM_DATE_FIELD) == local_date
        and isinstance(claimed_at, datetime)
    ):
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        if now - claimed_at < CLAIM_TTL:
            return False
    transaction.update(user_ref, {
        CLAIM_DATE_FIELD: local_date,
        CLAIM_AT_FIELD: now,
    })
    return True


def claim_push_reminder(email, account_id, local_date):
    email_key = normalize_user_email(email)
    now = datetime.now(timezone.utc)
    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            transaction = db_state.db.transaction()
            return firestore.transactional(_claim_reminder_in_transaction)(
                transaction,
                user_ref,
                account_id,
                local_date,
                now,
            )
        except Exception:
            return False
    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if (
            not user
            or user.get(ACCOUNT_DELETION_FIELD)
            or not account_id_matches(account_id, user)
            or not user.get(ENABLED_FIELD)
            or user.get(LAST_SENT_FIELD) == local_date
        ):
            return False
        claimed_at = user.get(CLAIM_AT_FIELD)
        if (
            user.get(CLAIM_DATE_FIELD) == local_date
            and isinstance(claimed_at, datetime)
            and now - claimed_at < CLAIM_TTL
        ):
            return False
        user[CLAIM_DATE_FIELD] = local_date
        user[CLAIM_AT_FIELD] = now
        return True


def _finish_reminder_in_transaction(
    transaction,
    user_ref,
    account_id,
    local_date,
    sent,
):
    document = user_ref.get(transaction=transaction)
    if not document.exists:
        return False
    data = document.to_dict() or {}
    if (
        data.get(ACCOUNT_DELETION_FIELD)
        or not account_id_matches(account_id, data)
        or data.get(CLAIM_DATE_FIELD) != local_date
    ):
        return False
    updates = {
        CLAIM_DATE_FIELD: firestore.DELETE_FIELD,
        CLAIM_AT_FIELD: firestore.DELETE_FIELD,
    }
    if sent:
        updates[LAST_SENT_FIELD] = local_date
    transaction.update(user_ref, updates)
    return True


def _finish_push_reminder(email, account_id, local_date, sent):
    email_key = normalize_user_email(email)
    if db_state.users_collection_ref and db_state.db is not None:
        try:
            user_ref = db_state.users_collection_ref.document(email_key)
            transaction = db_state.db.transaction()
            return firestore.transactional(_finish_reminder_in_transaction)(
                transaction,
                user_ref,
                account_id,
                local_date,
                sent,
            )
        except Exception:
            return False
    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if (
            not user
            or not account_id_matches(account_id, user)
            or user.get(CLAIM_DATE_FIELD) != local_date
        ):
            return False
        if sent:
            user[LAST_SENT_FIELD] = local_date
        user.pop(CLAIM_DATE_FIELD, None)
        user.pop(CLAIM_AT_FIELD, None)
        return True


def mark_push_reminder_sent(email, account_id, local_date):
    return _finish_push_reminder(email, account_id, local_date, True)


def release_push_reminder_claim(email, account_id, local_date):
    return _finish_push_reminder(email, account_id, local_date, False)


def delete_push_data(email, expected_account_id, deletion_token):
    email_key = normalize_user_email(email)
    if not email_key:
        return False
    if db_state.users_collection_ref:
        if db_state.db is None:
            return False
        try:
            user_document = db_state.users_collection_ref.document(email_key).get()
            user_data = user_document.to_dict() or {} if user_document.exists else {}
            if user_document.exists and (
                not user_data.get(ACCOUNT_DELETION_FIELD)
                or not account_id_matches(expected_account_id, user_data)
                or user_data.get(ACCOUNT_DELETION_TOKEN_FIELD) != deletion_token
            ):
                return False
            for document in _subscriptions_collection(email_key).stream():
                document.reference.delete()
        except Exception as error:
            logger.error("Push deletion cleanup failed", extra={
                "operation": "delete_push_data",
                "error": type(error).__name__,
            })
            return False
    with db_state.memory_lock:
        user = db_state.auth_users_memory.get(email_key)
        if user and (
            not user.get(ACCOUNT_DELETION_FIELD)
            or not account_id_matches(expected_account_id, user)
            or user.get(ACCOUNT_DELETION_TOKEN_FIELD) != deletion_token
        ):
            return False
        db_state.push_subscriptions_memory.pop(email_key, None)
    return True
