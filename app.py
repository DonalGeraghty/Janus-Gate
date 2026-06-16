# Portfolio auth API — codename Janus (Roman god of doorways and transitions).

# Standard library imports
import os
import time
from datetime import datetime

# Third-party imports
from flask import Flask, request, jsonify
from flask_cors import CORS

# Local imports
from core.auth_service import (
    decode_access_token,
    login_user,
    register_user,
    verify_password,
)
from services.logging_service import get_flask_app_logger
from services.firebase_service import (
    get_habits_map, merge_habits_map, patch_habit_cell,
    get_user_record,
    delete_user_account,
    get_custom_habits, update_custom_habits,
    get_habit_categories,
    add_habit_category,
    update_habit_category,
    delete_habit_category,
    get_todos, add_todo_item, delete_todo_item,
    get_flashcard_groups, update_flashcard_groups,
    add_flashcard_group, add_flashcard_to_group,
    get_random_flashcards,
    get_nutrition_history, update_nutrition_history,
    get_stoic_journal, update_stoic_journal,
    get_day_planner_options,
    add_day_planner_option,
    update_day_planner_option,
    delete_day_planner_option,
    get_day_planner_daily,
    update_day_planner_daily,
    get_meal_plan_sections,
    get_meal_plan_daily,
    update_meal_plan_daily,
    get_goals,
    add_goal,
    update_goal,
    increment_goal_progress,
    delete_goal,
)

# Initialize logger
logger = get_flask_app_logger()

app = Flask(__name__)
CORS(app)


def _bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None

# Request logging middleware
@app.before_request
def log_request_info():
    """Log request information before processing"""
    request.start_time = time.time()

    # Extract client information
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    user_agent = request.headers.get('User-Agent', 'Unknown')

    logger.info("Request received", extra={
        "operation": "request_received",
        "method": request.method,
        "path": request.path,
        "client_ip": client_ip,
        "user_agent": user_agent[:100],  # Truncate long user agents
        "content_length": request.content_length or 0,
        "timestamp": datetime.utcnow().isoformat()
    })

@app.after_request
def log_response_info(response):
    """Log response information after processing"""
    if hasattr(request, 'start_time'):
        duration = (time.time() - request.start_time) * 1000  # Convert to milliseconds

        logger.info("Request completed", extra={
            "operation": "request_completed",
            "method": request.method,
            "path": request.path,
            "status_code": response.status_code,
            "duration_ms": round(duration, 2),
            "response_size": len(response.get_data()),
            "timestamp": datetime.utcnow().isoformat()
        })

    return response


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    password = data.get("password")
    payload, err, _ = register_user(email, password)
    if err == "invalid_email":
        return jsonify({"status": "error", "error": "Invalid email"}), 400
    if err == "weak_password":
        return jsonify({
            "status": "error",
            "error": "Password must be at least 8 characters",
        }), 400
    if err == "exists":
        return jsonify({"status": "error", "error": "An account with this email already exists"}), 409
    if err:
        return jsonify({"status": "error", "error": "Registration failed"}), 500
    return jsonify({
        "status": "success",
        "token": payload["token"],
        "user": {"email": payload["email"]},
    }), 201


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    password = data.get("password")
    payload, err, _ = login_user(email, password)
    if err:
        return jsonify({"status": "error", "error": "Invalid email or password"}), 401
    return jsonify({
        "status": "success",
        "token": payload["token"],
        "user": {"email": payload["email"]},
    }), 200


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    return jsonify({"status": "success", "user": {"email": email}}), 200


@app.route("/api/auth/account", methods=["DELETE"])
def auth_delete_account():
    """Delete the authenticated user after password confirmation."""
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    password = data.get("password")
    if not password:
        return jsonify({"status": "error", "error": "Password is required"}), 400

    row = get_user_record(email)
    if not row or not verify_password(password, row.get("password_hash")):
        return jsonify({"status": "error", "error": "Invalid password"}), 401

    ok, err = delete_user_account(email)
    if not ok:
        return jsonify({"status": "error", "error": err or "Could not delete account"}), 500

    logger.info("Account deleted via API", extra={
        "operation": "auth_delete_account",
        "email": email,
    })
    return jsonify({"status": "success"}), 200


@app.route("/api/habits", methods=["GET"])
def habits_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    cells = get_habits_map(email)
    return jsonify({"status": "success", "cells": cells}), 200


@app.route("/api/habits", methods=["PUT"])
def habits_put_merge():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    incoming = data.get("cells")
    if not isinstance(incoming, dict):
        return jsonify({"status": "error", "error": "invalid_body"}), 400
    ok, err, merged = merge_habits_map(email, incoming)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        return jsonify({"status": "error", "error": err or "merge_failed"}), code
    return jsonify({"status": "success", "cells": merged}), 200


@app.route("/api/habits/cell", methods=["PATCH"])
def habits_patch_cell():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    date_str = data.get("date")
    habit_id = data.get("habitId")
    state = data.get("state")
    ok, err = patch_habit_cell(email, date_str, habit_id, state)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "patch_failed"}), code
    cells = get_habits_map(email)
    return jsonify({"status": "success", "cells": cells}), 200


@app.route("/api/user/habits", methods=["GET"])
def user_habits_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    habits = get_custom_habits(email)
    return jsonify({"status": "success", "habits": habits}), 200


@app.route("/api/user/habits", methods=["PUT"])
def user_habits_put():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    incoming = data.get("habits")
    if not isinstance(incoming, list):
        return jsonify({"status": "error", "error": "invalid_body"}), 400
    ok, err, habits = update_custom_habits(email, incoming)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "habits": habits}), 200


@app.route("/api/user/habit-categories", methods=["GET"])
def user_habit_categories_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    categories = get_habit_categories(email)
    return jsonify({"status": "success", "categories": categories}), 200


@app.route("/api/user/habit-categories", methods=["POST"])
def user_habit_categories_post():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    label = data.get("label")
    ok, err, categories = add_habit_category(email, label)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "add_failed"}), code
    return jsonify({"status": "success", "categories": categories}), 200


@app.route("/api/user/habit-categories", methods=["PATCH"])
def user_habit_categories_patch():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    category_id = data.get("id")
    label = data.get("label")
    ok, err, categories = update_habit_category(email, category_id, label)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "categories": categories}), 200


@app.route("/api/user/habit-categories", methods=["DELETE"])
def user_habit_categories_delete():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    category_id = data.get("id")
    reassign_to = data.get("reassignTo")
    ok, err, categories = delete_habit_category(email, category_id, reassign_to)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "category_in_use":
            code = 409
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "delete_failed"}), code
    return jsonify({"status": "success", "categories": categories}), 200


@app.route("/api/user/todos", methods=["GET"])
def user_todos_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    todos = get_todos(email)
    return jsonify({"status": "success", "todos": todos}), 200


@app.route("/api/user/todos", methods=["POST"])
def user_todos_post():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    ok, err, todos = add_todo_item(email, text)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "too_many_todos":
            code = 429
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "add_failed"}), code
    return jsonify({"status": "success", "todos": todos}), 201


@app.route("/api/user/todos", methods=["DELETE"])
def user_todos_delete():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    todo_id = data.get("todoId") or data.get("id")
    ok, err, todos = delete_todo_item(email, todo_id)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "delete_failed"}), code
    return jsonify({"status": "success", "todos": todos}), 200


@app.route("/api/user/flashcards", methods=["GET"])
def user_flashcards_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    groups = get_flashcard_groups(email)
    return jsonify({"status": "success", "groups": groups}), 200


@app.route("/api/user/flashcards", methods=["PUT"])
def user_flashcards_put():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    incoming = data.get("groups")
    if not isinstance(incoming, list):
        return jsonify({"status": "error", "error": "invalid_body"}), 400
    ok, err, groups = update_flashcard_groups(email, incoming)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "groups": groups}), 200


@app.route("/api/user/flashcards/groups", methods=["POST"])
def user_flashcards_groups_post():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    ok, err, groups = add_flashcard_group(email, name)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "add_group_failed"}), code
    return jsonify({"status": "success", "groups": groups}), 201


@app.route("/api/user/flashcards/cards", methods=["POST"])
def user_flashcards_cards_post():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    group_id = data.get("groupId")
    front = data.get("front")
    back = data.get("back")
    ok, err, groups = add_flashcard_to_group(email, group_id, front, back)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "group_not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "add_card_failed"}), code
    return jsonify({"status": "success", "groups": groups}), 201


@app.route("/api/user/flashcards/study", methods=["GET"])
def user_flashcards_study_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    group_id = request.args.get("groupId")
    ok, err, cards = get_random_flashcards(email, group_id)
    if not ok:
        code = 400
        if err == "group_not_found":
            code = 404
        return jsonify({"status": "error", "error": err or "study_failed"}), code
    return jsonify({"status": "success", "cards": cards}), 200


@app.route("/api/user/nutrition", methods=["GET"])
def user_nutrition_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    history = get_nutrition_history(email)
    return jsonify({"status": "success", "history": history}), 200


@app.route("/api/user/nutrition", methods=["PUT"])
def user_nutrition_put():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    incoming = data.get("history")
    if not isinstance(incoming, dict):
        return jsonify({"status": "error", "error": "invalid_body"}), 400
    ok, err, history = update_nutrition_history(email, incoming)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "history": history}), 200


@app.route("/api/user/stoic", methods=["GET"])
def user_stoic_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    payload = get_stoic_journal(email)
    return jsonify({"status": "success", "entry": payload}), 200


@app.route("/api/user/stoic", methods=["PUT"])
def user_stoic_put():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    date_key = data.get("date")
    form = data.get("form")
    ok, err, payload = update_stoic_journal(email, date_key, form)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "entry": payload}), 200


@app.route("/api/user/day-planner/options", methods=["GET"])
def user_day_planner_options_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    options = get_day_planner_options(email)
    return jsonify({"status": "success", "options": options}), 200


@app.route("/api/user/day-planner/options", methods=["POST"])
def user_day_planner_options_post():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    label = data.get("label")
    ok, err, options = add_day_planner_option(email, label)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "add_failed"}), code
    return jsonify({"status": "success", "options": options}), 200


@app.route("/api/user/day-planner/options", methods=["PATCH"])
def user_day_planner_options_patch():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    option_id = data.get("id")
    label = data.get("label")
    ok, err, options = update_day_planner_option(email, option_id, label)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "options": options}), 200


@app.route("/api/user/day-planner/options", methods=["DELETE"])
def user_day_planner_options_delete():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    option_id = data.get("id")
    ok, err, options = delete_day_planner_option(email, option_id)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "delete_failed"}), code
    return jsonify({"status": "success", "options": options}), 200


@app.route("/api/user/day-planner/daily", methods=["GET"])
def user_day_planner_daily_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    entry = get_day_planner_daily(email)
    return jsonify({"status": "success", "entry": entry}), 200


@app.route("/api/user/day-planner/daily", methods=["PUT"])
def user_day_planner_daily_put():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    date_key = data.get("date")
    slots = data.get("slots")
    ok, err, payload = update_day_planner_daily(email, date_key, slots)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "entry": payload}), 200


@app.route("/api/user/meal-plan", methods=["GET"])
def user_meal_plan_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    sections = get_meal_plan_sections()
    entry = get_meal_plan_daily(email)
    return jsonify({"status": "success", "sections": sections, "entry": entry}), 200


@app.route("/api/user/meal-plan", methods=["PUT"])
def user_meal_plan_put():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    date_key = data.get("date")
    selections = data.get("selections")
    completed = data.get("completed")
    ok, err, payload = update_meal_plan_daily(email, date_key, selections, completed)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "entry": payload}), 200


@app.route("/api/user/goals", methods=["GET"])
def user_goals_get():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    goals = get_goals(email)
    return jsonify({"status": "success", "goals": goals}), 200


@app.route("/api/user/goals", methods=["POST"])
def user_goals_post():
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    title = data.get("title")
    description = data.get("description", "")
    target = data.get("target")
    unit = data.get("unit", "")
    deadline = data.get("deadline")
    category = data.get("category", "")
    ok, err, goals = add_goal(email, title, description, target, unit, deadline, category)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "too_many_goals":
            code = 429
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "add_failed"}), code
    return jsonify({"status": "success", "goals": goals}), 201


@app.route("/api/user/goals/<goal_id>", methods=["PATCH"])
def user_goals_patch(goal_id):
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    ok, err, goals = update_goal(email, goal_id, data)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "update_failed"}), code
    return jsonify({"status": "success", "goals": goals}), 200


@app.route("/api/user/goals/<goal_id>/increment", methods=["POST"])
def user_goals_increment(goal_id):
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    amount = data.get("amount", 1)
    ok, err, goals = increment_goal_progress(email, goal_id, amount)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "increment_failed"}), code
    return jsonify({"status": "success", "goals": goals}), 200


@app.route("/api/user/goals/<goal_id>", methods=["DELETE"])
def user_goals_delete(goal_id):
    token = _bearer_token()
    email = decode_access_token(token)
    if not email:
        return jsonify({"status": "error", "error": "Unauthorized"}), 401
    ok, err, goals = delete_goal(email, goal_id)
    if not ok:
        code = 400
        if err == "no_user":
            code = 404
        elif err == "not_found":
            code = 404
        elif err == "write_failed":
            code = 500
        return jsonify({"status": "error", "error": err or "delete_failed"}), code
    return jsonify({"status": "success", "goals": goals}), 200


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    start_time = time.time()

    logger.info("Health check request received", extra={
        "operation": "health_check",
        "endpoint": "/health",
        "method": "GET"
    })

    try:
        # Basic health checks
        health_status = {
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'checks': {
                'app_running': True,
                'timestamp': True
            }
        }

        duration = (time.time() - start_time) * 1000
        logger.info("Health check completed", extra={
            "operation": "health_check",
            "status": "healthy",
            "duration_ms": round(duration, 2)
        })

        return jsonify(health_status), 200

    except Exception as e:
        duration = (time.time() - start_time) * 1000
        logger.error("Health check failed", extra={
            "operation": "health_check",
            "error": str(e),
            "duration_ms": round(duration, 2),
            "status": "unhealthy"
        })

        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/', methods=['GET'])
def root():
    """Root endpoint with API information"""
    start_time = time.time()

    logger.info("Root endpoint request received", extra={
        "operation": "root",
        "endpoint": "/",
        "method": "GET"
    })

    try:
        api_info = {
            'message': 'Portfolio Auth API',
            'version': '1.0.0',
            'endpoints': {
                'POST /api/auth/register': 'Register with email and password (password stored as hash)',
                'POST /api/auth/login': 'Login; returns JWT bearer token',
                'GET /api/auth/me': 'Current user from Authorization: Bearer <token>',
                'DELETE /api/auth/account': 'Delete account; JSON body { password } required',
                'GET /api/habits': 'Habit tracker cells for current user (JSON map)',
                'PUT /api/habits': 'Merge habit cells body { cells: { "YYYY-MM-DD_id": "done"|"none" } }',
                'PATCH /api/habits/cell': 'Set one cell { date, habitId, state }',
                'GET /api/user/habits': 'List habit definitions { id, label, category }',
                'PUT /api/user/habits': 'Replace habits body { habits: [...] }; category must exist',
                'GET /api/user/habit-categories': 'List habit categories { id, label }',
                'POST /api/user/habit-categories': 'Add category body { label }',
                'PATCH /api/user/habit-categories': 'Rename category body { id, label }',
                'DELETE /api/user/habit-categories': 'Delete category body { id, reassignTo? }',
                'GET /health': 'Health check endpoint',
                'GET /api/user/todos': 'List your todos',
                'POST /api/user/todos': 'Add todo item body { text }',
                'DELETE /api/user/todos': 'Delete todo body { todoId }',
                'GET /api/user/flashcards': 'List flashcard groups and cards',
                'PUT /api/user/flashcards': 'Replace all flashcard groups body { groups: [...] }',
                'POST /api/user/flashcards/groups': 'Add a flashcard group body { name }',
                'POST /api/user/flashcards/cards': 'Add a card body { groupId, front, back }',
                'GET /api/user/flashcards/study': 'Get randomized cards (optional ?groupId=...)',
                'GET /api/user/nutrition': 'Get calorie/weight/water history map',
                'PUT /api/user/nutrition': 'Replace calorie/weight/water history body { history }',
                'GET /api/user/stoic': 'Get current stoic journal entry',
                'PUT /api/user/stoic': 'Replace stoic journal entry body { date, form }',
                'GET /api/user/day-planner/options': 'List day planner dropdown options',
                'POST /api/user/day-planner/options': 'Add option body { label }',
                'PATCH /api/user/day-planner/options': 'Edit option body { id, label }',
                'DELETE /api/user/day-planner/options': 'Delete option body { id }',
                'GET /api/user/day-planner/daily': 'Get today slot selections { date, slots }',
                'PUT /api/user/day-planner/daily': 'Save slots body { date, slots: { "0": optionId, ... } }',
                'GET /api/user/meal-plan': 'Get trainer meal sections and today selection/completion entry',
                'PUT /api/user/meal-plan': 'Save today meal entry body { date, selections, completed }',
                'GET /api/user/goals': 'List all goals for the current user',
                'POST /api/user/goals': 'Add a new goal body { title, description?, target?, unit?, deadline?, category? }',
                'PATCH /api/user/goals/<goal_id>': 'Update a goal body { title?, description?, target?, current?, unit?, deadline?, category?, completed? }',
                'POST /api/user/goals/<goal_id>/increment': 'Increment goal progress body { amount? } (default: 1)',
                'DELETE /api/user/goals/<goal_id>': 'Delete a goal',
                'GET /': 'This information endpoint'
            },
            'timestamp': datetime.now().isoformat()
        }

        duration = (time.time() - start_time) * 1000
        logger.info("Root endpoint completed", extra={
            "operation": "root",
            "duration_ms": round(duration, 2),
            "status": "success"
        })

        return jsonify(api_info), 200

    except Exception as e:
        duration = (time.time() - start_time) * 1000
        logger.error("Root endpoint failed", extra={
            "operation": "root",
            "error": str(e),
            "duration_ms": round(duration, 2),
            "status": "error"
        })

        return jsonify({
            'error': 'Failed to retrieve API information',
            'timestamp': datetime.now().isoformat()
        }), 500

# Error handlers
@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors"""
    logger.warning("404 error occurred", extra={
        "operation": "error_handler",
        "error_code": 404,
        "path": request.path,
        "method": request.method
    })
    return jsonify({
        'error': 'Endpoint not found',
        'path': request.path,
        'method': request.method,
        'timestamp': datetime.now().isoformat()
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    logger.error("500 error occurred", extra={
        "operation": "error_handler",
        "error_code": 500,
        "path": request.path,
        "method": request.method,
        "error": str(error)
    })
    return jsonify({
        'error': 'Internal server error',
        'timestamp': datetime.now().isoformat()
    }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') == 'development'

    logger.info("Starting Flask server", extra={
        "operation": "server_start",
        "port": port,
        "debug_mode": debug,
        "environment": os.environ.get('FLASK_ENV', 'production')
    })

    print(f"Starting Flask server on port {port}")
    print(f"Debug mode: {debug}")
    print(f"Health: http://localhost:{port}/health")

    app.run(host='0.0.0.0', port=port, debug=debug)
