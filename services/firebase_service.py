"""Public Firebase API for user management."""

from .firebase import (
    initialize_firebase,
    get_database_status,
    create_user_record,
    get_user_record,
    delete_user_account,
)
