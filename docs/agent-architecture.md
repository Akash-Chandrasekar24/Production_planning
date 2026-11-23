# Production Planning Agent: architecture

Principle: the agent is not a chatbot on a database. It is a pipeline (Input -> Processing) feeding a business-intelligence layer (Presentation) with an AI layer on top that may only explain and propose. Every section below says what is **built**, what is **designed but not built**, and what is **blocked**.

## 1. Input

| Need | Source | Tables / columns used | Status |
|---|---|---|---|
| Demand | customer orders (CSV export, loaded to Neon `src_fact_customer_order`) | `customer_order_id, product_id, order_qty, shipped_qty, confirmed_delivery_date, priority, order_status, unit_price_inr` | Built |
| Supply from open production orders | `fact_production_order` | `product_id, order_qty, order_status (RELEASED, IN_PROGRESS)` | Built. Remaining quantity and finish timing unknown; the POs cannot be linked to customer orders |
| Products, BOM | `fact_bom_component` | `product_id, material_id, component_qty, scrap_factor, effective_from/to` | Built |
| Routing, setup | `dim_product_routing` | `product_id, station_id, operation_seq, standard_cycle_sec, setup_min` | Built (setup used; cycle times not, see Processing) |
| Lines, machines, plants | `dim_machine`, `fact_hourly_production` | `machine_id, line_id, plant_id`; `line_id, plant_id, target_qty, good_qty` | Built |
| Calendar, shifts | `dim_shift_calendar` | `shift_start, shift_end, planned_minutes, is_working_day` | Built; ends 2026-10-31, later shifts assumed |
| Inventory, safety stock, in-transit | `fact_inventory_daily_snapshot` | `on_hand, allocated, blocked, quality_hold, in_transit, safety_stock, unit_cost_inr, warehouse_id` | Built; plant label unstable, so pooled |
| Actuals, downtime | `fact_hourly_production` | `actual_qty, good_qty, reject_qty, downtime_min, downtime_reason` | Loaded; used for line rate only |
| Forecast | none exists | n/a | **Missing** |
| Skills, tooling, maintenance dates, station-to-machine map | none exist | n/a | **Missing** |
| ERP/MES (live orders, publish) | none documented | n/a | **Blocked**: no verified integration |
| Domo model, guardrails | none documented | n/a | **Blocked**: adapters stubbed |

Access path: `data.load()` reads the `src_*` tables in Neon (Postgres) when `DATABASE_URL` is set, else the CSVs in `Data/`. Refresh: manual (`python -m app.data` re-copies the CSVs); no scheduled feed exists.

## 2. Processing

- **Data model:** typed domain models (`app/domain.py`: Order, BomLine, RoutingOp, Slot, LineSpec, Stock, PlanningInput; Schedule, Assignment, Unscheduled, Diagnostic, Change). Source parsing is separate (`app/data.py`).
- **Validation:** `dq_report` recomputes known data conflicts with counts (broken PO link, hourly/PO mismatch, machine/plant mismatch, status/shipped conflicts, inventory plant instability). They appear as `DATA_*` diagnostics on every schedule.
- **Business rules (configurable in `Policy`):** open statuses; priority order (urgent, HIGH, MEDIUM, LOW, then due date); frozen horizon (48 h); warehouses counted as supply; in-transit counted; open POs netted per product by full order quantity; pooled stock across plants; default 450-minute shifts past the calendar; setup added on product change.
- **Calculations (deterministic, never by the LLM):** open quantity; PO netting; material cover (BOM x (1 + scrap) against pooled stock, in priority order); line rate (hourly target x observed good/target); finite-capacity placement SMT then FAL per plant with shift cursors; lateness vs due date; changeovers; WIP proxy; material cost; late-order value; utilisation by line and week.
- **Scenario engine:** demand spike, absenteeism (capacity %), machine loss, material shortage, urgent order; replans against the baseline with frozen jobs pinned; diffs and stability.
- **Persistence and audit:** schedules, scenarios, approvals, audit events in Neon (SQLite fallback).
- **Not modelled:** TST/QA/PKG stations, skills, tooling, maintenance windows, sequence-dependent setup, breaks, transfers lead time.

## 3. Presentation

- **Dashboard** (`app/static/index.html`): Overview (health, KPIs, due-date risks, material shortages, capacity heat map, unscheduled demand by reason, changes, diagnostics), Schedule (timeline and dispatch list, filters by plant, line, date, order), Scenarios (create and compare), Details (reason codes per order), Materials and capacity, Review and approve (approve/reject, publish as a separate disabled action, CSV export, audit log), Status and data (freshness, integration status, data quality).
- **KPIs:** demand, scheduled, covered by POs, unscheduled, late jobs, on-time share, utilisation, bottleneck, changeovers, WIP proxy (estimate), material cost, late-order value, schedule stability.
- **Alerts:** late-risk jobs, material shortfall and safety-stock gap, overloaded weeks (>= 95%), moved frozen jobs, data conflicts, integration unavailable.
- **Reports:** labelled CSV dispatch export (not publication); API for any BI tool.

## 4. AI intelligence

| Capability | Today | Notes |
|---|---|---|
| Analyse | **Built (deterministic)**: late risk, material shortage, capacity gaps, data conflicts, scenario trade-offs | rules, not machine learning |
| Explain | **Built, model stubbed**: LangGraph answers planner questions and summarises scenario differences from engine facts only; numbers not in the facts are rejected; guardrails on input and output | needs the Domo model and guardrail contracts |
| Recommend | **Partly**: the engine proposes schedules and shows consequences | the assistant may suggest scenarios in text once the model is connected; it does not choose or apply them |
| Detect | **Built (rules)**: the detections above | anomaly detection on actuals is **designed, not built** |
| Predict | **Not built**: demand forecasting, downtime and yield prediction, realistic lead times | the hourly actuals (downtime reasons, yield) could train a first downtime/yield model; forecasts need order history beyond 3 months and a forecast source |
| Automate | **Only generating proposals** | the agent cannot approve, publish, cancel orders, or edit BOMs/routings; planner approval is required and enforced in backend code |
