import os
from datetime import datetime, timedelta, timezone

import jwt
from werkzeug.security import check_password_hash, generate_password_hash

from services.firebase_service import create_user_record, get_user_record, initialize_firebase
from services.logging_service import logger

initialize_firebase()

MIN_PASSWORD_LENGTH = 8
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7


def _jwt_secret():
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret:
        secret = "dev-only-insecure-jwt-secret"
        logger.warning("JWT_SECRET_KEY not set; using insecure default", extra={
            "operation": "jwt_secret",
        })
    return secret


def hash_password(plain_password):
    """One-way password hash for storage (not reversible encryption)."""
    return generate_password_hash(plain_password)


def verify_password(plain_password, password_hash):
    if not password_hash:
        return False
    return check_password_hash(password_hash, plain_password)


def create_access_token(email):
    email_norm = (email or "").strip().lower()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email_norm,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token):
    if not token:
        return None
    try:
        data = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        sub = data.get("sub")
        return sub if isinstance(sub, str) else None
    except jwt.PyJWTError as e:
        logger.info("JWT decode failed", extra={
            "operation": "decode_access_token",
            "error": str(e),
        })
        return None


def register_user(email, password):
    if not isinstance(email, str):
        return None, "invalid_email", None
    if not isinstance(password, str):
        return None, "weak_password", None
    email_norm = (email or "").strip().lower()
    if not email_norm or "@" not in email_norm or len(email_norm) > 320:
        return None, "invalid_email", None
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return None, "weak_password", None

    pw_hash = hash_password(password)
    ok, err = create_user_record(email_norm, pw_hash)
    if not ok:
        return None, err or "exists", None

    token = create_access_token(email_norm)
    return {"email": email_norm, "token": token}, None, None


def login_user(email, password):
    if not isinstance(email, str) or not isinstance(password, str):
        return None, "invalid_credentials", None
    email_norm = (email or "").strip().lower()
    if not email_norm or not password:
        return None, "invalid_credentials", None

    row = get_user_record(email_norm)
    if not row or not verify_password(password, row.get("password_hash")):
        return None, "invalid_credentials", None

    token = create_access_token(email_norm)
    return {"email": email_norm, "token": token}, None, None
