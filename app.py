"""Janus Gate: a small user authentication API."""

import os
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
from pydantic import ValidationError

from core.auth_service import decode_access_token, login_user, register_user, verify_password
from core.nutrition_service import MealRecommendationInput, NutritionEntryInput
from services.firebase_service import (
    create_nutrition_entry,
    delete_ai_credential,
    delete_nutrition_entry,
    delete_user_account,
    get_database_status,
    get_ai_credential,
    get_ai_credential_status,
    get_ai_selection,
    get_user_record,
    get_user_record_for_account_deletion,
    list_nutrition_entries,
    save_ai_credential,
    save_ai_selection,
    update_nutrition_entry,
)
from services.credential_service import (
    CredentialConfigurationError,
    CredentialEncryptionError,
    decrypt_api_key,
    encrypt_api_key,
)
from services.ai_catalog import (
    SUPPORTED_PROVIDERS,
    is_supported_model,
    is_supported_provider,
    provider_name,
    public_provider_catalog,
)
from services.firebase.account_state import account_id_matches
from services.ai_errors import (
    AIAuthenticationError,
    AIRateLimitError,
    AIServiceError,
)
from services.ai_service import (
    analyze_meal,
    recommend_meals,
    validate_provider_api_key as validate_api_key,
)
from services.logging_service import get_flask_app_logger
from services.ai_contract import MAX_MEAL_MESSAGE_LENGTH


logger = get_flask_app_logger()
app = Flask(__name__)

CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Authorization", "Content-Type"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)


def _bearer_token():
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return None


def _authenticated_identity():
    identity = decode_access_token(_bearer_token())
    if not identity:
        return None
    user = get_user_record(identity["email"])
    if not user or not account_id_matches(identity["account_id"], user):
        return None
    return identity


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
    if not isinstance(data, dict):
        return jsonify(status="error", error="Invalid request"), 400
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
    if not isinstance(data, dict):
        return jsonify(status="error", error="Invalid email or password"), 401
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
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    return jsonify(status="success", user={"email": identity["email"]})


@app.delete("/api/auth/account")
def auth_delete_account():
    identity = decode_access_token(_bearer_token())
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]
    user = get_user_record_for_account_deletion(email)
    if (
        not user
        or not account_id_matches(identity["account_id"], user)
    ):
        return jsonify(status="error", error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(status="error", error="Invalid request"), 400
    password = data.get("password")
    if not isinstance(password, str) or not password:
        return jsonify(status="error", error="Password is required"), 400

    if not verify_password(password, user.get("password_hash")):
        return jsonify(status="error", error="Invalid password"), 401

    deleted, error = delete_user_account(email, identity["account_id"])
    if not deleted:
        logger.error("Could not delete account %s: %s", email, error)
        return jsonify(status="error", error="Could not delete account"), 500

    return jsonify(status="success")


def _credential_error(provider, error, message, status_code, compatibility=False):
    if compatibility:
        error = {
            "provider_key_invalid": "openai_key_invalid",
            "provider_rate_limited": "openai_rate_limited",
            "provider_unavailable": "openai_unavailable",
        }.get(error, error)
    payload = {
        "status": "error",
        "error": error,
        "message": message,
    }
    if not compatibility:
        payload["provider"] = provider
    return jsonify(payload), status_code


def _put_ai_credential(provider, compatibility=False):
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="unauthorized"), 401
    email = identity["email"]
    if not is_supported_provider(provider):
        return jsonify(status="error", error="invalid_provider"), 400

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(status="error", error="invalid_request"), 400
    try:
        api_key = validate_api_key(provider, data.get("api_key"), email)
        ciphertext = encrypt_api_key(
            api_key, email, provider=provider, aad_version=2
        )
    except ValueError:
        display_name = provider_name(provider)
        return _credential_error(
            provider,
            "invalid_api_key",
            f"A valid {display_name} API key is required",
            400,
            compatibility,
        )
    except AIAuthenticationError:
        display_name = provider_name(provider)
        return _credential_error(
            provider,
            "provider_key_invalid",
            f"{display_name} rejected this API key",
            422,
            compatibility,
        )
    except AIRateLimitError:
        return _credential_error(
            provider,
            "provider_rate_limited",
            "The provider could not verify the key because it is rate limited",
            429,
            compatibility,
        )
    except AIServiceError:
        return _credential_error(
            provider,
            "provider_unavailable",
            "The provider could not verify the key",
            502,
            compatibility,
        )
    except (CredentialConfigurationError, CredentialEncryptionError) as error:
        logger.error("Credential encryption unavailable: %s", type(error).__name__)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503

    saved, error, credential_status = save_ai_credential(
        email,
        provider,
        ciphertext,
        api_key[-4:],
        aad_version=2,
        account_id=identity["account_id"],
    )
    if not saved:
        logger.error(
            "Credential save failed for %s (%s): %s", email, provider, error
        )
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    payload = {"status": "success", "credential": credential_status}
    if not compatibility:
        payload["provider"] = provider
    return jsonify(payload)


def _get_ai_credential_status_response(provider, compatibility=False):
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="unauthorized"), 401
    email = identity["email"]
    if not is_supported_provider(provider):
        return jsonify(status="error", error="invalid_provider"), 400

    ok, error, credential_status = get_ai_credential_status(
        email,
        provider,
        identity["account_id"],
    )
    if not ok:
        logger.error(
            "Credential status failed for %s (%s): %s", email, provider, error
        )
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    payload = {
        "status": "success",
        "credential": credential_status or {"configured": False},
    }
    if not compatibility:
        payload["provider"] = provider
    return jsonify(payload)


def _delete_ai_credential_response(provider, compatibility=False):
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="unauthorized"), 401
    email = identity["email"]
    if not is_supported_provider(provider):
        return jsonify(status="error", error="invalid_provider"), 400

    deleted, error = delete_ai_credential(
        email,
        provider,
        identity["account_id"],
    )
    if not deleted:
        logger.error(
            "Credential delete failed for %s (%s): %s", email, provider, error
        )
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    payload = {"status": "success"}
    if not compatibility:
        payload["provider"] = provider
    return jsonify(payload)


@app.put("/api/user/ai-credentials/<provider>")
def ai_credential_put(provider):
    return _put_ai_credential(provider)


@app.get("/api/user/ai-credentials/<provider>")
def ai_credential_get(provider):
    return _get_ai_credential_status_response(provider)


@app.delete("/api/user/ai-credentials/<provider>")
def ai_credential_delete(provider):
    return _delete_ai_credential_response(provider)


@app.put("/api/user/openai-key")
def openai_key_put():
    return _put_ai_credential("openai", compatibility=True)


@app.get("/api/user/openai-key")
def openai_key_get():
    return _get_ai_credential_status_response("openai", compatibility=True)


@app.delete("/api/user/openai-key")
def openai_key_delete():
    return _delete_ai_credential_response("openai", compatibility=True)


@app.get("/api/user/ai-settings")
def ai_settings_get():
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="unauthorized"), 401
    email = identity["email"]

    ok, error, selection = get_ai_selection(
        email,
        identity["account_id"],
    )
    if not ok:
        logger.error("AI settings read failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error="settings_service_unavailable",
            message="AI settings are unavailable",
        ), 503

    credential_statuses = {}
    for provider in SUPPORTED_PROVIDERS:
        credential_ok, credential_error, credential = get_ai_credential_status(
            email,
            provider,
            identity["account_id"],
        )
        if not credential_ok:
            logger.error(
                "Credential status failed for %s (%s): %s",
                email,
                provider,
                credential_error,
            )
            return jsonify(
                status="error",
                error="credential_service_unavailable",
                message="Secure credential storage is unavailable",
            ), 503
        credential_statuses[provider] = credential or {"configured": False}

    providers = public_provider_catalog()
    for provider in providers:
        provider["credential"] = credential_statuses[provider["id"]]

    return jsonify(
        status="success",
        selection=selection,
        providers=providers,
    )


@app.put("/api/user/ai-settings")
def ai_settings_put():
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="unauthorized"), 401
    email = identity["email"]

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(status="error", error="invalid_request"), 400
    provider = data.get("provider")
    model = data.get("model")
    if not is_supported_provider(provider):
        return jsonify(status="error", error="invalid_provider"), 400
    if not is_supported_model(provider, model):
        return jsonify(status="error", error="invalid_model"), 400

    saved, error, selection = save_ai_selection(
        email,
        provider,
        model,
        identity["account_id"],
    )
    if not saved:
        logger.error("AI settings save failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error="settings_service_unavailable",
            message="AI settings could not be saved",
        ), 503
    return jsonify(status="success", selection=selection)


def _selected_ai_credential(email, account_id):
    ok, error, selection = get_ai_selection(email, account_id)
    if not ok:
        return None, None, "settings_service_unavailable"

    provider = selection["provider"]
    ok, error, credential = get_ai_credential(
        email,
        provider,
        account_id,
    )
    if not ok:
        return selection, None, "credential_service_unavailable"
    return selection, credential, None


def _stored_credential_aad_version(provider, credential):
    aad_version = credential.get("aad_version")
    if aad_version is None:
        return 1 if provider == "openai" else 2
    return aad_version


@app.post("/api/nutrition/analyze")
def nutrition_analyze():
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(status="error", error="Message is required"), 400
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify(status="error", error="Message is required"), 400
    if len(message.strip()) > MAX_MEAL_MESSAGE_LENGTH:
        return jsonify(status="error", error="Message must be 2000 characters or fewer"), 400

    selection, credential, error = _selected_ai_credential(
        email,
        identity["account_id"],
    )
    if error:
        logger.error("Credential read failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error=error,
            message=(
                "AI settings are unavailable"
                if error == "settings_service_unavailable"
                else "Secure credential storage is unavailable"
            ),
        ), 503
    if not credential:
        provider = selection["provider"]
        return jsonify(
            status="error",
            error="provider_key_required",
            provider=provider,
            message=(
                f"Add your {provider_name(provider)} API key before analyzing meals"
            ),
        ), 409

    provider = selection["provider"]
    model = selection["model"]
    try:
        api_key = decrypt_api_key(
            credential.get("ciphertext", ""),
            email,
            provider=provider,
            aad_version=_stored_credential_aad_version(provider, credential),
        )
        analysis = analyze_meal(message, email, api_key, provider, model)
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
    except AIAuthenticationError:
        return jsonify(
            status="error",
            error="provider_key_invalid",
            provider=provider,
            message=f"Your {provider_name(provider)} API key is no longer valid",
        ), 422
    except AIRateLimitError:
        return jsonify(
            status="error",
            error="provider_rate_limited",
            provider=provider,
            message="Meal analysis is temporarily busy",
        ), 429
    except AIServiceError as error:
        logger.error("Meal analysis failed: %s", error)
        return jsonify(
            status="error",
            error="provider_unavailable",
            provider=provider,
            message="Meal analysis failed",
        ), 502

    return jsonify(status="success", analysis=analysis)


@app.post("/api/nutrition/recommend")
def nutrition_recommend():
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]

    try:
        recommendation_input = MealRecommendationInput.model_validate(
            request.get_json(silent=True) or {}
        )
    except ValidationError:
        return jsonify(
            status="error",
            error="invalid_recommendation_request",
            message="Enter valid nutrition targets and choose one to three meals",
        ), 400

    selection, credential, error = _selected_ai_credential(
        email,
        identity["account_id"],
    )
    if error:
        logger.error("Credential read failed for %s: %s", email, error)
        return jsonify(
            status="error",
            error=error,
            message=(
                "AI settings are unavailable"
                if error == "settings_service_unavailable"
                else "Secure credential storage is unavailable"
            ),
        ), 503
    if not credential:
        provider = selection["provider"]
        return jsonify(
            status="error",
            error="provider_key_required",
            provider=provider,
            message=(
                f"Add your {provider_name(provider)} API key before requesting "
                "meal recommendations"
            ),
        ), 409

    provider = selection["provider"]
    model = selection["model"]
    try:
        api_key = decrypt_api_key(
            credential.get("ciphertext", ""),
            email,
            provider=provider,
            aad_version=_stored_credential_aad_version(provider, credential),
        )
        recommendation = recommend_meals(
            recommendation_input, email, api_key, provider, model
        )
    except (CredentialConfigurationError, CredentialEncryptionError) as error:
        logger.error("Credential decryption unavailable: %s", type(error).__name__)
        return jsonify(
            status="error",
            error="credential_service_unavailable",
            message="Secure credential storage is unavailable",
        ), 503
    except AIAuthenticationError:
        return jsonify(
            status="error",
            error="provider_key_invalid",
            provider=provider,
            message=f"Your {provider_name(provider)} API key is no longer valid",
        ), 422
    except AIRateLimitError:
        return jsonify(
            status="error",
            error="provider_rate_limited",
            provider=provider,
            message="Meal recommendations are temporarily busy",
        ), 429
    except AIServiceError as error:
        logger.error("Meal recommendation failed: %s", error)
        return jsonify(
            status="error",
            error="provider_unavailable",
            provider=provider,
            message="Meal recommendation failed",
        ), 502

    return jsonify(status="success", recommendation=recommendation)


@app.post("/api/nutrition/entries")
def nutrition_entries_create():
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]

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
        identity["account_id"],
    )
    if not created:
        logger.error("Nutrition entry create failed for %s: %s", email, error)
        return jsonify(status="error", error="Could not save nutrition entry"), 500
    return jsonify(status="success", entry=entry), 201


@app.get("/api/nutrition/entries")
def nutrition_entries_list():
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]

    date_value = None
    date_text = request.args.get("date")
    all_text = request.args.get("all")
    if all_text not in (None, "true"):
        return jsonify(
            status="error",
            error="All must be true when supplied",
        ), 400
    all_entries_requested = all_text == "true"
    if date_text:
        try:
            date_value = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            return jsonify(status="error", error="Date must use YYYY-MM-DD"), 400

    start_value = None
    end_value = None
    start_text = request.args.get("start")
    end_text = request.args.get("end")
    if all_entries_requested and (
        date_text
        or start_text
        or end_text
        or request.args.get("limit") is not None
    ):
        return jsonify(
            status="error",
            error="All cannot be combined with date, start/end, or limit",
        ), 400
    if bool(start_text) != bool(end_text):
        return jsonify(
            status="error",
            error="Start and end must be supplied together",
        ), 400
    if date_text and start_text:
        return jsonify(
            status="error",
            error="Use either date or start/end, not both",
        ), 400
    if start_text and end_text:
        try:
            start_value = datetime.fromisoformat(
                start_text.replace("Z", "+00:00")
            )
            end_value = datetime.fromisoformat(
                end_text.replace("Z", "+00:00")
            )
            if (
                start_value.tzinfo is None
                or start_value.utcoffset() is None
                or end_value.tzinfo is None
                or end_value.utcoffset() is None
            ):
                raise ValueError
            start_value = start_value.astimezone(timezone.utc)
            end_value = end_value.astimezone(timezone.utc)
        except ValueError:
            return jsonify(
                status="error",
                error="Start and end must be timezone-aware ISO-8601 datetimes",
            ), 400
        if end_value <= start_value:
            return jsonify(
                status="error",
                error="End must be later than start",
            ), 400
        if end_value - start_value > timedelta(days=8):
            return jsonify(
                status="error",
                error="Date range cannot exceed eight days",
            ), 400

    if all_entries_requested:
        limit = None
    else:
        range_requested = start_value is not None
        default_limit = "500" if range_requested else "50"
        maximum_limit = 500 if range_requested else 100
        try:
            limit = min(
                max(int(request.args.get("limit", default_limit)), 1),
                maximum_limit,
            )
        except ValueError:
            return jsonify(status="error", error="Limit must be a number"), 400

    ok, error, entries = list_nutrition_entries(
        email=email,
        date_value=date_value,
        limit=None if all_entries_requested else limit + 1,
        account_id=identity["account_id"],
        start_value=start_value,
        end_value=end_value,
    )
    if not ok:
        logger.error("Nutrition entry list failed for %s: %s", email, error)
        return jsonify(status="error", error="Could not load nutrition entries"), 500
    truncated = False if all_entries_requested else len(entries) > limit
    response_entries = entries if all_entries_requested else entries[:limit]
    return jsonify(
        status="success",
        entries=response_entries,
        pagination={
            "start": start_value.isoformat() if start_value else None,
            "end": end_value.isoformat() if end_value else None,
            "limit": limit,
            "truncated": truncated,
        },
    )


@app.delete("/api/nutrition/entries/<entry_id>")
def nutrition_entry_delete(entry_id):
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]

    deleted, error = delete_nutrition_entry(
        email,
        entry_id,
        identity["account_id"],
    )
    if error == "invalid_entry_id":
        return jsonify(status="error", error="Invalid nutrition entry ID"), 400
    if error == "not_found":
        return jsonify(status="error", error="Nutrition entry not found"), 404
    if not deleted:
        logger.error("Nutrition entry delete failed for %s: %s", email, error)
        return jsonify(status="error", error="Could not delete nutrition entry"), 500
    return jsonify(status="success")


@app.put("/api/nutrition/entries/<entry_id>")
def nutrition_entry_update(entry_id):
    identity = _authenticated_identity()
    if not identity:
        return jsonify(status="error", error="Unauthorized"), 401
    email = identity["email"]

    try:
        entry_input = NutritionEntryInput.model_validate(request.get_json(silent=True) or {})
    except ValidationError:
        return jsonify(status="error", error="Invalid nutrition entry"), 400

    updated, error, entry = update_nutrition_entry(
        email,
        entry_id,
        [item.model_dump() for item in entry_input.items],
        entry_input.eaten_at,
        entry_input.source_message,
        identity["account_id"],
    )
    if error == "invalid_entry_id":
        return jsonify(status="error", error="Invalid nutrition entry ID"), 400
    if error == "not_found":
        return jsonify(status="error", error="Nutrition entry not found"), 404
    if not updated:
        logger.error("Nutrition entry update failed for %s: %s", email, error)
        return jsonify(status="error", error="Could not update nutrition entry"), 500
    return jsonify(status="success", entry=entry)


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
            "get_ai_settings": "GET /api/user/ai-settings",
            "set_ai_settings": "PUT /api/user/ai-settings",
            "set_ai_credential": "PUT /api/user/ai-credentials/{provider}",
            "get_ai_credential_status": "GET /api/user/ai-credentials/{provider}",
            "delete_ai_credential": "DELETE /api/user/ai-credentials/{provider}",
            "set_openai_key": "PUT /api/user/openai-key",
            "get_openai_key_status": "GET /api/user/openai-key",
            "delete_openai_key": "DELETE /api/user/openai-key",
            "analyze_meal": "POST /api/nutrition/analyze",
            "recommend_meals": "POST /api/nutrition/recommend",
            "create_nutrition_entry": "POST /api/nutrition/entries",
            "list_nutrition_entries": "GET /api/nutrition/entries",
            "delete_nutrition_entry": "DELETE /api/nutrition/entries/{entry_id}",
            "update_nutrition_entry": "PUT /api/nutrition/entries/{entry_id}",
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
