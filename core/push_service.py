"""Validation contracts for opt-in Web Push reminders."""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class PushSubscriptionKeys(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    p256dh: str = Field(min_length=20, max_length=512)
    auth: str = Field(min_length=8, max_length=256)

    @field_validator("p256dh", "auth")
    @classmethod
    def validate_base64url(cls, value):
        if not BASE64URL_RE.fullmatch(value):
            raise ValueError("Push key must use base64url encoding")
        return value


class PushSubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    endpoint: str = Field(min_length=8, max_length=4096)
    expirationTime: int | None = Field(default=None, ge=0)
    keys: PushSubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value):
        if not value.startswith("https://"):
            raise ValueError("Push endpoint must use HTTPS")
        return value


class PushSubscriptionDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    endpoint: str = Field(min_length=8, max_length=4096)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value):
        if not value.startswith("https://"):
            raise ValueError("Push endpoint must use HTTPS")
        return value


class PushSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool
    local_time: str = Field(default="20:00")
    timezone: str = Field(default="UTC", min_length=1, max_length=100)

    @field_validator("local_time")
    @classmethod
    def validate_local_time(cls, value):
        if not LOCAL_TIME_RE.fullmatch(value):
            raise ValueError("Time must use HH:MM")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Timezone must be a valid IANA timezone") from None
        return value
