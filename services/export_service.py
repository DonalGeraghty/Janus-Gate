"""
Data export service for Janus-Gate.
Provides functionality to export user data in JSON and CSV formats.
"""

import csv
import io
import json
import zipfile
from datetime import datetime

from services.firebase_service import (
    get_habits_map,
    get_custom_habits,
    get_habit_categories,
    get_todos,
    get_flashcard_groups,
    get_nutrition_history,
    get_stoic_journal,
    get_day_planner_options,
    get_day_planner_daily,
    get_meal_plan_daily,
    get_goals,
    get_sleep_entries,
)
from services.logging_service import logger


def _safe_json_serializer(obj):
    """Handle datetime serialization for JSON export."""
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    return str(obj)


def _create_json_file(data, filename):
    """Create a JSON file in memory."""
    json_str = json.dumps(data, indent=2, default=_safe_json_serializer, ensure_ascii=False)
    return io.BytesIO(json_str.encode('utf-8')), filename


def _create_csv_file(data, filename, fieldnames=None):
    """Create a CSV file in memory."""
    output = io.StringIO()
    
    if not data:
        return io.BytesIO(b''), filename
    
    # If data is a list of dicts
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if fieldnames is None:
            fieldnames = list(data[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)
    # If data is a dict
    elif isinstance(data, dict):
        writer = csv.DictWriter(output, fieldnames=['key', 'value'])
        writer.writeheader()
        for key, value in data.items():
            writer.writerow({'key': key, 'value': value})
    else:
        output.write(str(data))
    
    csv_bytes = io.BytesIO(output.getvalue().encode('utf-8'))
    return csv_bytes, filename


def _get_habits_data(email):
    """Export habits data."""
    habits_map = get_habits_map(email)
    custom_habits = get_custom_habits(email)
    categories = get_habit_categories(email)
    
    return {
        'cells': habits_map,
        'custom_habits': custom_habits,
        'categories': categories,
    }


def _get_todos_data(email):
    """Export todos data."""
    return get_todos(email)


def _get_flashcards_data(email):
    """Export flashcards data."""
    return get_flashcard_groups(email)


def _get_nutrition_data(email):
    """Export nutrition data."""
    return get_nutrition_history(email)


def _get_stoic_data(email):
    """Export stoic journal data."""
    return get_stoic_journal(email)


def _get_day_planner_data(email):
    """Export day planner data."""
    options = get_day_planner_options(email)
    daily = get_day_planner_daily(email)
    return {
        'options': options,
        'daily': daily,
    }


def _get_meal_plan_data(email):
    """Export meal plan data."""
    return get_meal_plan_daily(email)


def _get_goals_data(email):
    """Export goals data."""
    return get_goals(email)


def _get_sleep_data(email):
    """Export sleep data."""
    return get_sleep_entries(email)


def export_user_data(email):
    """
    Export all user data as a ZIP file containing JSON and CSV files.
    
    Returns:
        tuple: (success: bool, error: str or None, zip_bytes: bytes or None, filename: str)
    """
    if not email:
        return False, "Email is required", None, None
    
    try:
        # Create in-memory ZIP file
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Export timestamp
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            
            # 1. Habits
            habits_data = _get_habits_data(email)
            if habits_data:
                json_bytes, _ = _create_json_file(habits_data, 'habits.json')
                zip_file.writestr('habits.json', json_bytes.getvalue().decode('utf-8'))
                
                # CSV for habit cells
                cells = habits_data.get('cells', {})
                if cells:
                    cell_list = [{'date_habit': k, 'state': v} for k, v in cells.items()]
                    csv_bytes, _ = _create_csv_file(cell_list, 'habits_cells.csv')
                    zip_file.writestr('habits_cells.csv', csv_bytes.getvalue().decode('utf-8'))
            
            # 2. Todos
            todos_data = _get_todos_data(email)
            if todos_data:
                json_bytes, _ = _create_json_file(todos_data, 'todos.json')
                zip_file.writestr('todos.json', json_bytes.getvalue().decode('utf-8'))
                csv_bytes, _ = _create_csv_file(todos_data, 'todos.csv')
                zip_file.writestr('todos.csv', csv_bytes.getvalue().decode('utf-8'))
            
            # 3. Flashcards
            flashcards_data = _get_flashcards_data(email)
            if flashcards_data:
                json_bytes, _ = _create_json_file(flashcards_data, 'flashcards.json')
                zip_file.writestr('flashcards.json', json_bytes.getvalue().decode('utf-8'))
            
            # 4. Nutrition
            nutrition_data = _get_nutrition_data(email)
            if nutrition_data:
                json_bytes, _ = _create_json_file(nutrition_data, 'nutrition.json')
                zip_file.writestr('nutrition.json', json_bytes.getvalue().decode('utf-8'))
                
                # CSV for nutrition history
                history = nutrition_data.get('history', {})
                if history:
                    history_list = []
                    for date, data in history.items():
                        if isinstance(data, dict):
                            row = {'date': date}
                            row.update(data)
                            history_list.append(row)
                    csv_bytes, _ = _create_csv_file(history_list, 'nutrition_history.csv')
                    zip_file.writestr('nutrition_history.csv', csv_bytes.getvalue().decode('utf-8'))
            
            # 5. Stoic Journal
            stoic_data = _get_stoic_data(email)
            if stoic_data:
                json_bytes, _ = _create_json_file(stoic_data, 'stoic.json')
                zip_file.writestr('stoic.json', json_bytes.getvalue().decode('utf-8'))
            
            # 6. Day Planner
            day_planner_data = _get_day_planner_data(email)
            if day_planner_data:
                json_bytes, _ = _create_json_file(day_planner_data, 'day_planner.json')
                zip_file.writestr('day_planner.json', json_bytes.getvalue().decode('utf-8'))
            
            # 7. Meal Plan
            meal_plan_data = _get_meal_plan_data(email)
            if meal_plan_data:
                json_bytes, _ = _create_json_file(meal_plan_data, 'meal_plan.json')
                zip_file.writestr('meal_plan.json', json_bytes.getvalue().decode('utf-8'))
            
            # 8. Goals
            goals_data = _get_goals_data(email)
            if goals_data:
                json_bytes, _ = _create_json_file(goals_data, 'goals.json')
                zip_file.writestr('goals.json', json_bytes.getvalue().decode('utf-8'))
                csv_bytes, _ = _create_csv_file(goals_data, 'goals.csv')
                zip_file.writestr('goals.csv', csv_bytes.getvalue().decode('utf-8'))
            
            # 9. Sleep
            sleep_data = _get_sleep_data(email)
            if sleep_data:
                json_bytes, _ = _create_json_file(sleep_data, 'sleep.json')
                zip_file.writestr('sleep.json', json_bytes.getvalue().decode('utf-8'))
                csv_bytes, _ = _create_csv_file(sleep_data, 'sleep.csv')
                zip_file.writestr('sleep.csv', csv_bytes.getvalue().decode('utf-8'))
            
            # 10. Metadata
            metadata = {
                'export_date': datetime.utcnow().isoformat(),
                'email': email,
                'data_types': [
                    'habits', 'todos', 'flashcards', 'nutrition', 
                    'stoic', 'day_planner', 'meal_plan', 'goals', 'sleep'
                ],
                'format': 'ZIP with JSON and CSV files',
            }
            json_bytes, _ = _create_json_file(metadata, 'metadata.json')
            zip_file.writestr('metadata.json', json_bytes.getvalue().decode('utf-8'))
            
            # 11. Summary markdown
            summary = f"""# Minerva Data Export

**Exported:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
**User:** {email}

## Included Data

- **habits.json/csv** - Habit tracker cells and custom habits
- **todos.json/csv** - Todo list items
- **flashcards.json** - Flashcard groups and cards
- **nutrition.json/csv** - Nutrition history
- **stoic.json** - Stoic journal entries
- **day_planner.json** - Day planner options and daily slots
- **meal_plan.json** - Meal plan selections
- **goals.json/csv** - Goal progress tracker data
- **sleep.json/csv** - Sleep tracking data

## Usage

1. Extract the ZIP file
2. Open JSON files in any text editor or data analysis tool
3. Open CSV files in Excel, Google Sheets, or any spreadsheet application
4. Use the data for analysis, backup, or migration

---
*Exported from Minerva via Janus-Gate API*
"""
            zip_file.writestr('README.md', summary)
        
        # Get the ZIP file bytes
        zip_buffer.seek(0)
        zip_bytes = zip_buffer.read()
        
        filename = f'minerva_export_{timestamp}.zip'
        
        logger.info("Data export completed", extra={
            "operation": "export_user_data",
            "email": email,
            "filename": filename,
            "size_bytes": len(zip_bytes),
        })
        
        return True, None, zip_bytes, filename
        
    except Exception as e:
        logger.error("Data export failed", extra={
            "operation": "export_user_data",
            "email": email,
            "error": str(e),
        })
        return False, str(e), None, None
