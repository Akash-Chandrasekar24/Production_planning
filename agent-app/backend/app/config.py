"""Policy = business rules that are configurable, not data. Settings = secrets/endpoints from env or an untracked .env."""
import os
from pathlib import Path
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(os.getenv("PLANNING_DATA_DIR", Path(__file__).resolve().parents[3] / "Data"))   # <project>/Data (backend/app -> agent-app -> project)


class Policy(BaseModel):
    horizon_days: int = 60                     # scheduling window from as_of
    # time fences (standard MPS zones): a re-plan keeps every job starting in the frozen zone where it was; changes to orders in the
    # slushy zone are flagged for an impact check at approval; the flexible zone after that changes freely
    frozen_days: int = 14                      # frozen zone: 0-2 weeks
    slushy_days: int = 42                      # slushy zone: up to 6 weeks
    open_statuses: tuple = ("OPEN", "CONFIRMED", "IN_PRODUCTION", "PARTIALLY_SHIPPED")
    priority_rank: dict = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    material_warehouses: tuple = ("RM-WH-01", "SMT-KIT-01", "WIP-WH-01", "FG-WH-01")   # confirmed by business: all warehouses count as supply
    include_in_transit: bool = True            # confirmed by business; no ETA exists, so counted as available at as_of
    open_po_statuses: tuple = ("RELEASED", "IN_PROGRESS")
    net_open_po_supply: bool = True            # confirmed: open POs are supply. Netted per product by what they still owe (production confirmations)
    material_scope: str = "pooled"             # "pooled" = stock shared by plants (ASSUMPTION: plant labels on inventory rows flip daily); "plant" = per plant
    transfer_days: float = 1.0                 # ASSUMPTION: days to truck materials between Hosur and Bengaluru (load, ~40 km, unload); no transfer times in the data
    default_shift_minutes: int = 450           # used for dates beyond the calendar file
    apply_setup_on_product_change: bool = True
    batch_window_days: int = 7                 # group the same product together within each N-day due window to cut changeovers (0 = off)
    respect_safety_stock: bool = True          # never plan material below its safety stock
    plan_maintenance: bool = True              # block line time for planned machine services (maintenance records)
    plan_open_po_work: bool = True             # plans schedule what open production orders still have to make (the lines' real work)
    yield_aware: bool = True                   # rejects use material and line time: plan with each product's recorded yield per line
    yield_days: int = 30                       # yields from this many days of production records
    level_bottleneck: bool = True              # when both plants finish on time, give the job to the plant whose final assembly is less loaded
    plan_purchases: bool = True                # MRP: what stock can't cover starts after the parts arrive (ordered today, supplier lead time)
    purchase_default_lead_days: int = 14       # lead time for a material with no supplier record
    # meeting the customers' dates (the plan options): which orders get scarce parts and line time first, and express supply
    sequence: str = "priority"                 # "priority" | "penalty" (orders due soonest with the most late-penalty exposure first)
    express: str = "off"                       # "off" | "at_risk" (only parts whose normal lead time misses the order's date) | "all"
    express_lead_factor: float = 0.5           # ASSUMED express terms until the suppliers' real ones are entered: half the lead time ...
    express_min_days: int = 2                  # ... never under 2 days ...
    express_premium_pct: float = 20.0          # ... for 20% more on the parts bought by express
    fill_shifts: bool = False                  # extra shifts: every shift runs (including days now off) and works through the break
    outsource_orders: list[str] = []           # orders a contract manufacturer makes (taken off our lines; delivered on their date)
    outsource_price_pct: float = 90.0          # ASSUMED until a contract manufacturer quotes: they charge 90% of our selling price ...
    outsource_lead_days: int = 7               # ... and need 7 days, so orders due sooner can't be outsourced
    # in-house first: outsourcing is the last resort
    outsource_min_late_days: float = 14.0      # only orders more than this many days late (or not makeable at all) are offered to a contract manufacturer
    outsource_trigger_on_time_pct: float = 25.0  # the agent recommends outsourcing only if even the best in-house plan has fewer units on time than this ...
    outsource_trigger_gain_pts: float = 20.0   # ... and outsourcing would add at least this many points of on-time delivery
    plan_forecast: bool = True                 # make-to-stock: forecast demand not covered by confirmed orders, planned last, from stock left over
    forecast_weeks: int = 4                    # weeks of forecast demand to plan
    fg_target_days: float = 3.0                # production budget: finished goods to hold at the end, in days of forecast demand
    crew_limits: bool = True                   # a shift short of people runs its lines for that share of its minutes
    # demand and capacity gate (volume.py): before the agent schedules, demand that doesn't fit capacity needs the production manager's decision
    volume_gate: bool = True
    volume_break_minutes: float = 30.0         # "work through breaks": extra minutes a shift in overloaded weeks (shifts are 480 min, 450 planned)
    volume_plan_id: str | None = None          # the agreed volume plan a schedule was made with (its committed dates and extra minutes)
    # scheduled reports (email + bell): a daily plant brief to each production head, a weekly report to the production managers
    reports_enabled: bool = False              # scheduled daily/weekly report emails: off (the only automatic email is the approved plan)
    report_daily_hour: int = 7                 # local hour the daily brief goes out
    report_weekly_day: int = 0                 # 0 = Monday
    time_zone: str = "Asia/Kolkata"            # assumption, unconfirmed
    planner_roles: tuple = ("planner",)        # only these may approve/reject
    # API actions per role (enforced by app.auth). Unknown roles get nothing. Approve/publish/cancel/edit-BOM are never agent tools.
    # Positions in the app: engineer (runs planning day to day: orders, plans, tools; cannot approve), production_manager (approves plans
    # for both plants), plant_head (shown as "Production head": leads one plant, sees that plant, prepares it once a plan is approved).
    role_actions: dict = {"viewer": ("read",), "planner": ("read", "generate", "scenario", "approve", "export", "publish", "intake", "agent", "incident", "forecast", "report"),
                          "engineer": ("read", "generate", "scenario", "export", "intake", "agent", "incident", "forecast"),
                          "production_manager": ("read", "approve", "export", "publish", "task", "agent", "incident", "intake", "forecast", "report"),
                          "plant_head": ("read", "export", "task", "incident", "intake", "plant_approve"),
                          "admin": ("read", "export", "integrate", "intake", "team", "users")}
    # approvals: the production manager starts planning and approves the agent's best plan, then each plant's production head approves it
    # for their plant. Admin sees everything but decides nothing: only the data connectors (and accounts) are theirs.
    # planning agent: when to re-plan on its own (it never approves or releases)
    agent_min_new_orders: int = 20             # this many new open orders since its last plan ...
    agent_min_new_units: int = 5000            # ... or this many new units ...
    agent_urgent_days: int = 7                 # ... or any new HIGH-priority order due within this many days
    agent_interval_min: int = 15               # how often it checks the order book
    agent_fault_min: int = 60                  # equipment-fault minutes on a line within the feed's last 24 h that count as a breakdown
    agent_default_outage_h: float = 8.0        # assumed repair time when nobody has given one (one shift); the plant head confirms it
    agent_overtime_min: int = 60               # extra minutes per shift the agent may suggest ...
    agent_overtime_days: int = 7               # ... for this many days
    agent_split_min_qty: int = 500             # orders at least this big may be split into two lots across plants
    # floor agent (reads the last 24 h of the production feed)
    floor_stopped_hours: int = 2               # a line that made nothing this many hours in a row has stopped
    floor_output_min: float = 0.6              # below this share of target over the last 4 h: output has collapsed
    floor_material_stop_hours: int = 2         # hours stopped for missing material before it is raised
    floor_micro_stops: int = 6                 # hours with short stops before it is raised
    # inventory agent
    inv_min_cover_days: float = 3.0            # materials covering fewer days of production than this are at risk ...
    inv_critical_cover_days: float = 1.0       # ... and critical below this
    inv_expiry_days: int = 30                  # lots expiring within this many days
    inv_usage_days: int = 7                    # daily usage = average over this many days of final-assembly output


class Settings(BaseSettings):
    """Names follow AI_Agent_Development_Setup_Guide.docx. Secrets are SecretStr: never logged, serialised or sent to the browser."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    domo_subdomain: str | None = None                  # DOMO_SUBDOMAIN, e.g. gwcteq-partner
    domo_token: SecretStr | None = None                # DOMO_TOKEN (sent as header X-DOMO-Developer-Token)
    domo_model: str | None = None                      # DOMO_MODEL: primary model; omitted -> Domo's default model
    domo_fallback_model: str | None = None             # DOMO_FALLBACK_MODEL: tried once if the primary call fails
    domo_use_session_id: bool = False                  # DOMO_USE_SESSION_ID: Domo returns 404 (DS-0079) for session IDs it did not create, so off by default
    domo_json_mode: bool = False                       # DOMO_JSON_MODE: send responseFormat (guide: confirm the exact "type" value in Domo first)
    database_url: SecretStr | None = None              # DATABASE_URL: Postgres (Neon). Unset -> local SQLite
    readonly_database_url: SecretStr | None = None     # READONLY_DATABASE_URL: SELECT-only role for the agent's SQL tool. Unset -> SQL tool disabled
    neon_api_key: SecretStr | None = None              # NEON_API_KEY: lets the model query through the Neon MCP server (read-only)
    neon_project_id: str | None = None                 # NEON_PROJECT_ID
    neon_branch_id: str | None = None                  # NEON_BRANCH_ID
    neon_database_name: str | None = None              # NEON_DATABASE_NAME (e.g. neondb)
    neon_mcp_url: str | None = None                    # NEON_MCP_URL (default https://mcp.neon.tech/mcp)
    nvidia_api_key: SecretStr | None = None            # NVIDIA_API_KEY: NVIDIA guard models (build.nvidia.com). Unset -> built-in checks only
    nvidia_safety_model: str = "nvidia/llama-3.1-nemotron-safety-guard-8b-v3"   # NVIDIA_SAFETY_MODEL: content-safety model
    connector_key: SecretStr | None = None             # CONNECTOR_KEY: Fernet key that encrypts saved connector credentials. Unset -> cannot save
    auth_mode: str = "password"                        # AUTH_MODE: password (sign-in with app accounts) | dev_headers (tests only: X-Role headers)
    admin_username: str = "admin"                      # ADMIN_USERNAME / ADMIN_PASSWORD: first admin, created once when there are no accounts
    admin_password: str = ""
    session_hours: int = 12                            # SESSION_HOURS: how long a sign-in lasts
    data_source: str = "auto"                          # DATA_SOURCE: auto (database if src_* tables exist, else CSV) | csv | db
    planning_db: str | None = None                     # PLANNING_DB: SQLite path used when DATABASE_URL is unset
    plant_sites: str | None = None                     # PLANT_SITES: JSON {"IN-TN-01": {"city": ..., "lat": ..., "lon": ...}} for the twin's map
    # email for notifications (any SMTP server: Microsoft 365, Gmail, SES ...). Unset SMTP_HOST = in-app notifications only.
    smtp_host: str | None = None                       # SMTP_HOST, e.g. smtp.office365.com
    smtp_port: int = 587                               # SMTP_PORT
    smtp_user: str | None = None                       # SMTP_USER
    smtp_password: SecretStr | None = None             # SMTP_PASSWORD
    smtp_from: str | None = None                       # SMTP_FROM, e.g. "Production planning <planning@example.com>"
    smtp_starttls: bool = True                         # SMTP_STARTTLS (port 465 servers: set SMTP_SSL=true instead)
    smtp_ssl: bool = False                             # SMTP_SSL
    app_url: str = "http://localhost:5173"             # APP_URL: links in emails open the app here
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,https://production-planning-khaki.vercel.app"   # CORS_ORIGINS
    agent_enabled: bool = True                         # AGENT_ENABLED: the planning agent's background check
    volume_gate: bool = True                           # VOLUME_GATE: the demand and capacity gate before the agent schedules
    auto_baseline: bool = True                        # AUTO_BASELINE: create a baseline plan at startup when none exists        # CORS_ORIGINS, comma separated


def _sa(url: str | None) -> str | None:
    """Normalise to a SQLAlchemy URL for the psycopg (v3) driver, as in the guide."""
    if not url:
        return None
    for old in ("postgresql://", "postgres://"):
        if url.startswith(old):
            return "postgresql+psycopg://" + url[len(old):]
    return url


def database_url() -> str | None:
    u = Settings().database_url
    return _sa(u.get_secret_value()) if u and u.get_secret_value() else None


def readonly_database_url() -> str | None:
    u = Settings().readonly_database_url
    return _sa(u.get_secret_value()) if u and u.get_secret_value() else None
