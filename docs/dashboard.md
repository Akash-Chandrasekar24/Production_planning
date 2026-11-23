# Planner dashboard (Phase 5)

One page served by the API at `/` (`app/static/index.html`, plain HTML/CSS/JS, no build step, no frontend dependencies). It shows what the backend returns; it never computes feasibility. Deep links: `/#Schedule`, `/#Materials & capacity`, etc.

## Launch

1. `pip install -r requirements.txt`
2. Optional `.env` (copy `.env.example`): `DATABASE_URL` for Postgres/Neon; without it a local SQLite file is used and the CSVs in `Data/` are read.
3. First time with a database: `python -m app.data` copies the 8 CSVs into `src_*` tables.
4. `python -m uvicorn app.main:app` and open http://127.0.0.1:8000/ (API docs at `/docs`).
5. Pick role `planner` (top left), then **Generate baseline**.

## Views

| View | Shows | API |
|---|---|---|
| Overview | feasibility in plain words, KPIs, due-date risks, material shortages, capacity heat map, unscheduled demand by reason, changes vs previous plan, diagnostics and constraints not enforced, "Ask about this schedule" | `/schedules/{id}`, `/material-calloff`, `/capacity-gaps`, `/agent` |
| Schedule | timeline per line (red = late), dispatch list filtered by plant, line, date, order | `/dispatch` |
| Scenarios | forms for demand spike, absenteeism, machine loss, material shortage, urgent order; comparison table with deltas (stability, utilisation, WIP proxy, on-time, changeovers, material cost) | `/reference`, `/schedules/{id}/scenarios`, `/compare` |
| Details | one order: assignment, reason codes with plain explanations, diagnostics, changes | schedule JSON |
| Materials & capacity | call-off table, capacity by line and week, orders that did not fit | `/material-calloff`, `/capacity-gaps` |
| Review & approve | review facts, approve/reject, separate publish, CSV export, audit log | `/approve`, `/reject`, `/publish`, `/export`, `/audit` |
| Status & data | data source and freshness (days old), integration status, access warning, data-quality issues | `/status`, `/data-quality` |

## Approval and permissions

- Approve/reject need role `planner` and a `PENDING_APPROVAL` schedule. If the schedule has risks, the server answers `RISK_ACK_REQUIRED`; the page lists them in a dialog and resubmits only after the planner ticks the acknowledgement. Nothing to approve (no jobs) is refused by the server.
- **Publish is a separate button, disabled unless** the server reports an available ERP/MES integration, the schedule is `APPROVED`, and the role is planner. Today the integration is **not available**, so it stays disabled with the reason, and the page never says anything was published. The CSV export is labelled "not publication".
- Disabled or hidden buttons are convenience only: every action is checked again by the backend.

## Honest labelling

Estimates and assumptions are marked in the page: open POs assumed outstanding in full, in-transit stock assumed available, WIP is a proxy, shifts after 2026-10-31 are assumed, and a list of constraints not enforced (skills, tooling, maintenance, ...). Domo model, guardrails, and ERP/MES status chips come from `/status`; "adapter not verified" means no working call format exists yet.

## Configuration

Browser: none. The page needs only the API. Server: `DATABASE_URL`, `DATA_SOURCE`, `PLANNING_DATA_DIR`, `PLANNING_DB`, `AUTH_MODE`, `DOMO_*` (see `docs/api.md`, `docs/agent-integration.md`). No token or credential is ever sent to or stored in the browser; the role/name boxes are not secrets.

## Known limits and blockers

- **Sign-in is not real.** The role comes from a drop-down (header). Until an identity provider is connected, anyone who can open the page can pick `planner`. Do not expose the app beyond the dev machine.
- The Domo model and guardrails are not connected, so the assistant shows engine facts plus "model explanation unavailable".
- ERP/MES publication is unavailable (no verified connector).
- Each Neon call costs roughly a second from the dev machine, so first load is a few seconds.
- No automated browser test suite; the page was exercised with headless Chrome (all 7 views load real data, no console errors).
