"""Publication boundary for approved schedules.

NOT AVAILABLE: no ERP/MES integration is documented, configured or installed in this project, so none is implemented and
publication is never claimed. To add one: implement `Publisher.publish` against the verified ERP/MES API (endpoint, auth,
payload, idempotency/rollback behaviour) and return it from `get_publisher()`. Until then use the labelled export endpoint.
"""
from typing import Protocol
from .domain import Schedule
from .integrations import IntegrationUnavailable, NotConfigured


class Publisher(Protocol):
    def publish(self, schedule: Schedule, dispatch: list[dict]) -> dict:
        """Send an APPROVED schedule to the external system. Return a receipt dict; raise IntegrationUnavailable if unreachable."""


class NoConnector:
    def publish(self, schedule: Schedule, dispatch: list[dict]) -> dict:
        raise NotConfigured("no verified ERP/MES connector exists in this project")


def get_publisher() -> Publisher:
    return NoConnector()


def status() -> str:
    return "NOT_AVAILABLE (no verified ERP/MES integration); use GET /api/schedules/{id}/export"


__all__ = ["Publisher", "NoConnector", "get_publisher", "status", "IntegrationUnavailable"]
