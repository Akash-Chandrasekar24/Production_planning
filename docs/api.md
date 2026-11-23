# API, approval controls, persistence and integration status (Phase 4)

Run: `python -m uvicorn app.main:app` (interactive docs at `/docs`). Tests: `python -m pytest`.

## Auth assumptions (IMPORTANT)

No identity provider is verified in this project. `AUTH_MODE=dev_headers` (the only mode) reads `X-Role` and `X-User` headers, which **anyone who can reach the API can set**. It exists so role rules are enforced in backend code now; it is not authentication. Any other `AUTH_MODE` fails closed with `503 AUTH_NOT_CONFIGURED`. Replace `principal()` in `app/auth.py` (same `Principal(user, role)` shape) before sharing the app.

Role -> actions (`Policy.role_actions`): `viewer`: read. `planner`: read, generate, scenario, approve, export, publish. Unknown roles (including `agent`) get nothing. The LangGraph agent has its own intent permissions and **no** approve/publish/cancel/BOM/routing capability; `POST /agent` only ever creates `PENDING_APPROVAL` schedules.

## Endpoints

| Method / path | Action | Notes |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /status` | read | version, auth mode, persistence, data loaded, integration status (no secrets) |
| `GET /data-quality` | read | data issues with counts |
| `GET /demand` | read | open demand and open-PO supply by product |
| `POST /schedules` | generate | body `{as_of?, horizon_days (1-365)}` -> `ScheduleSummary` (201) |
| `GET /schedules` | read | list, `limit`/`offset` |
| `GET /schedules/{id}` | read | full `Schedule`: assignments with `reason_codes`, `unscheduled`, `diagnostics`, `changes`, `capacity`, `constraints_not_enforced`, `feasibility` |
| `GET /schedules/{id}/dispatch` / `material-calloff` / `capacity-gaps` | read | deterministic views |
| `GET /schedules/{id}/audit` | read | who did what, when |
| `POST /schedules/{id}/scenarios` | scenario | body = `Scenario`; replans against the baseline with frozen jobs pinned; returns id + comparison |
| `GET /compare?base=&ids=a,b` | read | metric comparison rows |
| `POST /schedules/{id}/approve` / `reject` | approve | body `{note?, acknowledge_risks}` |
| `POST /schedules/{id}/publish` | publish | **no connector: returns 501**, status unchanged |
| `GET /schedules/{id}/export` | export | CSV dispatch list, labelled export only |
| `POST /agent` | read (+ graph permissions) | see `docs/agent-integration.md` |

## Lifecycle and rules

`generate / scenario` -> `PENDING_APPROVAL` -> (`approve` -> `APPROVED` -> `publish` -> `PUBLISHED`) or (`reject` -> `REJECTED`). Three separate actions.

- Approve is refused (`409 NOTHING_TO_APPROVE`) if the schedule has no assignments (e.g. `NO_NEW_PRODUCTION_NEEDED`, `NOTHING_SCHEDULABLE`).
- If the schedule carries risks (unscheduled demand, late jobs, material-short jobs, error diagnostics such as `MISSING_ROUTING`, `FROZEN_JOB_MOVED`), approve returns `409 RISK_ACK_REQUIRED` with the list; the planner must resubmit with `acknowledge_risks=true`. The acknowledged list is stored in the audit log.
- Decisions use an atomic compare-and-set, so two planners cannot both decide; the loser gets `409 INVALID_STATE`.
- Frozen horizon and priority ordering are applied by the planning service (`Policy.frozen_horizon_hours`, priority rank); scenario replans keep frozen jobs in place and raise `FROZEN_JOB_MOVED` / `FROZEN_JOB_UNPLACEABLE` when capacity loss forces a change (which then require acknowledgement). The API does not allow editing assignments, BOMs, routings or orders.

## Errors

Every error: `{"error": {"code", "message", "detail"}, "request_id"}` (also in `X-Request-ID`). Codes: `UNAUTHENTICATED` 401, `FORBIDDEN` 403, `SCHEDULE_NOT_FOUND` 404, `INVALID_STATE` / `NOT_APPROVED` / `NOTHING_TO_APPROVE` / `RISK_ACK_REQUIRED` 409, `INVALID_REQUEST` (field list) / `INVALID_SCENARIO` / `AGENT_*` 422/403/404, `PUBLISH_CONNECTOR_UNAVAILABLE` 501, `AUTH_NOT_CONFIGURED` / `DATA_UNAVAILABLE` 503, `INTERNAL_ERROR` 500 (no internals leaked).

## Persistence

`app/store.py`. **Postgres (Neon) when `DATABASE_URL` is set**, otherwise a local SQLite file (`PLANNING_DB`, default `var/planning.db`, git-ignored). Tables are created automatically (`schedules`, `events`). `DATABASE_URL` is read only from the environment or the untracked `.env` (it contains a password, so it is a secret value: never logged, never returned; `/status` reports only `{"kind": "postgresql"}`). Tests force SQLite (`tests/conftest.py`) so they never touch the real database.

Stored: full schedule JSON (reason-coded assignments, diagnostics, changes), scenario definition, approval status/decider/note/timestamps, and an audit log. Scenario inputs are rebuilt deterministically from the CSVs + stored policy + scenario, so old schedules depend on the CSVs not changing (stored schedule bodies are unaffected). Approval uses an atomic compare-and-set (`UPDATE ... WHERE status=...`), which also works across multiple API workers.

Performance: one shared connection is reused (a new TLS connection per statement to Neon took seconds); each statement still costs a network round trip (~1 s measured from the dev machine), so list/detail calls are slow-ish until a pool and fewer round trips are added. The source CSV data is still read from `Data/`, not from the database.

## External integrations

| Integration | Status |
|---|---|
| ERP/MES publication | **Unavailable**: none documented/configured. `app/publish.py` is the connector boundary (`Publisher`); `publish` returns 501 and never marks a schedule published. Use `GET /schedules/{id}/export` (header `X-Export-Label: EXPORT-ONLY-NOT-PUBLISHED-TO-ERP-MES`). |
| Assistant guardrails | NVIDIA NemoGuard, fail closed (`docs/integrations.md`). |
| Connectors | `/api/integrations/*`: catalog, connectors CRUD, test, objects, preview, sync, syncs, datasets, activity (`docs/integrations.md`). |
| Identity provider | Not verified; dev headers only. |

## Configuration (names only)

`PLANNING_DATA_DIR` (CSV folder), `DATABASE_URL` (Postgres/Neon, secret), `PLANNING_DB` (SQLite fallback), `AUTH_MODE`, `DOMO_DEVELOPER_TOKEN`, `DOMO_LLM_ENDPOINT`, `DOMO_LLM_MODEL`, `NVIDIA_API_KEY`, `CONNECTOR_KEY` (env or untracked `.env`; see `.env.example`).

## Logging

One line per request: method, path, status, duration, request id, claimed role. No headers, bodies, tokens, question text or explanations. Unhandled errors log the exception type only. A startup warning notes dev-header auth.

## For Phase 5 (UI)

Send `X-Role` / `X-User` (placeholder until real auth); use `/schedules` routes (the old `/proposals` routes are gone). Show `feasibility`, `diagnostics`, `constraints_not_enforced`; on approve handle `RISK_ACK_REQUIRED` by showing `error.detail` and resubmitting with `acknowledge_risks=true` only after the planner confirms; show `PUBLISH_CONNECTOR_UNAVAILABLE` honestly and offer the export. Never show a schedule as published unless `status == "PUBLISHED"`. The minimal page in `app/static/index.html` already follows this.
