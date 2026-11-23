# Production Planning Agent: use-case conformance

Checked against the use-case statement ("Plan the right product, quantity and sequence"). Status: **Built**, **Partial**, **Not possible with current data** (the data has no such information), **Blocked** (needs a system or credential that does not exist yet).

## Purpose
"Convert customer demand, forecasts and plant constraints into an achievable, cost-aware production plan; keep it synchronised with material availability, capacity, shifts and due dates."
- Built: demand, material availability (safety stock reserved), finite line capacity, shift calendar, due dates and priorities drive every plan (`app/planning.py`).
- Partial: cost-aware = material cost and value of late orders are scored; labour, overtime and changeover cost have no data.
- Not possible with current data: forecasts (none in the data).

## What the agent is responsible for
| Responsibility | Status | Where / notes |
|---|---|---|
| Consolidate confirmed orders, safety stock and priority rules by plant, line, product | Built | open qty = ordered − shipped; priority HIGH > MEDIUM > LOW; safety stock now reserved (`respect_safety_stock`) |
| ...forecast demand | Not possible with current data | no forecast source |
| Check routings and BOM versions | Built | routing setup times per segment; BOM effectivity dates; missing routing/BOM reported, never assumed |
| Check work-centre calendars | Partial | one shift calendar for both plants; standard 3-shift days assumed after it ends |
| Check labour skills / tooling | Not possible with current data | absenteeism is a what-if (lost line time) |
| Check planned downtime | Partial | only a 30-day maintenance interval exists; use the machine-loss what-if |
| Calculate feasible quantities | Built | quantities capped by available material and finite capacity; shortfalls reported, never invented |
| Sequence jobs to reduce changeovers | Built (new) | same product grouped within each due week: on the current data changeovers fell 34 → 24 (2,575 → 1,800 setup min) and late jobs 39 → 37 |
| ...reduce bottlenecks / late orders / excess WIP | Partial | bottleneck and late risk reported and prioritised by due date; WIP is a measured proxy, not optimised |
| What-if: demand spikes, absenteeism, machine loss, material shortage, urgent orders | Built | all five (`app/scenarios.py`), frozen jobs kept in place |
| Publish a finite-capacity plan | Built / Blocked | finite-capacity plan and dispatch list: built. Release to ERP/MES: blocked (no connector); labelled download instead |
| Explain every material schedule change | Built | reason codes per job, change list vs previous plan, assistant explains in plain language |

## How the agent should work
| Step | Status |
|---|---|
| Read orders, inventory, BOM/routing and resource calendars | Built (forecast: not possible) |
| Validate master data, find missing or conflicting constraints | Built: 8 data-quality checks + missing routing/BOM diagnostics |
| Generate candidate schedules with priority, capacity and changeover rules | Built |
| Simulate scenarios; score delivery, utilisation, WIP and cost | Built: on-time share, utilisation, WIP proxy, material cost, late-order value, changeovers, stability |
| Submit for planner approval; publish approved plan to ERP/MES | Approval: built (risk acknowledgement, audit trail). Publish: blocked |

## Systems and data required
| System | Status |
|---|---|
| ERP/SAP orders and BOM | Partial: exports loaded into the database; no live ERP connection |
| MES actual output / WIP | Partial: hourly output used for attainment, downtime, rejects; WIP by operation not available |
| SQL Server planning tables | Partial: PostgreSQL is used instead |
| Excel demand inputs, maintenance calendars, workforce rosters | Not possible with current data: none provided |

## Outputs
Finite production schedule, line-wise dispatch list, material call-off, capacity-gap alerts, scenario comparison, reason-coded schedule changes: **all built**.

## Success measures (KPIs)
| KPI | Status |
|---|---|
| Schedule attainment | Built (new): good output vs target from production records, overall and per line (81.6% overall) |
| On-time delivery | Built: share of demand finishing on time in the plan |
| Changeover time | Built |
| Capacity utilisation | Built |
| Schedule stability | Built (scenario vs baseline) |
| WIP | Partial: proxy (units waiting between line stages) |
| Plan adherence | Not possible with current data: production records cannot be tied to production orders reliably |

## Autonomy, approvals and controls
| Control | Status |
|---|---|
| Planner approval before release | Built (server-enforced; risks must be acknowledged) |
| No autonomous order cancellation or BOM/routing edits | Built: no such function exists; the assistant's topic-control guardrail refuses such requests before any model call |
| Frozen horizons | Built (48 h, configurable) |
| Priority policy | Built (configurable) |
| Plant calendars | Partial (one calendar for both plants) |
| Role-based access | Partial: roles enforced by the server, but sign-in is not real authentication yet |

## Assistant guardrails (NVIDIA NemoGuard, `app/nemoguard.py`)

The pattern-matching checks were replaced by NVIDIA's NemoGuard jailbreak, topic-control and content-safety models: questions are checked before the model, answers before the user, and the assistant is off (fail closed) without `NVIDIA_API_KEY`. Details: `docs/integrations.md`.
