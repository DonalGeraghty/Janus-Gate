import re
from threading import RLock

# Shared regex validators
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CELL_KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")
TODO_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

# Global database handles
db = None
users_collection_ref = None

# In-memory fallback stores
auth_users_memory = {}
nutrition_entries_memory = {}
workout_history_memory = {}
flashcards_memory = {}
flashcard_reviews_memory = {}
push_subscriptions_memory = {}
memory_lock = RLock()
