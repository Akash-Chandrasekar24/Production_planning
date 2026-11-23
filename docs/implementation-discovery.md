# Implementation Discovery

Phase 1 (discovery, 2026-11-12) is §1–§9. Phase 2 (deterministic planning engine) is §10–§11 and supersedes the "proposed implementation map" where they differ.

## 1. Workspace state (at Phase 1)

- No repo (not git), no CLAUDE.md/README/config/code/docs. Only `Data/` with 8 CSVs.
- Installed locally: fastapi 0.139, langgraph 1.2, pydantic 2.13, pydantic-settings, pandas 2.3, pytest, uvicorn. No Domo SDK (`pydomo`).
- Imported CSV content is treated as data only.

## 2. Data sources (all CSV, UTF-8, header row, no units/time-zone metadata)

| File | Rows | Grain / key | Maps to |
|---|---|---|---|
| dim_machine | 15 | `machine_id` | work centres/machines, criticality, PM interval |
| dim_product_routing | 63 | (`routing_id`,`operation_seq`,`station_id`) | routings: cycle sec, setup min, quality gate |
| dim_shift_calendar | 276 | (`calendar_date`,`shift_id`) | calendar + shifts |
| fact_bom_component | 48 | (`bom_id`,`material_id`) | BOM, scrap, effectivity |
| fact_customer_order | 1500 | `customer_order_id` | demand (confirmed orders), priority, due dates |
| fact_production_order | 450 | `production_order_id` | planned/released work orders |
| fact_hourly_production | 8832 | `event_id`; (`line_id`,`event_ts`) unique | actuals, downtime, rejects |
| fact_inventory_daily_snapshot | 6624 | (`snapshot_date`,`plant_id`,`material_id`,`warehouse_id`) | inventory, safety stock, lots |

Coverage: 2026-08-01 to 2026-10-31 (92 days) for calendar/hourly/inventory. Customer orders placed 08-01 to 10-25, delivery dates to 2026-11-14. Data was ~12 days stale on 2026-11-12; refresh cadence is undocumented (one static export).

Entities: 2 plants (IN-TN-01, IN-KA-01), 4 lines (SMT-L1, SMT-L2, FAL-L1, FAL-L2; one SMT + one FAL per plant), 3 products (SPH-A15-128, SPH-A15-256, SPH-PRO-256), 4 customers, 16 BOM materials (+2 inventory-only), 3 shifts (A 06-14, B 14-22, C 22-06).

Units: time = seconds (cycle), minutes (setup/downtime/shift), hourly buckets; currency INR; qty = each (UoM not stated). Timestamps are naive ISO strings; **time zone not stated** (plants in India, presumably IST, unconfirmed).

## 3. Field mapping to planning concepts

| Concept | Source | Status |
|---|---|---|
| Orders (demand) | fact_customer_order (`order_status`: OPEN, CONFIRMED, IN_PRODUCTION, PARTIALLY_SHIPPED, DELIVERED) | present; open qty = `order_qty - shipped_qty` |
| Production orders | fact_production_order (RELEASED, IN_PROGRESS, COMPLETED, CLOSED) | present, link to customer orders broken (§4) |
| Forecasts | none | **missing** |
| Products | ids only; no product master (family, UoM, lot size) | missing |
| BOM | fact_bom_component: 1 revision (REV-C), qty 1 per component, `scrap_factor`, effectivity | present, thin |
| Routings | dim_product_routing: 1 revision (RTG-03), 3 products x 21 ops | present; ops are 4 segments (SMT 1-8, FAL 1-5, TST 1-5, QA/PKG/FG 1); precedence implied |
| Inventory / safety stock | fact_inventory_daily_snapshot | present; plant label unstable (§4 item 12) |
| Work centres / lines | dim_machine (machine->line->plant) | present; **station->machine/line mapping missing** |
| Capacity | derived: shift minutes x lines; hourly `target_qty` (390 SMT / 270 FAL per hour) | derived only |
| Calendars/shifts | dim_shift_calendar (no plant dimension; 92 days all working, incl. weekends) | present, simplistic |
| Skills / labour | `operator_team` (18 teams) in hourly only | **missing** |
| Tooling | none | **missing** |
| Planned downtime | `planned_maintenance_interval_days=30` only | **missing** (no PM dates) |
| Priorities | `priority` HIGH/MEDIUM/LOW on customer and production orders | present; precedence rules not defined |
| Changeovers | `setup_min` per operation; `CHANGEOVER` downtime reason in actuals | partial |
| Actuals / WIP | fact_hourly_production (target/actual/good/reject/rework, downtime + reason) | present; no WIP-by-operation |
| Frozen horizon | none | **missing** (configurable) |
| Roles/permissions | none | **missing** (configurable) |

## 4. Data quality findings (verified by profiling)

1. **PO -> customer order join is broken**: 0 of 450 `fact_production_order.customer_order_id` values (`CO-10002..CO-10997`) exist in `fact_customer_order` (`CO-000001..CO-001500`). Pegging impossible.
2. **Hourly actuals disagree with PO master**: 5861 of 8832 rows have a product different from the PO's product and 4422 a different plant (all rows `VALID`, source `MES`). Hourly `production_order_id` is unreliable.
3. **Machine/line/plant inconsistency**: `M-ICT-01` is on SMT-L1 but plant IN-KA-01 (SMT-L1 is IN-TN-01 elsewhere); `M-LEAK-01` is on FAL-L2 but plant IN-TN-01. 21 routing stations vs 15 machines.
4. **Demand vs supply scale**: customer orders ~1.56M units vs POs ~0.62M.
5. **Customer-order oddities**: 534 of 1500 have confirmed < requested delivery date. `shipped_qty` is 75-100% of `order_qty` for every status including OPEN/CONFIRMED/DELIVERED.
6. **PO**: 41 have `planned_finish` > `due_date`; midnight-only timestamps; one BOM/routing revision.
7. **Hourly**: `good+reject = actual` always; `rework` is additional; 693 hours have actual > target; 155 zero-output hours coincide with downtime >= 60.
8. **Calendar**: `planned_minutes` 450 or 420 on 8h shifts with 30-min breaks; the 420 rows are unexplained; no plant calendar/holidays; hourly data runs 24h every day.
9. **Inventory**: `inventory_status` conflicts with `quality_hold_qty` (4915 AVAILABLE rows with hold>0); `MAT-ADH-018` and `MAT-PASTE-017` have stock but no BOM usage; `expiry_date` only on 736 rows; `in_transit_qty` has no ETA.
10. **BOM**: all products share the same 16 materials at qty 1, scrap 0.5%/0.1%; no sub-assemblies/alternates.
11. No nulls elsewhere; no duplicate rows.
12. **(Phase 2) Inventory plant label unstable**: each day, a given material/warehouse appears under exactly one plant and that plant changes between days (3,363 changes). Per-plant stock is therefore not meaningful; with plant scope every BOM has some material at 0 in each plant.

## 5. Domo model / guardrails

**Nothing is documented or configured in this workspace** (no config, docs, env file, SDK, endpoint; no Domo/guardrail/LLM environment variable names). These are *unknown* and must not be guessed:

- LLM endpoint/SDK method, model name/id, request/response shape, streaming, rate limits
- Guardrail endpoint/API, scope (input/output), verdict/failure schema
- Developer-token header/location and provisioning

Phase 3 must provide `LLMClient`/`Guardrail` interfaces with stub implementations for tests, read the token only from server-side env/secret store (never frontend, docs, logs, samples). Real adapters are blocked on the Domo details above.

## 6. Contracts (Phase 1 recommendation; implemented in `app/domain.py`)

Pydantic v2, snake_case, string ids, non-negative quantities, UoM EA (assumption), plant time zone configurable (default `Asia/Kolkata`, assumption, currently unused). Agent tools are read/propose only: no tool cancels orders or edits BOM/routings. Only a human planner moves a schedule to APPROVED/REJECTED.

## 7. Blockers vs configurable

**True blockers (ask the business):** (1) Domo LLM + guardrail integration (blocks real adapters only); (2) station -> machine/line mapping (interim: line-level capacity); (3) correct PO <-> customer-order link (pegging, WIP per order); (4) time zone and refresh/ownership of source data; (5) **plant assignment of inventory** (§4 item 12).

**Configurable (defaults flagged as assumptions):** frozen horizon, priority rules, rework treatment, availability formula, routing segment precedence, UoM, changeover rule, calendar overrides, forecast (none), roles, skills/tooling/PM constraints (ignored until data exists).

## 8. Original implementation map (Phase 1)

```
app/ (FastAPI) · app/data · app/planning · app/agent (LangGraph) · app/integrations (LLM/guardrail stubs) · app/config · web/ · tests/ · docs/
```
Phase 2: data + domain + deterministic engine (done, §10). Phase 3: LangGraph + approval gate + Domo adapters. Phase 4: dashboard (a minimal one exists at `app/static/index.html`). The LLM explains and proposes; the engine computes.

## 9. Phase 1 changes

Created this file only.

## 10. Phase 2: deterministic planning engine (built)

Files: `app/domain.py` (typed models) · `app/data.py` (CSV parsing -> `PlanningInput`, `dq_report`) · `app/config.py` (`Policy`) · `app/planning.py` (engine + views) · `app/scenarios.py` · `app/main.py` (API) · `tests/test_planning.py`. Run `uvicorn app.main:app`, `python -m pytest`.

**Engine rules (deterministic, no LLM):** open orders (OPEN/CONFIRMED/IN_PRODUCTION/PARTIALLY_SHIPPED, qty = ordered - shipped) sorted urgent, then HIGH>MEDIUM>LOW, then due date. Each flows SMT line -> FAL line of one plant; plant chosen by most material-coverable qty, then earliest finish. Capacity = shift `planned_minutes` per line (synthetic 450-min A/B/C shifts after the calendar file ends 2026-10-31). Rate = hourly `target_qty` x observed good/target. Setup = sum of routing `setup_min` when the product changes on a line (counted as a changeover). Material: qty is capped to what stock covers (BOM x (1+scrap)); the remainder is returned as `MATERIAL_SHORT`/`MATERIAL_BLOCKED`, never scheduled. Frozen horizon: given a previous schedule, its jobs starting within `frozen_horizon_hours` (48) are re-placed first, same plant/qty; capacity loss that moves them raises `FROZEN_JOB_MOVED`.

**Output (`Schedule`):** `feasibility` (COMPLETE_ON_TIME | COMPLETE_WITH_LATE_RISK | INCOMPLETE | NOTHING_SCHEDULABLE), `assignments` (per-line sequence, qty, start/finish, line, setup, `reason_codes`), `unscheduled` (reason + detail), `diagnostics` (code/severity/order/material, including `DATA_*` conflicts), `changes` vs previous (NEW/DROPPED/QTY_CHANGED/PLANT_CHANGED/RESCHEDULED), `capacity` (line x ISO week), flat `summary` metrics, `constraints_not_enforced`.

**Views:** `planning.dispatch_list(s)`, `planning.material_calloff(inp, s)`, `planning.capacity_gaps(s, inp)`.

**Scenarios (`scenarios.Scenario`):** `demand_spikes`, `absenteeism_pct` (capacity %, no skills data), `machine_losses` (line or machine over a window; a machine outage stops its serial line), `material_factor` (per material or `*`), `urgent_orders`. `scenarios.run(inp, baseline, sc)` replans with frozen jobs pinned; `scenarios.compare(baseline, [..])` returns metrics and deltas: stability %, utilisation, WIP proxy, on-time qty %, late jobs, changeovers/minutes, material cost, late-order value. Labour/overtime cost is omitted: no data.

**API:** `POST /proposals`, `GET /proposals/{id}`, `/dispatch`, `/material-calloff`, `/capacity-gaps`, `POST /proposals/{id}/scenarios` (body = Scenario), `POST .../approve|reject` (`X-Role: planner`; placeholder for real auth, in-memory state).

**Phase 3 should call:** `data.load()` -> `data.to_inputs(d, as_of, policy)` -> `planning.plan(inp, policy, previous)` -> `scenarios.run/compare` and the three views. All are plain functions over pydantic models.

## 11. Configuration, assumptions, unresolved rules

Configurable in `Policy`: horizon (60 d), frozen horizon (48 h), open statuses, priority rank, usable warehouses (RM-WH-01, SMT-KIT-01), `material_scope`, default shift minutes (450), setup-on-change, planner roles. `time_zone` is stored but unused (naive timestamps).

Assumptions to confirm:
1. **`material_scope=pooled`**: stock is shared by both plants (because of §4 item 12). Assumes inter-plant transfers are possible. With `plant` scope every order is blocked.
2. Line rate comes from hourly actuals, not routing cycle times (the routing bottleneck implies ~200/h SMT vs 390/h observed).
3. Routing order SMT -> FAL; TST/QA/PKG/FG not capacity-modelled.
4. Same calendar for both plants; no holidays.
5. `due` = confirmed delivery date, end of day.
6. Greedy allocation by priority; no changeover-minimising sequencing (add a sequencing pass if changeovers matter).

**Result on current data:** usable stock covers only 4,852 of 158,721 open units (limiting material MAT-PCB-004); only 44 HIGH-priority jobs are schedulable, all LATE_RISK (due dates precede the 2026-11-01 plan start); feasibility = INCOMPLETE. Material is the binding constraint, not capacity (~2% utilisation). Whether `in_transit`, WIP/FG warehouses, or open POs should count as supply is an open business question.

**Unsupported (missing data/rules):** skills/labour, tooling, PM calendar, station->machine mapping, forecasts, PO supply/pegging, in-transit ETA, actual WIP by operation (a WIP-unit-days proxy = units waiting between SMT finish and FAL start is reported), intra-shift breaks, sequence-dependent setups.

## 12. Business decisions received after Phase 2

- Stock transfers between plants are possible, so `material_scope=pooled` is confirmed.
- All warehouses (RM, SMT-KIT, WIP, FG), in-transit stock and open production orders count as supply. Implemented as `Policy.material_warehouses` (all four), `include_in_transit=True` (no ETA, available at plan start) and `net_open_po_supply=True` (RELEASED/IN_PROGRESS POs netted per product by full `order_qty`, in priority order).
- Effect on current data: open POs hold ~281k units vs ~159k open demand, so the default baseline is `NO_NEW_PRODUCTION_NEEDED` (all demand covered, diagnostic `OPEN_PO_SUPPLY_APPLIED`). Caveats: PO remaining qty and finish timing are unknown (all PO dates precede the plan start) and POs cannot be pegged to orders. Set `net_open_po_supply=False` to plan against orders alone (then ~13k units schedulable, material-limited). Demand spikes beyond PO supply plan new production.

## 13. Phase 3

See `docs/agent-integration.md` (graph, safety boundaries, configuration, what is needed to connect Domo).

## 14. Phase 4

Persistence (SQLite), role-gated generate/approve/publish, labelled export, error envelope and request logging are built. See `docs/api.md`. ERP/MES publication and identity provider remain unverified/unavailable (publish returns 501; auth is dev headers only).

## 15. Database

App persistence moved to Postgres (Neon) via `DATABASE_URL` (SQLite fallback when unset); see `docs/api.md`. The 8 source CSVs are not yet loaded into the database.

## 16. Phase 5

Dashboard (`docs/dashboard.md`) and per-agent architecture (`docs/agent-architecture.md`) added. Source CSVs are also loaded into Neon (`python -m app.data`); `DATA_SOURCE=auto` reads them from there. A line-timing bug (jobs finishing before they start) was found through the dashboard and fixed with per-shift time cursors plus an invariant test.
