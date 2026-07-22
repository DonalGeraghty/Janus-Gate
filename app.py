"""Janus Gate: a small user authentication API."""

import os
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

from core.auth_service import decode_access_token, login_user, register_user, verify_password
from services.firebase_service import delete_user_account, get_database_status, get_user_record
from services.logging_service import get_flask_app_logger


logger = get_flask_app_logger()
app = Flask(__name__)
CORS(app)


def _bearer_token():
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return None


def _authenticated_email():
    return decode_access_token(_bearer_token())


@app.before_request
def start_request_timer():
    request.start_time = time.monotonic()


@app.after_request
def log_response(response):
    duration_ms = (time.monotonic() - request.start_time) * 1000
    logger.info(
        "%s %s %s %.2fms",
        request.method,
        request.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.post("/api/auth/register")
def auth_register():
    data = request.get_json(silent=True) or {}
    payload, error, _ = register_user(data.get("email"), data.get("password"))

    if error == "invalid_email":
        return jsonify(status="error", error="Invalid email"), 400
    if error == "weak_password":
        return jsonify(status="error", error="Password must be at least 8 characters"), 400
    if error == "exists":
        return jsonify(status="error", error="An account with this email already exists"), 409
    if error:
        return jsonify(status="error", error="Registration failed"), 500

    return jsonify(
        status="success",
        token=payload["token"],
        user={"email": payload["email"]},
    ), 201


@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}
    payload, error, _ = login_user(data.get("email"), data.get("password"))
    if error:
        return jsonify(status="error", error="Invalid email or password"), 401

    return jsonify(
        status="success",
        token=payload["token"],
        user={"email": payload["email"]},
    )


@app.get("/api/auth/me")
def auth_me():
    email = _authenticated_email()
    if not email or not get_user_record(email):
        return jsonify(status="error", error="Unauthorized"), 401
    return jsonify(status="success", user={"email": email})


@app.delete("/api/auth/account")
def auth_delete_account():
    email = _authenticated_email()
    if not email:
        return jsonify(status="error", error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    password = data.get("password")
    if not isinstance(password, str) or not password:
        return jsonify(status="error", error="Password is required"), 400

    user = get_user_record(email)
    if not user or not verify_password(password, user.get("password_hash")):
        return jsonify(status="error", error="Invalid password"), 401

    deleted, error = delete_user_account(email)
    if not deleted:
        logger.error("Could not delete account %s: %s", email, error)
        return jsonify(status="error", error="Could not delete account"), 500

    return jsonify(status="success")


@app.get("/health")
def health_check():
    return jsonify(status="healthy", database=get_database_status())


@app.get("/")
def root():
    return jsonify(
        service="Janus Gate",
        endpoints={
            "register": "POST /api/auth/register",
            "login": "POST /api/auth/login",
            "current_user": "GET /api/auth/me",
            "delete_account": "DELETE /api/auth/account",
            "health": "GET /health",
        },
    )


@app.errorhandler(404)
def not_found(_error):
    return jsonify(status="error", error="Not found"), 404


@app.errorhandler(500)
def internal_error(error):
    logger.exception("Unhandled server error: %s", error)
    return jsonify(status="error", error="Internal server error"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
