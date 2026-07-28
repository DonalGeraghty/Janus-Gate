"""Provider-neutral dispatch for AI-backed nutrition operations."""

from .ai_catalog import default_model, is_supported_model, is_supported_provider
from .ai_errors import AIAuthenticationError, AIRateLimitError, AIServiceError
from . import openai_service


def _validated_model(provider, model):
    model = model or default_model(provider)
    if not is_supported_model(provider, model):
        raise ValueError("invalid_model")
    return model


def validate_provider_api_key(provider, api_key, email=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")

    if provider == "openai":
        try:
            return openai_service.validate_api_key(api_key, email)
        except openai_service.OpenAIAuthenticationError as error:
            raise AIAuthenticationError("Provider API key is invalid") from error
        except openai_service.OpenAIRateLimitError as error:
            raise AIRateLimitError("Provider rate limit reached") from error
        except openai_service.OpenAIServiceError as error:
            raise AIServiceError("Provider request failed") from error

    from . import mistral_service

    try:
        return mistral_service.validate_api_key(api_key, email=email)
    except mistral_service.MistralAuthenticationError as error:
        raise AIAuthenticationError("Provider API key is invalid") from error
    except mistral_service.MistralRateLimitError as error:
        raise AIRateLimitError("Provider rate limit reached") from error
    except mistral_service.MistralServiceError as error:
        raise AIServiceError("Provider request failed") from error


def analyze_meal(message, email, api_key, provider="openai", model=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    model = _validated_model(provider, model)

    if provider == "openai":
        try:
            return openai_service.analyze_meal(message, email, api_key, model)
        except openai_service.OpenAIAuthenticationError as error:
            raise AIAuthenticationError("Provider API key is invalid") from error
        except openai_service.OpenAIRateLimitError as error:
            raise AIRateLimitError("Provider rate limit reached") from error
        except openai_service.OpenAIServiceError as error:
            raise AIServiceError("Provider request failed") from error

    from . import mistral_service

    try:
        return mistral_service.analyze_meal(message, email, api_key, model)
    except mistral_service.MistralAuthenticationError as error:
        raise AIAuthenticationError("Provider API key is invalid") from error
    except mistral_service.MistralRateLimitError as error:
        raise AIRateLimitError("Provider rate limit reached") from error
    except mistral_service.MistralServiceError as error:
        raise AIServiceError("Provider request failed") from error


def recommend_meals(context, email, api_key, provider="openai", model=None):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    model = _validated_model(provider, model)

    if provider == "openai":
        try:
            return openai_service.recommend_meals(context, email, api_key, model)
        except openai_service.OpenAIAuthenticationError as error:
            raise AIAuthenticationError("Provider API key is invalid") from error
        except openai_service.OpenAIRateLimitError as error:
            raise AIRateLimitError("Provider rate limit reached") from error
        except openai_service.OpenAIServiceError as error:
            raise AIServiceError("Provider request failed") from error

    from . import mistral_service

    try:
        return mistral_service.recommend_meals(context, email, api_key, model)
    except mistral_service.MistralAuthenticationError as error:
        raise AIAuthenticationError("Provider API key is invalid") from error
    except mistral_service.MistralRateLimitError as error:
        raise AIRateLimitError("Provider rate limit reached") from error
    except mistral_service.MistralServiceError as error:
        raise AIServiceError("Provider request failed") from error
