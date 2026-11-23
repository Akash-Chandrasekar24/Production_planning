"""Errors shared by external services (guardrails, publishing, connectors). `code` is safe to show to users."""


class IntegrationUnavailable(Exception):
    """Service cannot be used right now."""
    code = "UNAVAILABLE"


class NotConfigured(IntegrationUnavailable):
    code = "NOT_CONFIGURED"


class NotVerified(IntegrationUnavailable):
    code = "NOT_VERIFIED"
