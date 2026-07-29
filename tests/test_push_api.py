import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import app
from services.firebase import db_state
from services.firebase.push import (
    claim_push_reminder,
    list_due_push_reminders,
    mark_push_reminder_sent,
    release_push_reminder_claim,
)


PUSH_ENV = {
    "VAPID_PUBLIC_KEY": "public-test-key",
    "VAPID_PRIVATE_KEY": "private-test-key",
    "VAPID_SUBJECT": "mailto:test@example.com",
}
SUBSCRIPTION = {
    "endpoint": "https://push.example.test/subscription/123",
    "expirationTime": None,
    "keys": {
        "p256dh": "A" * 32,
        "auth": "B" * 16,
    },
}


class PushApiTests(unittest.TestCase):
    def setUp(self):
        db_state.db = None
        db_state.users_collection_ref = None
        db_state.auth_users_memory.clear()
        db_state.nutrition_entries_memory.clear()
        db_state.push_subscriptions_memory.clear()
        self.client = app.test_client()

    def register(self):
        response = self.client.post(
            "/api/auth/register",
            json={"email": "user@example.com", "password": "password123"},
        )
        return {
            "Authorization": f"Bearer {response.get_json()['token']}",
        }

    @patch.dict(os.environ, PUSH_ENV, clear=False)
    def test_settings_and_subscription_lifecycle(self):
        headers = self.register()

        response = self.client.get("/api/user/push-settings", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["settings"]["enabled"])
        self.assertTrue(response.get_json()["push"]["configured"])

        response = self.client.post(
            "/api/user/push-subscriptions",
            headers=headers,
            json=SUBSCRIPTION,
        )
        self.assertEqual(response.status_code, 201)

        response = self.client.put(
            "/api/user/push-settings",
            headers=headers,
            json={
                "enabled": True,
                "local_time": "19:30",
                "timezone": "Europe/Dublin",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = self.client.get(
            "/api/user/push-settings",
            headers=headers,
        ).get_json()
        self.assertEqual(body["settings"]["local_time"], "19:30")
        self.assertNotIn("subscriptions", body)
        self.assertNotIn("endpoint", str(body))

        response = self.client.delete(
            "/api/user/push-subscriptions",
            headers=headers,
            json={"endpoint": SUBSCRIPTION["endpoint"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db_state.push_subscriptions_memory, {})

    @patch.dict(os.environ, PUSH_ENV, clear=False)
    def test_due_scan_and_account_deletion_cleanup(self):
        headers = self.register()
        self.client.post(
            "/api/user/push-subscriptions",
            headers=headers,
            json=SUBSCRIPTION,
        )
        self.client.put(
            "/api/user/push-settings",
            headers=headers,
            json={
                "enabled": True,
                "local_time": "12:00",
                "timezone": "UTC",
            },
        )

        due = list_due_push_reminders(
            datetime(2026, 7, 29, 12, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["email"], "user@example.com")
        candidate = due[0]
        self.assertTrue(claim_push_reminder(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ))
        self.assertFalse(claim_push_reminder(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ))
        self.assertTrue(release_push_reminder_claim(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ))
        self.assertTrue(claim_push_reminder(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ))
        self.assertTrue(mark_push_reminder_sent(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ))
        self.assertFalse(claim_push_reminder(
            candidate["email"],
            candidate["account_id"],
            candidate["local_date"],
        ))

        deleted = self.client.delete(
            "/api/auth/account",
            headers=headers,
            json={"password": "password123"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(db_state.push_subscriptions_memory, {})

    def test_push_inputs_and_scheduler_secret_are_enforced(self):
        headers = self.register()
        response = self.client.post(
            "/api/user/push-subscriptions",
            headers=headers,
            json={"endpoint": "http://not-secure", "keys": {}},
        )
        self.assertEqual(response.status_code, 400)

        with patch.dict(os.environ, {"PUSH_CRON_SECRET": "cron-secret"}):
            self.assertEqual(
                self.client.post("/api/internal/push/reminders").status_code,
                401,
            )
            with patch(
                "app.dispatch_due_reminders",
                return_value={
                    "status": "success",
                    "users": 0,
                    "sent": 0,
                    "failed": 0,
                },
            ) as dispatch:
                response = self.client.post(
                    "/api/internal/push/reminders",
                    headers={"X-Cron-Secret": "cron-secret"},
                )
                self.assertEqual(response.status_code, 200)
                dispatch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
