# Conformance with AI_Agent_Development_Setup_Guide.docx

Status: **Matched** (as specified), **Adapted** (deliberate, reason given), **Open** (needs something from you).

| Guide item | Status | Notes |
|---|---|---|
| Structure `agent-app/backend/app/{config,db,domo_llm,tools,agent,main}.py`, `ui/{index.html,vite.config.js,src/App.jsx,theme.css,app.css}` | Matched | plus `planning*.py`, `domain.py`, `store.py` etc. for the planner engine (kept from earlier phases) |
| Python 3.11+, FastAPI, SQLAlchemy 2 + `psycopg`, pydantic-settings | Matched | `requirements.txt` follows the guide (+ pandas, pytest) |
| Settings names `DOMO_SUBDOMAIN, DOMO_TOKEN, DOMO_MODEL, DATABASE_URL, READONLY_DATABASE_URL` | Matched | tokens held as `SecretStr`; `.env` git-ignored; token only in backend |
| `db.py` models `Conversation`, `Message` (+`steps`), `init_db` on startup | Matched | added `owner` (conversations are per user) and `warnings` columns |
| `domo_llm.py` `DomoTextLLM` -> `POST https://{subdomain}.domo.com/api/ai/v1/text/generation`, header `X-DOMO-Developer-Token`, payload `input/temperature/maxTokens/model/system/sessionId` | Matched and **live-tested** (2026-11-12) | ping and a full agent run succeeded against gwcteq-partner. **Primary model `domo.domo_ai.domogpt-large-v3:anthropic` (`DOMO_MODEL`), fallback `domo.openai.gpt-5.6-sol` (`DOMO_FALLBACK_MODEL`)**: if the primary call fails for any reason the same request is retried once on the fallback (verified live by forcing a bad primary; both failing returns the error, no endless retry). temperature 0. **Deviation found live:** sending `sessionId` (conversation id, guide section 11) makes Domo answer 404 (DS-0079) because it only accepts sessions it created, so it is off by default (`DOMO_USE_SESSION_ID`). `responseFormat` stays opt-in (`DOMO_JSON_MODE`; the guide says to confirm its `type` first) and works without it |
| `tools.py` `get_schema`, `run_sql` (single SELECT, FORBIDDEN regex, 50 rows, 8 s timeout, `SET TRANSACTION READ ONLY`, read-only engine) | Matched | hides app tables (conversations, messages, schedules, events). **Open:** the `agent_ro` role does not exist yet (creating it in Neon was blocked); until `READONLY_DATABASE_URL` is set the SQL tools are disabled, never falling back to the read/write role. Run `backend/db.sql` |
| Extra tools (guide section 10) | Adapted | planner tools added: `list_schedules, get_schedule_summary, get_material_shortfalls, get_capacity_gaps, generate_schedule, run_scenario`. No approve/publish/cancel/BOM/routing tool exists; creating tools check the caller's role. The guide's Beast Mode / dataset-schema tools (Appendices B, C) are **not implemented** (not needed for planning; need a Domo token to verify) |
| `agent.py` JSON-action loop, max 5 tool calls, `PromptTemplate | llm | StrOutputParser`, history last 10, `_extract_json` | Matched | prompt extended with planner/safety rules; final answers are checked for numbers absent from tool results (returned as `warnings`) |
| API `/api/conversations`, `/api/conversations/{id}/messages`, `/api/chat` | Matched | conversations are scoped to `X-User`; errors use one envelope |
| CORS for `http://localhost:5173`, Vite proxy `/api` | Matched | `CORS_ORIGINS` configurable |
| Frontend React + Vite, Montserrat 400-700 via Google Fonts link | Matched | |
| `theme.css` (9.8) and `app.css` (9.9) | Matched | verified byte-for-byte after whitespace normalisation; planner styles appended using tokens only (no hex), font sizes limited to the guide's 16/14/13/12 px |
| Light default, dark via `:root[data-theme="dark"]`, toggle saved in `localStorage` | Matched | |
| `App.jsx` chat (sidebar, tool-trace `<details>`, "Thinking…", composer) | Matched | plus Assistant/Planner switch, role and name inputs |
| Hardening: secrets, SQL role + regex | Matched / Open | see above |
| Hardening: temperature 0, `sessionId`, logging of Domo token usage | Matched | usage logged (no content) |
| Hardening: Alembic migrations, JWT/SSO, per-user rate limits | **Open** | still `create_all`/`CREATE IF NOT EXISTS`, dev-header auth (not authentication), no rate limiting |
| Deployment (gunicorn behind Nginx, serve `ui/dist`) | Adapted | the backend can serve `ui/dist` itself; gunicorn/Nginx not set up |
| Guardrails | Built | NVIDIA NemoGuard jailbreak, topic-control and content-safety models; fail closed without `NVIDIA_API_KEY` (`docs/integrations.md`) |

## Live test result (real Domo model, local database)
Planner asked to generate a schedule -> the model called `generate_schedule`, reported the engine's numbers (no ungrounded-number warnings); a what-if (demand x3) and a shortfall question used `run_scenario`, `list_schedules`, `get_material_shortfalls`; "approve and publish" was refused by the model and no such tool exists; a viewer's `generate_schedule` was rejected by the role check; all schedules stayed `PENDING_APPROVAL`. Also fixed: the chat endpoint no longer holds a database transaction open while the agent runs (it caused `database is locked` on SQLite).

## Neon MCP binding and how the model queries the database
- **Bound:** `app/neon_mcp.py` is an MCP (Streamable HTTP) client and `neon_run_sql` is an agent tool. It uses the hosted server with `readonly=true` and only the querying/schema/projects categories, builds the call from the server's own advertised `run_sql` input schema (nothing guessed), applies the single-SELECT guard first, and never sends or logs the key. Tested against a mock MCP server (protocol order, session id, SSE replies, schema-driven args, errors, blocked writes).
- **Live-tested (2026-11-12) with a Neon API key:** handshake, `tools/list` (14 tools; `run_sql` takes `sql, project_id, branch_id, database_name, role_name, compute_id, max_rows`, only `sql` required) and real queries succeeded against project `nameless-wildflower-19691153`: same `neondb` database and tables as the app's own connection. Settings: `NEON_API_KEY`, `NEON_PROJECT_ID`, `NEON_BRANCH_ID`, `NEON_DATABASE_NAME`. Caveat: MCP connects as the owner role `neondb_owner`; read-only is enforced by `readonly=true` on the server plus our single-SELECT guard, and I did not independently attempt a write through MCP to prove the server-side flag.
- **Live run of the model querying Neon (via the SQL tool, read-only transaction):** the primary model called `get_schema`, wrote its own SQL, and answered from the results: open orders per product (405/415/363 orders; 54,782 / 53,226 / 50,713 units), lowest usable stock (MAT-ANT-012 10,855; MAT-SCR-016 11,986; MAT-CON-013 12,616), downtime by line and reason (SMT-L1 15,810 min; EQUIPMENT_FAULT most common). All match the engine's earlier figures. A request to delete all rows from `src_dim_machine` was refused with no tool call; row counts in Neon were unchanged afterwards. The number-check flagged two totals the model had summed itself (1183 and 158721): correct, but not literally in the tool output, which shows the check works.
- **Through MCP (live, 2026-11-12):** with the local SQL tool off, the model used `neon_run_sql` only and got the same answers (open orders 405/415/363, lowest stock MAT-ANT-012 10,855, FAL-L2 machines across two plants); a delete request was refused with no tool call. `get_schema` now falls back to MCP when no local read-only role exists (one call instead of 2-3 discovery queries). Tests blank all NEON_/DOMO_ variables so they never reach live services.
- Each question took ~30 s (several model calls plus ~1 s per Neon query).

## Needs from you
1. `NEON_API_KEY` (+ `NEON_PROJECT_ID`) to live-test the MCP path, and run `backend/db.sql` as the Neon owner, set `READONLY_DATABASE_URL`.
2. Identity provider details (JWT/SSO) and the ERP/MES connector remain unverified.

Earlier docs (`api.md`, `dashboard.md`, `agent-integration.md`) describe the previous layout (root `app/`, `/schedules` routes, LangGraph, vanilla dashboard); where they differ, this file and `agent-app/README.md` win: routes are now under `/api`, the agent is LangChain, the UI is React.
