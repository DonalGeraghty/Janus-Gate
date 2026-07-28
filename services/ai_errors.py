"""Provider-neutral AI service exceptions."""


class AIAuthenticationError(RuntimeError):
    pass


class AIRateLimitError(RuntimeError):
    pass


class AIServiceError(RuntimeError):
    pass
