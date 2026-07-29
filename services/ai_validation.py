"""Provider-neutral results for API credential validation."""

from dataclasses import dataclass


BILLING_REQUIRED = "provider_billing_required"


@dataclass(frozen=True)
class AICredentialValidation:
    """A normalized key plus any non-fatal provider warning."""

    api_key: str
    warning: str | None = None
