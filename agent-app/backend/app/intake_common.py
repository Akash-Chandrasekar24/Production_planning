"""Names shared by the intake modules: the upload's columns, the error raised for bad orders, and the plan's today."""
import datetime as dt
import logging

from . import store

log = logging.getLogger("planner.agent.intake")
NAME = "orchestrator"
CITY = {"IN-KA-01": "Bengaluru", "IN-TN-01": "Hosur"}
ALIASES = {"po_number": "customer_order_id", "po": "customer_order_id", "order_id": "customer_order_id", "qty": "order_qty", "quantity": "order_qty",
           "due": "requested_delivery_date", "due_date": "requested_delivery_date", "delivery_date": "requested_delivery_date", "customer": "customer_id",
           "product": "product_id", "price": "unit_price_inr"}
FIELDS = ["customer_order_id", "customer_id", "product_id", "order_qty", "requested_delivery_date", "priority", "channel", "unit_price_inr"]


class Invalid(ValueError):
    def __init__(self, msg, detail=None):
        super().__init__(msg)
        self.detail = detail


def today() -> dt.date:
    """The plan's today: the day after the last stock count."""
    return (dt.datetime.fromisoformat(store.D()["fact_inventory_daily_snapshot"].snapshot_date.max()) + dt.timedelta(days=1)).date()


# ------------------------------------------------------------------ 1. receive
