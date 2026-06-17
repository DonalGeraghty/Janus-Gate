"""
Compatibility facade.

Firebase-related functionality has been split into feature-focused modules under
`services/firebase/` (users, habits, todos, flashcards, nutrition, stoic).
This module re-exports those APIs so existing imports continue to work.
"""

from .firebase import (
    initialize_firebase,
    get_database_status,
    create_user_record,
    get_user_record,
    delete_user_account,
    get_habits_map,
    merge_habits_map,
    patch_habit_cell,
    get_custom_habits,
    update_custom_habits,
    get_habit_categories,
    add_habit_category,
    update_habit_category,
    delete_habit_category,
    get_todos,
    add_todo_item,
    delete_todo_item,
    get_flashcard_groups,
    update_flashcard_groups,
    add_flashcard_group,
    add_flashcard_to_group,
    get_random_flashcards,
    get_nutrition_history,
    update_nutrition_history,
    get_stoic_journal,
    update_stoic_journal,
    get_day_planner_options,
    add_day_planner_option,
    update_day_planner_option,
    delete_day_planner_option,
    get_day_planner_daily,
    update_day_planner_daily,
    get_meal_plan_sections,
    get_meal_plan_daily,
    update_meal_plan_daily,
)
from .firebase.goals import (
    get_goals,
    add_goal,
    update_goal,
    increment_goal_progress,
    delete_goal,
)
from .firebase.sleep import (
    get_sleep_entries,
    add_sleep_entry,
    update_sleep_entry,
    delete_sleep_entry,
)
from .firebase.achievements import (
    get_achievements,
    unlock_achievements,
    get_achievement_definitions,
    get_user_stats,
)
