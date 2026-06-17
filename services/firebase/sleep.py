import uuid
from datetime import datetime, time

from . import db_state
from .core import normalize_user_email, user_exists
from ..logging_service import logger

SLEEP_NOTE_MAX_LEN = 500
SLEEP_MAX_ITEMS = 1000


def _normalize_sleep_entry(entry):
    """Normalize a single sleep entry."""
    if not isinstance(entry, dict):
        return None
    
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not db_state.TODO_ID_RE.match(entry_id):
        return None
    
    date = entry.get("date")
    if not isinstance(date, str) or not db_state.DATE_RE.match(date):
        return None
    
    bedtime = entry.get("bedtime")
    if bedtime is not None:
        if not isinstance(bedtime, str):
            return None
        try:
            time.fromisoformat(bedtime)
        except ValueError:
            return None
    
    wakeup = entry.get("wakeup")
    if wakeup is not None:
        if not isinstance(wakeup, str):
            return None
        try:
            time.fromisoformat(wakeup)
        except ValueError:
            return None
    
    quality = entry.get("quality")
    if quality is not None:
        try:
            quality = int(quality)
            if quality < 1 or quality > 5:
                return None
        except (ValueError, TypeError):
            return None
    
    duration_minutes = entry.get("durationMinutes")
    if duration_minutes is not None:
        try:
            duration_minutes = int(duration_minutes)
            if duration_minutes < 0 or duration_minutes > 1440:  # Max 24 hours
                return None
        except (ValueError, TypeError):
            return None
    
    notes = entry.get("notes", "")
    if not isinstance(notes, str):
        notes = ""
    notes = notes.strip()[:SLEEP_NOTE_MAX_LEN]
    
    created_at = entry.get("createdAt")
    if created_at is not None:
        if not isinstance(created_at, str):
            return None
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    
    return {
        "id": entry_id,
        "date": date,
        "bedtime": bedtime,
        "wakeup": wakeup,
        "quality": quality,
        "durationMinutes": duration_minutes,
        "notes": notes,
        "createdAt": created_at,
    }


def _normalize_sleep_list(raw):
    """Normalize a list of sleep entries."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        normalized = _normalize_sleep_entry(item)
        if normalized:
            out.append(normalized)
    return out


def get_sleep_entries(email):
    """Get all sleep entries for a user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return []
    
    if db_state.users_collection_ref:
        try:
            doc = db_state.users_collection_ref.document(email_key).get()
            if doc.exists:
                data = doc.to_dict() or {}
                return _normalize_sleep_list(data.get("sleep_v1"))
        except Exception as e:
            logger.error("Firestore sleep read failed", extra={
                "operation": "get_sleep_entries",
                "error": str(e),
            })
    
    return list(db_state.sleep_memory.get(email_key, []))


def _write_sleep_list(email_key, sleep_list):
    """Write sleep list to Firestore or memory."""
    if db_state.users_collection_ref:
        try:
            doc_ref = db_state.users_collection_ref.document(email_key)
            if not doc_ref.get().exists:
                return False
            doc_ref.set({"sleep_v1": sleep_list}, merge=True)
            return True
        except Exception as e:
            logger.error("Firestore sleep write failed", extra={
                "operation": "_write_sleep_list",
                "error": str(e),
            })
            return False
    
    if email_key not in db_state.auth_users_memory:
        return False
    db_state.sleep_memory[email_key] = list(sleep_list)
    return True


def add_sleep_entry(email, date, bedtime=None, wakeup=None, quality=None, duration_minutes=None, notes=""):
    """Add a new sleep entry."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(date, str) or not db_state.DATE_RE.match(date):
        return False, "invalid_date", None
    
    if bedtime is not None:
        if not isinstance(bedtime, str):
            return False, "invalid_bedtime", None
        try:
            time.fromisoformat(bedtime)
        except ValueError:
            return False, "invalid_bedtime", None
    
    if wakeup is not None:
        if not isinstance(wakeup, str):
            return False, "invalid_wakeup", None
        try:
            time.fromisoformat(wakeup)
        except ValueError:
            return False, "invalid_wakeup", None
    
    if quality is not None:
        try:
            quality = int(quality)
            if quality < 1 or quality > 5:
                return False, "invalid_quality", None
        except (ValueError, TypeError):
            return False, "invalid_quality", None
    
    if duration_minutes is not None:
        try:
            duration_minutes = int(duration_minutes)
            if duration_minutes < 0 or duration_minutes > 1440:
                return False, "invalid_duration", None
        except (ValueError, TypeError):
            return False, "invalid_duration", None
    
    if not isinstance(notes, str):
        return False, "invalid_notes", None
    notes = notes.strip()[:SLEEP_NOTE_MAX_LEN]
    
    entries = get_sleep_entries(email)
    if len(entries) >= SLEEP_MAX_ITEMS:
        return False, "too_many_entries", None
    
    now = datetime.utcnow().isoformat()
    new_entry = {
        "id": uuid.uuid4().hex,
        "date": date,
        "bedtime": bedtime,
        "wakeup": wakeup,
        "quality": quality,
        "durationMinutes": duration_minutes,
        "notes": notes,
        "createdAt": now,
    }
    
    entries.append(new_entry)
    if _write_sleep_list(email_key, entries):
        return True, None, entries
    return False, "write_failed", None


def update_sleep_entry(email, entry_id, updates):
    """Update an existing sleep entry."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(entry_id, str) or not db_state.TODO_ID_RE.match(entry_id):
        return False, "invalid_entry_id", None
    
    if not isinstance(updates, dict):
        return False, "invalid_updates", None
    
    entries = get_sleep_entries(email)
    entry_index = None
    for i, e in enumerate(entries):
        if e.get("id") == entry_id:
            entry_index = i
            break
    
    if entry_index is None:
        return False, "not_found", None
    
    updated_entry = dict(entries[entry_index])
    
    if "date" in updates:
        date = updates["date"]
        if not isinstance(date, str) or not db_state.DATE_RE.match(date):
            return False, "invalid_date", None
        updated_entry["date"] = date
    
    if "bedtime" in updates:
        bedtime = updates["bedtime"]
        if bedtime is not None:
            if not isinstance(bedtime, str):
                return False, "invalid_bedtime", None
            try:
                time.fromisoformat(bedtime)
            except ValueError:
                return False, "invalid_bedtime", None
        updated_entry["bedtime"] = bedtime
    
    if "wakeup" in updates:
        wakeup = updates["wakeup"]
        if wakeup is not None:
            if not isinstance(wakeup, str):
                return False, "invalid_wakeup", None
            try:
                time.fromisoformat(wakeup)
            except ValueError:
                return False, "invalid_wakeup", None
        updated_entry["wakeup"] = wakeup
    
    if "quality" in updates:
        quality = updates["quality"]
        if quality is not None:
            try:
                quality = int(quality)
                if quality < 1 or quality > 5:
                    return False, "invalid_quality", None
            except (ValueError, TypeError):
                return False, "invalid_quality", None
        updated_entry["quality"] = quality
    
    if "durationMinutes" in updates:
        duration_minutes = updates["durationMinutes"]
        if duration_minutes is not None:
            try:
                duration_minutes = int(duration_minutes)
                if duration_minutes < 0 or duration_minutes > 1440:
                    return False, "invalid_duration", None
            except (ValueError, TypeError):
                return False, "invalid_duration", None
        updated_entry["durationMinutes"] = duration_minutes
    
    if "notes" in updates:
        notes = updates["notes"]
        if notes is not None and not isinstance(notes, str):
            return False, "invalid_notes", None
        if notes:
            notes = notes.strip()[:SLEEP_NOTE_MAX_LEN]
        updated_entry["notes"] = notes
    
    entries[entry_index] = updated_entry
    
    if _write_sleep_list(email_key, entries):
        return True, None, entries
    return False, "write_failed", None


def delete_sleep_entry(email, entry_id):
    """Delete a sleep entry."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(entry_id, str) or not db_state.TODO_ID_RE.match(entry_id):
        return False, "invalid_entry_id", None
    
    entries = get_sleep_entries(email)
    next_entries = [e for e in entries if e.get("id") != entry_id]
    
    if len(next_entries) == len(entries):
        return False, "not_found", None
    
    if _write_sleep_list(email_key, next_entries):
        return True, None, next_entries
    return False, "write_failed", None
