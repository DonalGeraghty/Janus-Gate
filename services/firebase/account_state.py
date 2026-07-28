"""Shared account-generation and deletion-state helpers."""


ACCOUNT_ID_FIELD = "account_id"
ACCOUNT_DELETION_FIELD = "account_deletion_in_progress"
ACCOUNT_DELETION_TOKEN_FIELD = "account_deletion_token"
ACCOUNT_DELETION_STARTED_AT_FIELD = "account_deletion_started_at"


def account_id_matches(expected_account_id, user_data):
    """Match claimed and stored generations, including pre-migration accounts."""
    stored_account_id = (user_data or {}).get(ACCOUNT_ID_FIELD)
    if expected_account_id is None:
        return stored_account_id is None
    return stored_account_id == expected_account_id
