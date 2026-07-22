"""Janus Gate: a small user authentication API."""

import os
import time
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from pydantic import ValidationError

from core.auth_service import decode_access_token, login_user, register_user, verify_password
from core.nutrition_service import NutritionEntryInput
from services.firebase_service import (
    create_nutrition_entry,
    delete_openai_credential,
    delete_user_account,
    get_database_status,
    get_openai_credential,
    get_openai_credential_status,
    get_user_record,
    list_nutrition_entries,
    save_openai_credential,
)
from services.credential_service import (
    CredentialConfigurationError,
    CredentialEncryptionError,
    decrypt_api_key,
    encrypt_api_key,
)
from services.logging_service import get_flask_app_logger
from services.openai_service import (
    MAX_MEAL_MESSAGE_LENGTH,
    OpenAIAuthenticationError,
    OpenAIRateLimitError,
    OpenAIServiceError,
    analyze_meal,
    validate_api_key,
)


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


def _authenticated_user_email():
    email = _authenticated_email()
    if not email or not get_user_record(email):
        return None
    return email


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
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="Unauthorized"), 401
    return jsonify(status="success", user={"email": email})


@app.delete("/api/auth/account")
def auth_delete_account():
    email = _authenticated_user_email()
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


@app.put("/api/user/openai-key")
def openai_key_put():
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="unauthorized"), 401

    data = request.get_json(silent=True) or {}
    try:
        api_key = validate_api_key(data.get("api_key"), email)
        ciphertext = encrypt_api_key(api_key, email)
    except ValueError:
        return jsonify(
            status="error",
            error="invalid_api_key",
            message="A valid OpenAI API key is required",
        ), 400
    except OpenAIAuthenticationError:
        return jsonify(
            status="error",
            error="openai_key_invalid",
            message="OpenAI rejected this API key",
        ), 422
    except OpenAIRateLimitError:
        return jsonify(
            status="error",
            error="openai_rate_limited",
            message="OpenAI could not verify the key because it is rate limited",
        ), 429
    except OpenAIServiceError:
        return jsonify(
            status="error",
            error="openai_unavailable",
            message="OpenAI could not verify the key",
        ), 502
    except (CredentialConfigurationError, CredentialEncryptionError) as error:
        logger.error("Credential encryption unavailable: %s", type(error).__name__)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503

    saved, error, credential_status = save_openai_credential(
        email, ciphertext, api_key[-4:]
    )
    if not saved:
        logger.error("Credential save failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    return jsonify(status="success", credential=credential_status)


@app.get("/api/user/openai-key")
def openai_key_get():
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="unauthorized"), 401

    ok, error, credential_status = get_openai_credential_status(email)
    if not ok:
        logger.error("Credential status failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    return jsonify(
        status="success",
        credential=credential_status or {"configured": False},
    )


@app.delete("/api/user/openai-key")
def openai_key_delete():
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="unauthorized"), 401

    deleted, error = delete_openai_credential(email)
    if not deleted:
        logger.error("Credential delete failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    return jsonify(status="success")


@app.post("/api/nutrition/analyze")
def nutrition_analyze():
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify(status="error", error="Message is required"), 400
    if len(message.strip()) > MAX_MEAL_MESSAGE_LENGTH:
        return jsonify(status="error", error="Message must be 2000 characters or fewer"), 400

    ok, error, credential = get_openai_credential(email)
    if not ok:
        logger.error("Credential read failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    if not credential:
        return jsonify(
            status="error",
            error="openai_key_required",
            message="Add an OpenAI API key before analyzing meals",
        ), 409

    try:
        api_key = decrypt_api_key(credential.get("ciphertext", ""), email)
        analysis = analyze_meal(message, email, api_key)
    except ValueError as error:
        message = (
            "Message must be 2000 characters or fewer"
            if str(error) == "message_too_long"
            else "Message is required"
        )
        return jsonify(status="error", error=message), 400
    except (CredentialConfigurationError, CredentialEncryptionError) as error:
        logger.error("Credential decryption unavailable: %s", type(error).__name__)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    except OpenAIAuthenticationError:
        return jsonify(
            status="error",
            error="openai_key_invalid",
            message="Your OpenAI API key is no longer valid",
        ), 422
    except OpenAIRateLimitError:
        return jsonify(status="error", error="Meal analysis is temporarily busy"), 429
    except OpenAIServiceError as error:
        logger.error("Meal analysis failed: %s", error)
        return jsonify(status="error", error="Meal analysis failed"), 502

    return jsonify(status="success", analysis=analysis)


@app.post("/api/nutrition/entries")
def nutrition_entries_create():
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="Unauthorized"), 401

    try:
        entry_input = NutritionEntryInput.model_validate(request.get_json(silent=True) or {})
    except ValidationError:
        return jsonify(status="error", error="Invalid nutrition entry"), 400

    items = [item.model_dump() for item in entry_input.items]
    created, error, entry = create_nutrition_entry(
        email,
        items,
        entry_input.eaten_at,
        entry_input.source_message,
    )
    if not created:
        logger.error("Nutrition entry create failed for %s: %s", email, error)
        return jsonify(status="error", error="Could not save nutrition entry"), 500
    return jsonify(status="success", entry=entry), 201


@app.get("/api/nutrition/entries")
def nutrition_entries_list():
    email = _authenticated_user_email()
    if not email:
        return jsonify(status="error", error="Unauthorized"), 401

    date_value = None
    date_text = request.args.get("date")
    if date_text:
        try:
            date_value = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            return jsonify(status="error", error="Date must use YYYY-MM-DD"), 400

    try:
        limit = min(max(int(request.args.get("limit", "50")), 1), 100)
    except ValueError:
        return jsonify(status="error", error="Limit must be a number"), 400

    ok, error, entries = list_nutrition_entries(email, date_value, limit)
    if not ok:
        logger.error("Nutrition entry list failed for %s: %s", email, error)
        return jsonify(status="error", error="Could not load nutrition entries"), 500
    return jsonify(status="success", entries=entries)


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
            "set_openai_key": "PUT /api/user/openai-key",
            "get_openai_key_status": "GET /api/user/openai-key",
            "delete_openai_key": "DELETE /api/user/openai-key",
            "analyze_meal": "POST /api/nutrition/analyze",
            "create_nutrition_entry": "POST /api/nutrition/entries",
            "list_nutrition_entries": "GET /api/nutrition/entries",
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
