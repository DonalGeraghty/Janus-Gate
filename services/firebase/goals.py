import uuid
from datetime import datetime

from . import db_state
from .core import normalize_user_email, user_exists
from ..logging_service import logger

GOAL_TITLE_MAX_LEN = 120
GOAL_DESCRIPTION_MAX_LEN = 500
GOAL_MAX_ITEMS = 200


def _normalize_goal(goal):
    """Normalize a single goal object."""
    if not isinstance(goal, dict):
        return None
    
    goal_id = goal.get("id")
    if not isinstance(goal_id, str) or not db_state.TODO_ID_RE.match(goal_id):
        return None
    
    title = goal.get("title")
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title or len(title) > GOAL_TITLE_MAX_LEN:
        return None
    
    # Validate unit type
    unit = goal.get("unit", "")
    if not isinstance(unit, str):
        unit = ""
    unit = unit.strip()[:20]
    
    # Validate target (must be numeric if unit exists)
    target = goal.get("target")
    if target is not None:
        try:
            target = float(target)
            if target < 0:
                return None
        except (ValueError, TypeError):
            return None
    
    # Validate current progress
    current = goal.get("current")
    if current is not None:
        try:
            current = float(current)
            if current < 0:
                return None
        except (ValueError, TypeError):
            return None
    
    # Validate deadline
    deadline = goal.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, str):
            return None
        try:
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            return None
    
    # Validate category
    category = goal.get("category", "")
    if not isinstance(category, str):
        category = ""
    category = category.strip()[:40]
    
    # Validate description
    description = goal.get("description", "")
    if not isinstance(description, str):
        description = ""
    description = description.strip()[:GOAL_DESCRIPTION_MAX_LEN]
    
    # Validate createdAt
    created_at = goal.get("createdAt")
    if created_at is not None:
        if not isinstance(created_at, str):
            return None
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    
    # Validate completed
    completed = goal.get("completed", False)
    if not isinstance(completed, bool):
        completed = False
    
    return {
        "id": goal_id,
        "title": title,
        "description": description,
        "target": target,
        "current": current,
        "unit": unit,
        "deadline": deadline,
        "category": category,
        "createdAt": created_at,
        "completed": completed,
    }


def _normalize_goals_list(raw):
    """Normalize a list of goals."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        normalized = _normalize_goal(item)
        if normalized:
            out.append(normalized)
    return out


def get_goals(email):
    """Get all goals for a user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return []
    
    if db_state.users_collection_ref:
        try:
            doc = db_state.users_collection_ref.document(email_key).get()
            if doc.exists:
                data = doc.to_dict() or {}
                return _normalize_goals_list(data.get("goals_v1"))
        except Exception as e:
            logger.error("Firestore goals read failed", extra={
                "operation": "get_goals",
                "error": str(e),
            })
    
    return list(db_state.goals_memory.get(email_key, []))


def _write_goals_list(email_key, goals_list):
    """Write goals list to Firestore or memory."""
    if db_state.users_collection_ref:
        try:
            doc_ref = db_state.users_collection_ref.document(email_key)
            if not doc_ref.get().exists:
                return False
            doc_ref.set({"goals_v1": goals_list}, merge=True)
            return True
        except Exception as e:
            logger.error("Firestore goals write failed", extra={
                "operation": "_write_goals_list",
                "error": str(e),
            })
            return False
    
    if email_key not in db_state.auth_users_memory:
        return False
    db_state.goals_memory[email_key] = list(goals_list)
    return True


def add_goal(email, title, description="", target=None, unit="", deadline=None, category=""):
    """Add a new goal."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(title, str):
        return False, "invalid_title", None
    title = title.strip()
    if not title or len(title) > GOAL_TITLE_MAX_LEN:
        return False, "invalid_title", None
    
    if description and not isinstance(description, str):
        return False, "invalid_description", None
    if description:
        description = description.strip()[:GOAL_DESCRIPTION_MAX_LEN]
    
    if target is not None:
        try:
            target = float(target)
            if target < 0:
                return False, "invalid_target", None
        except (ValueError, TypeError):
            return False, "invalid_target", None
    
    if unit and not isinstance(unit, str):
        return False, "invalid_unit", None
    if unit:
        unit = unit.strip()[:20]
    
    if deadline is not None:
        if not isinstance(deadline, str):
            return False, "invalid_deadline", None
        try:
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            return False, "invalid_deadline", None
    
    if category and not isinstance(category, str):
        return False, "invalid_category", None
    if category:
        category = category.strip()[:40]
    
    goals = get_goals(email)
    if len(goals) >= GOAL_MAX_ITEMS:
        return False, "too_many_goals", None
    
    now = datetime.utcnow().isoformat()
    new_goal = {
        "id": uuid.uuid4().hex,
        "title": title,
        "description": description,
        "target": target,
        "current": 0,
        "unit": unit,
        "deadline": deadline,
        "category": category,
        "createdAt": now,
        "completed": False,
    }
    
    goals.append(new_goal)
    if _write_goals_list(email_key, goals):
        return True, None, goals
    return False, "write_failed", None


def update_goal(email, goal_id, updates):
    """Update an existing goal."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(goal_id, str) or not db_state.TODO_ID_RE.match(goal_id):
        return False, "invalid_goal_id", None
    
    if not isinstance(updates, dict):
        return False, "invalid_updates", None
    
    goals = get_goals(email)
    goal_index = None
    for i, g in enumerate(goals):
        if g.get("id") == goal_id:
            goal_index = i
            break
    
    if goal_index is None:
        return False, "not_found", None
    
    # Create updated goal
    updated_goal = dict(goals[goal_index])
    
    # Apply updates with validation
    if "title" in updates:
        title = updates["title"]
        if not isinstance(title, str):
            return False, "invalid_title", None
        title = title.strip()
        if not title or len(title) > GOAL_TITLE_MAX_LEN:
            return False, "invalid_title", None
        updated_goal["title"] = title
    
    if "description" in updates:
        description = updates["description"]
        if description is not None and not isinstance(description, str):
            return False, "invalid_description", None
        if description:
            description = description.strip()[:GOAL_DESCRIPTION_MAX_LEN]
        updated_goal["description"] = description
    
    if "target" in updates:
        target = updates["target"]
        if target is not None:
            try:
                target = float(target)
                if target < 0:
                    return False, "invalid_target", None
            except (ValueError, TypeError):
                return False, "invalid_target", None
        updated_goal["target"] = target
    
    if "current" in updates:
        current = updates["current"]
        if current is not None:
            try:
                current = float(current)
                if current < 0:
                    return False, "invalid_current", None
            except (ValueError, TypeError):
                return False, "invalid_current", None
        updated_goal["current"] = current
    
    if "unit" in updates:
        unit = updates["unit"]
        if unit is not None and not isinstance(unit, str):
            return False, "invalid_unit", None
        if unit:
            unit = unit.strip()[:20]
        updated_goal["unit"] = unit
    
    if "deadline" in updates:
        deadline = updates["deadline"]
        if deadline is not None:
            if not isinstance(deadline, str):
                return False, "invalid_deadline", None
            try:
                datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            except ValueError:
                return False, "invalid_deadline", None
        updated_goal["deadline"] = deadline
    
    if "category" in updates:
        category = updates["category"]
        if category is not None and not isinstance(category, str):
            return False, "invalid_category", None
        if category:
            category = category.strip()[:40]
        updated_goal["category"] = category
    
    if "completed" in updates:
        completed = updates["completed"]
        if not isinstance(completed, bool):
            return False, "invalid_completed", None
        updated_goal["completed"] = completed
    
    # Update the goal in the list
    goals[goal_index] = updated_goal
    
    if _write_goals_list(email_key, goals):
        return True, None, goals
    return False, "write_failed", None


def increment_goal_progress(email, goal_id, amount=1):
    """Increment the current progress of a goal by a specified amount."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(goal_id, str) or not db_state.TODO_ID_RE.match(goal_id):
        return False, "invalid_goal_id", None
    
    try:
        amount = float(amount)
        if amount <= 0:
            return False, "invalid_amount", None
    except (ValueError, TypeError):
        return False, "invalid_amount", None
    
    goals = get_goals(email)
    goal_index = None
    for i, g in enumerate(goals):
        if g.get("id") == goal_id:
            goal_index = i
            break
    
    if goal_index is None:
        return False, "not_found", None
    
    goal = goals[goal_index]
    current = goal.get("current") or 0
    target = goal.get("target")
    
    new_current = current + amount
    
    # If there's a target and we exceed it, cap at target
    if target is not None and new_current > target:
        new_current = target
    
    goal["current"] = new_current
    
    # Auto-complete if target is reached
    if target is not None and new_current >= target:
        goal["completed"] = True
    
    goals[goal_index] = goal
    
    if _write_goals_list(email_key, goals):
        return True, None, goals
    return False, "write_failed", None


def delete_goal(email, goal_id):
    """Delete a goal."""
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    if not isinstance(goal_id, str) or not db_state.TODO_ID_RE.match(goal_id):
        return False, "invalid_goal_id", None
    
    goals = get_goals(email)
    next_goals = [g for g in goals if g.get("id") != goal_id]
    
    if len(next_goals) == len(goals):
        return False, "not_found", None
    
    if _write_goals_list(email_key, next_goals):
        return True, None, next_goals
    return False, "write_failed", None
