import uuid
from datetime import datetime, timedelta

from . import db_state
from .core import normalize_user_email, user_exists
from .habits import get_habits_map, get_custom_habits
from ..logging_service import logger

ACHIEVEMENT_TITLE_MAX_LEN = 100
ACHIEVEMENT_DESCRIPTION_MAX_LEN = 500
ACHIEVEMENT_MAX_ITEMS = 100


# Pre-defined achievements
PREDEFINED_ACHIEVEMENTS = [
    {
        'id': 'streak_7',
        'title': 'Week Warrior',
        'description': 'Complete any habit for 7 consecutive days',
        'type': 'habit_streak',
        'target': 7,
        'habit_id': None,  # Any habit
        'category': 'streak',
        'icon': '🔥',
    },
    {
        'id': 'streak_30',
        'title': 'Month Master',
        'description': 'Complete any habit for 30 consecutive days',
        'type': 'habit_streak',
        'target': 30,
        'habit_id': None,
        'category': 'streak',
        'icon': '🔥🔥',
    },
    {
        'id': 'streak_90',
        'title': 'Quarter Champion',
        'description': 'Complete any habit for 90 consecutive days',
        'type': 'habit_streak',
        'target': 90,
        'habit_id': None,
        'category': 'streak',
        'icon': '🔥🔥🔥',
    },
    {
        'id': 'habits_100',
        'title': 'Centurion',
        'description': 'Complete 100 habit cells',
        'type': 'total_completions',
        'target': 100,
        'category': 'milestone',
        'icon': '🏆',
    },
    {
        'id': 'habits_500',
        'title': 'Habit Hero',
        'description': 'Complete 500 habit cells',
        'type': 'total_completions',
        'target': 500,
        'category': 'milestone',
        'icon': '🏆🏆',
    },
    {
        'id': 'habits_1000',
        'title': 'Habit Legend',
        'description': 'Complete 1000 habit cells',
        'type': 'total_completions',
        'target': 1000,
        'category': 'milestone',
        'icon': '🏆🏆🏆',
    },
    {
        'id': 'todos_50',
        'title': 'Task Master',
        'description': 'Complete 50 todos',
        'type': 'todos_completed',
        'target': 50,
        'category': 'productivity',
        'icon': '✅',
    },
    {
        'id': 'goals_10',
        'title': 'Goal Getter',
        'description': 'Complete 10 goals',
        'type': 'goals_completed',
        'target': 10,
        'category': 'goals',
        'icon': '🎯',
    },
    {
        'id': 'goals_50',
        'title': 'Goal Crusader',
        'description': 'Complete 50 goals',
        'type': 'goals_completed',
        'target': 50,
        'category': 'goals',
        'icon': '🎯🎯',
    },
    {
        'id': 'perfect_week',
        'title': 'Perfect Week',
        'description': 'Complete all habits every day for a week',
        'type': 'perfect_streak',
        'target': 7,
        'category': 'perfection',
        'icon': '⭐',
    },
]


def _get_achievement_definitions():
    """Get all achievement definitions."""
    return list(PREDEFINED_ACHIEVEMENTS)


def _calculate_habit_streaks(email):
    """Calculate current and longest streaks for each habit."""
    email_key = normalize_user_email(email)
    if not email_key:
        return {}
    
    habits_map = get_habits_map(email)
    custom_habits = get_custom_habits(email)
    
    # Group cells by habit_id
    habit_cells = {}
    for cell_key, state in habits_map.items():
        if state == 'done':
            parts = cell_key.split('_', 1)
            if len(parts) == 2:
                date_str, habit_id = parts
                if habit_id not in habit_cells:
                    habit_cells[habit_id] = []
                habit_cells[habit_id].append(date_str)
    
    # Calculate streaks for each habit
    streaks = {}
    for habit_id, dates in habit_cells.items():
        if not dates:
            continue
        
        # Sort dates
        dates_sorted = sorted(dates)
        
        # Find current streak
        current_streak = 0
        longest_streak = 0
        temp_streak = 0
        
        for i, date_str in enumerate(dates_sorted):
            date = datetime.strptime(date_str, '%Y-%m-%d').date()
            
            if i == 0:
                temp_streak = 1
            else:
                prev_date = datetime.strptime(dates_sorted[i-1], '%Y-%m-%d').date()
                if date == prev_date + timedelta(days=1):
                    temp_streak += 1
                else:
                    longest_streak = max(longest_streak, temp_streak)
                    temp_streak = 1
            
            current_streak = temp_streak
        
        longest_streak = max(longest_streak, temp_streak)
        
        # Check if streak continues to today
        if dates_sorted:
            last_date = datetime.strptime(dates_sorted[-1], '%Y-%m-%d').date()
            today = datetime.utcnow().date()
            if last_date == today:
                pass  # Streak is current
            elif last_date == today - timedelta(days=1):
                current_streak += 1
            else:
                current_streak = 0
        
        streaks[habit_id] = {
            'current': current_streak,
            'longest': longest_streak,
        }
    
    return streaks


def _calculate_total_completions(email):
    """Calculate total habit completions."""
    habits_map = get_habits_map(email)
    return len([state for state in habits_map.values() if state == 'done'])


def _calculate_todos_completed(email):
    """Calculate total todos completed."""
    from .todos import get_todos
    # This is a simplified count - in reality, todos are deleted when completed
    # So we'll use the habit completion as a proxy or track separately
    habits_map = get_habits_map(email)
    return len([state for state in habits_map.values() if state == 'done'])


def _calculate_goals_completed(email):
    """Calculate total goals completed."""
    from .goals import get_goals
    goals = get_goals(email)
    return len([g for g in goals if g.get('completed', False)])


def _check_achievements(email):
    """Check which achievements the user has earned."""
    email_key = normalize_user_email(email)
    if not email_key:
        return []
    
    # Get existing achievements
    existing = get_achievements(email)
    existing_ids = {a.get('achievementId') for a in existing}
    
    # Calculate stats
    streaks = _calculate_habit_streaks(email)
    total_completions = _calculate_total_completions(email)
    goals_completed = _calculate_goals_completed(email)
    
    # Get all habit IDs
    custom_habits = get_custom_habits(email)
    habit_ids = {h.get('id') for h in custom_habits}
    
    earned_achievements = []
    
    for achievement in PREDEFINED_ACHIEVEMENTS:
        ach_id = achievement.get('id')
        ach_type = achievement.get('type')
        target = achievement.get('target')
        habit_id = achievement.get('habit_id')
        
        # Skip if already earned
        if ach_id in existing_ids:
            continue
        
        earned = False
        
        if ach_type == 'habit_streak':
            # Check if any habit has reached the streak target
            if habit_id:
                # Specific habit
                if habit_id in streaks and streaks[habit_id].get('current', 0) >= target:
                    earned = True
            else:
                # Any habit
                for hid, streak_data in streaks.items():
                    if streak_data.get('current', 0) >= target:
                        earned = True
                        break
        
        elif ach_type == 'total_completions':
            if total_completions >= target:
                earned = True
        
        elif ach_type == 'todos_completed':
            todos_completed = _calculate_todos_completed(email)
            if todos_completed >= target:
                earned = True
        
        elif ach_type == 'goals_completed':
            if goals_completed >= target:
                earned = True
        
        elif ach_type == 'perfect_streak':
            # Check if all habits were completed for target consecutive days
            # This is more complex - for now, use total completions as proxy
            if total_completions >= target * len(habit_ids):
                earned = True
        
        if earned:
            earned_achievements.append({
                'achievementId': ach_id,
                'unlockedAt': datetime.utcnow().isoformat(),
                'title': achievement.get('title'),
                'description': achievement.get('description'),
                'category': achievement.get('category'),
                'icon': achievement.get('icon'),
                'type': ach_type,
                'target': target,
            })
    
    return earned_achievements


def get_achievements(email):
    """Get all unlocked achievements for a user."""
    email_key = normalize_user_email(email)
    if not email_key:
        return []
    
    if db_state.users_collection_ref:
        try:
            doc = db_state.users_collection_ref.document(email_key).get()
            if doc.exists:
                data = doc.to_dict() or {}
                achievements = data.get("achievements_v1")
                if isinstance(achievements, list):
                    return achievements
        except Exception as e:
            logger.error("Firestore achievements read failed", extra={
                "operation": "get_achievements",
                "error": str(e),
            })
    
    return list(db_state.achievements_memory.get(email_key, []))


def _write_achievements_list(email_key, achievements_list):
    """Write achievements list to Firestore or memory."""
    if db_state.users_collection_ref:
        try:
            doc_ref = db_state.users_collection_ref.document(email_key)
            if not doc_ref.get().exists:
                return False
            doc_ref.set({"achievements_v1": achievements_list}, merge=True)
            return True
        except Exception as e:
            logger.error("Firestore achievements write failed", extra={
                "operation": "_write_achievements_list",
                "error": str(e),
            })
            return False
    
    if email_key not in db_state.auth_users_memory:
        return False
    db_state.achievements_memory[email_key] = list(achievements_list)
    return True


def unlock_achievements(email):
    """
    Check and unlock any new achievements the user has earned.
    
    Returns:
        tuple: (success: bool, error: str or None, achievements: list or None)
    """
    email_key = normalize_user_email(email)
    if not email_key or not user_exists(email_key):
        return False, "no_user", None
    
    # Check for new achievements
    new_achievements = _check_achievements(email)
    
    if not new_achievements:
        # No new achievements
        existing = get_achievements(email)
        return True, None, existing
    
    # Add new achievements to existing list
    existing = get_achievements(email)
    updated = list(existing) + new_achievements
    
    if _write_achievements_list(email_key, updated):
        return True, None, updated
    
    return False, "write_failed", None


def get_achievement_definitions():
    """Get all achievement definitions (not user-specific)."""
    return _get_achievement_definitions()


def get_user_stats(email):
    """
    Get user statistics for dashboard display.
    
    Returns:
        dict: User statistics including streaks, totals, and progress
    """
    email_key = normalize_user_email(email)
    if not email_key:
        return {}
    
    streaks = _calculate_habit_streaks(email)
    total_completions = _calculate_total_completions(email)
    goals_completed = _calculate_goals_completed(email)
    achievements = get_achievements(email)
    
    # Calculate overall stats
    custom_habits = get_custom_habits(email)
    total_habits = len(custom_habits)
    
    # Active streaks (current > 0)
    active_streaks = sum(1 for s in streaks.values() if s.get('current', 0) > 0)
    
    # Longest streak across all habits
    longest_streak = max((s.get('longest', 0) for s in streaks.values()), default=0)
    
    # Current streak (max of all current streaks)
    current_streak = max((s.get('current', 0) for s in streaks.values()), default=0)
    
    return {
        'totalHabits': total_habits,
        'totalCompletions': total_completions,
        'activeStreaks': active_streaks,
        'longestStreak': longest_streak,
        'currentStreak': current_streak,
        'goalsCompleted': goals_completed,
        'achievementsUnlocked': len(achievements),
        'achievements': achievements,
        'habitStreaks': streaks,
    }
