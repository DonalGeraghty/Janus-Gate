"""VAPID configuration and delivery for generic nutrition reminders."""

import json
import os

from pywebpush import WebPushException, webpush

from .firebase_service import (
    claim_push_reminder,
    delete_push_subscription,
    list_due_push_reminders,
    mark_push_reminder_sent,
    release_push_reminder_claim,
)
from .logging_service import logger


def public_push_configuration():
    public_key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("VAPID_SUBJECT", "").strip()
    return {
        "configured": bool(public_key and private_key and subject),
        "vapid_public_key": public_key,
    }


def _send(subscription):
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("VAPID_SUBJECT", "").strip()
    payload = json.dumps({
        "title": "Nyx reminder",
        "body": "Take a moment to update your nutrition log.",
        "tag": "nyx-daily-reminder",
        "url": "/",
    })
    webpush(
        subscription_info={
            "endpoint": subscription["endpoint"],
            "keys": subscription["keys"],
        },
        data=payload,
        vapid_private_key=private_key,
        vapid_claims={"sub": subject},
        ttl=3600,
    )


def dispatch_due_reminders():
    if not public_push_configuration()["configured"]:
        return {"status": "not_configured", "users": 0, "sent": 0, "failed": 0}

    users = 0
    sent = 0
    failed = 0
    for candidate in list_due_push_reminders():
        if not claim_push_reminder(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ):
            continue
        users += 1
        user_sent = False
        for subscription in candidate["subscriptions"]:
            try:
                _send(subscription)
                sent += 1
                user_sent = True
            except WebPushException as error:
                failed += 1
                status_code = getattr(error.response, "status_code", None)
                if status_code in (404, 410):
                    delete_push_subscription(
                        candidate["email"],
                        candidate["account_id"],
                        subscription["endpoint"],
                    )
                logger.warning("Web Push delivery failed", extra={
                    "operation": "dispatch_due_reminders",
                    "status_code": status_code,
                })
            except Exception as error:
                failed += 1
                logger.error("Web Push delivery failed", extra={
                    "operation": "dispatch_due_reminders",
                    "error": type(error).__name__,
                })
        if user_sent:
            mark_push_reminder_sent(
                candidate["email"],
                candidate["account_id"],
                candidate["local_date"],
            )
        else:
            release_push_reminder_claim(
                candidate["email"],
                candidate["account_id"],
                candidate["local_date"],
            )
    return {"status": "success", "users": users, "sent": sent, "failed": failed}
