"""Typed domain models. No CSV/source knowledge here (see data.py) and no LLM knowledge."""
from datetime import date, datetime
from pydantic import BaseModel, Field


class Order(BaseModel):
    order_id: str
    product_id: str
    qty: int = Field(gt=0)               # open qty (ordered - shipped)
    due: date
    priority: str
    status: str = "OPEN"
    urgent: bool = False
    unit_price_inr: float | None = None
    customer_id: str | None = None
    plant_id: str | None = None          # set = make it in this plant only (what-ifs that keep work where it was planned)
    kitted: bool = False                 # its material is already issued (open production orders): no stock check, no consumption


class RoutingOp(BaseModel):
    product_id: str
    seq: int
    station_id: str
    segment: str                         # SMT | FAL | TST | QA | PKG | FG (from station id)
    cycle_sec: float
    setup_min: float


class BomLine(BaseModel):
    product_id: str
    material_id: str
    qty_per: float
    scrap: float
    effective_from: date
    effective_to: date


class Slot(BaseModel):
    start: datetime
    end: datetime
    minutes: float                       # plannable minutes in the slot (shift planned minutes)


class LineSpec(BaseModel):
    line_id: str
    plant_id: str
    kind: str                            # SMT | FAL
    rate_per_min: float                  # units/min, from observed hourly target x good/target
    machine_ids: list[str] = []
    slots: list[Slot] = []


class Stock(BaseModel):
    plant_id: str
    material_id: str
    available: float                     # on_hand - allocated - blocked - quality_hold (usable warehouses)
    safety: float
    unit_cost_inr: float | None = None


class PlanningInput(BaseModel):
    as_of: datetime
    horizon_end: datetime
    orders: list[Order]
    routing: list[RoutingOp]
    bom: list[BomLine]
    lines: list[LineSpec]
    stock: list[Stock]
    machine_line: dict[str, str] = {}
    open_po_supply: dict[str, int] = {}   # product -> units on open production orders
    data_issues: list[dict] = []         # conflicts/gaps found while parsing
    maintenance: list[dict] = []         # planned services in the horizon: {line_id, machine_ids, start, end, minutes} (line time blocked)
    yields: dict[str, float] = {}        # "line_id|product_id" -> good / made over the last 30 days; "line_id|*" = the line's average
    lead_days: dict[str, int] = {}       # material -> supplier lead time in days (supplier records)
    forecast: dict[str, list[dict]] = {} # product -> [{week (Monday), qty, model}] forecast for the coming weeks (qty = agreed, if reviewed)
    fg_stock: dict[str, int] = {}        # product -> finished units in stock (none in the current data: counted as 0)


class Diagnostic(BaseModel):
    code: str
    severity: str                        # info | warn | error
    detail: str
    order_id: str | None = None
    line_id: str | None = None
    material_id: str | None = None


class Assignment(BaseModel):
    customer_order_id: str
    product_id: str
    plant_id: str
    qty: int
    priority: str
    due: str
    urgent: bool = False
    frozen: bool = False
    smt_line: str
    smt_seq: int = 0
    smt_start: datetime
    smt_finish: datetime
    smt_setup_min: float = 0
    fal_line: str
    fal_seq: int = 0
    fal_start: datetime
    fal_finish: datetime
    fal_setup_min: float = 0
    status: str = "ON_TIME"              # ON_TIME | LATE_RISK
    late_days: float = 0
    material_short: bool = False
    reason_codes: list[str] = []
    material_cost_inr: float | None = None
    express_cost_inr: float = 0          # value of the parts this job buys by express delivery (the premium is a % of it)


class Unscheduled(BaseModel):
    customer_order_id: str
    product_id: str
    qty: int
    priority: str
    reason: str                          # NO_CAPACITY_IN_HORIZON | MATERIAL_BLOCKED | MATERIAL_SHORT | MISSING_ROUTING | MISSING_BOM
    detail: str = ""


class Change(BaseModel):
    customer_order_id: str
    kind: str                            # NEW | DROPPED | QTY_CHANGED | PLANT_CHANGED | RESCHEDULED
    detail: str


class Schedule(BaseModel):
    id: str
    name: str = "baseline"
    status: str = "DRAFT"                # approval lifecycle: PENDING_APPROVAL | APPROVED | REJECTED
    feasibility: str                     # NO_NEW_PRODUCTION_NEEDED | COMPLETE_ON_TIME | COMPLETE_WITH_LATE_RISK | INCOMPLETE | NOTHING_SCHEDULABLE
    as_of: str
    policy: dict
    assignments: list[Assignment] = []
    unscheduled: list[Unscheduled] = []
    diagnostics: list[Diagnostic] = []
    changes: list[Change] = []
    capacity: list[dict] = []            # per line per ISO week: available/used minutes
    line_utilisation: dict = {}
    summary: dict = {}                   # flat metrics
    constraints_not_enforced: list[str] = []
    created_by: str = "agent"
    decided_by: str | None = None
