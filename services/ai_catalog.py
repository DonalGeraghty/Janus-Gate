"""Server-owned AI provider and model catalog."""

import os


PROVIDER_CATALOG = (
    {
        "id": "openai",
        "name": "OpenAI",
        "models": (
            {
                "id": "gpt-5.6-sol",
                "name": "GPT-5.6 Sol",
                "description": "OpenAI's most capable model for complex meal analysis.",
            },
            {
                "id": "gpt-5.6-terra",
                "name": "GPT-5.6 Terra",
                "description": "A balanced OpenAI model for everyday nutrition requests.",
            },
            {
                "id": "gpt-5.6-luna",
                "name": "GPT-5.6 Luna",
                "description": "A fast OpenAI model for straightforward nutrition requests.",
            },
        ),
    },
    {
        "id": "mistral",
        "name": "Mistral AI",
        "models": (
            {
                "id": "mistral-small-2603",
                "name": "Mistral Small 4",
                "description": "Mistral's fast default model for everyday nutrition requests.",
            },
            {
                "id": "mistral-large-2512",
                "name": "Mistral Large 3",
                "description": "A strong general-purpose Mistral model for quality and value.",
            },
            {
                "id": "mistral-medium-3-5",
                "name": "Mistral Medium 3.5",
                "description": "Mistral's premium frontier model for demanding analysis.",
            },
        ),
    },
)

SUPPORTED_PROVIDERS = tuple(provider["id"] for provider in PROVIDER_CATALOG)
_MODELS_BY_PROVIDER = {
    provider["id"]: tuple(model["id"] for model in provider["models"])
    for provider in PROVIDER_CATALOG
}
_FALLBACK_MODELS = {
    "openai": "gpt-5.6-sol",
    "mistral": "mistral-small-2603",
}
_MODEL_ENVIRONMENT_VARIABLES = {
    "openai": "OPENAI_MODEL",
    "mistral": "MISTRAL_MODEL",
}


def is_supported_provider(provider):
    return isinstance(provider, str) and provider in SUPPORTED_PROVIDERS


def is_supported_model(provider, model):
    return (
        is_supported_provider(provider)
        and isinstance(model, str)
        and model in _MODELS_BY_PROVIDER[provider]
    )


def default_model(provider):
    if not is_supported_provider(provider):
        raise ValueError("invalid_provider")
    configured_model = os.environ.get(_MODEL_ENVIRONMENT_VARIABLES[provider])
    if is_supported_model(provider, configured_model):
        return configured_model
    return _FALLBACK_MODELS[provider]


def default_selection():
    return {"provider": "openai", "model": default_model("openai")}


def selection_from_user_record(data):
    data = data or {}
    provider = data.get("ai_provider")
    model = data.get("ai_model")

    if provider is None:
        return default_selection()
    if not is_supported_provider(provider):
        return default_selection()
    if model is None:
        return {"provider": provider, "model": default_model(provider)}
    if not is_supported_model(provider, model):
        return {"provider": provider, "model": default_model(provider)}
    return {"provider": provider, "model": model}


def public_provider_catalog():
    return [
        {
            "id": provider["id"],
            "name": provider["name"],
            "models": [dict(model) for model in provider["models"]],
        }
        for provider in PROVIDER_CATALOG
    ]
