"""Provider-neutral dispatch for AI-backed product operations."""

from importlib import import_module

from .ai_catalog import default_model, is_supported_model, is_supported_provider
from .ai_errors import (
    AIAuthenticationError,
    AIAuthorizationError,
    AIBillingError,
    AIRateLimitError,
    AIServiceError,
)


_PROVIDER_ADAPTERS = {
    "openai": {
        "module": ".openai_service",
        "authentication_error": "OpenAIAuthenticationError",
        "authorization_error": "OpenAIAuthorizationError",
        "billing_error": "OpenAIBillingError",
        "rate_limit_error": "OpenAIRateLimitError",
        "service_error": "OpenAIServiceError",
    },
    "mistral": {
        "module": ".mistral_service",
        "authentication_error": "MistralAuthenticationError",
        "authorization_error": "MistralAuthorizationError",
        "billing_error": "MistralBillingError",
        "rate_limit_error": "MistralRateLimitError",
        "service_error": "MistralServiceError",
    },
    "anthropic": {
        "module": ".anthropic_service",
        "authentication_error": "AnthropicAuthenticationError",
        "authorization_error": "AnthropicAuthorizationError",
        "billing_error": "AnthropicBillingError",
        "rate_limit_error": "AnthropicRateLimitError",
        "service_error": "AnthropicServiceError",
    },
}


def _validated_model(provider, model):
    model = model or default_model(provider)
    if not is_supported_model(provider, model):
        raise ValueError("invalid_model")
    return model


def _provider_adapter(provider):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    try:
        specification = _PROVIDER_ADAPTERS[provider]
    except KeyError as error:
        raise ValueError("invalid_provider") from error
    return import_module(specification["module"], package=__package__), specification


def _call_provider(provider, operation, *args, **kwargs):
    adapter, specification = _provider_adapter(provider)
    authentication_error = getattr(
        adapter, specification["authentication_error"]
    )
    authorization_error = getattr(
        adapter, specification["authorization_error"]
    )
    billing_error = getattr(adapter, specification["billing_error"])
    rate_limit_error = getattr(adapter, specification["rate_limit_error"])
    service_error = getattr(adapter, specification["service_error"])

    try:
        return getattr(adapter, operation)(*args, **kwargs)
    except authentication_error as error:
        raise AIAuthenticationError("Provider API key is invalid") from error
    except authorization_error as error:
        raise AIAuthorizationError("Provider API access is denied") from error
    except billing_error as error:
        raise AIBillingError("Provider billing or credit is required") from error
    except rate_limit_error as error:
        raise AIRateLimitError("Provider rate limit reached") from error
    except service_error as error:
        raise AIServiceError("Provider request failed") from error


def validate_provider_api_key(provider, api_key, email=None):
    return _call_provider(
        provider,
        "validate_api_key",
        api_key,
        email=email,
    )


def analyze_meal(message, email, api_key, provider="openai", model=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    model = _validated_model(provider, model)
    return _call_provider(
        provider,
        "analyze_meal",
        message,
        email,
        api_key,
        model,
    )


def analyze_workout(message, email, api_key, provider="openai", model=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    model = _validated_model(provider, model)
    return _call_provider(
        provider,
        "analyze_workout",
        message,
        email,
        api_key,
        model,
    )


def recommend_meals(context, email, api_key, provider="openai", model=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    model = _validated_model(provider, model)
    return _call_provider(
        provider,
        "recommend_meals",
        context,
        email,
        api_key,
        model,
    )


def respond_minerva(message, email, api_key, provider="openai", model=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    model = _validated_model(provider, model)
    return _call_provider(
        provider,
        "respond_minerva",
        message,
        email,
        api_key,
        model,
    )
