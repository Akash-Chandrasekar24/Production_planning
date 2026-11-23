# Integrations and assistant guardrails

## Assistant guardrails: NVIDIA NemoGuard (`backend/app/nemoguard.py`)

The pattern-matching guardrails (`app/guardrails.py`) were removed. Every assistant question and answer is now checked by NVIDIA's NemoGuard models on the NVIDIA API.

| Check | Model / endpoint | When | Blocks |
|---|---|---|---|
| Jailbreak | `nemoguard-jailbreak-detect`: `POST https://ai.api.nvidia.com/v1/security/nvidia/nemoguard-jailbreak-detect`, body `{"input": text}` -> `{"jailbreak": bool, "score": n}` | every question | prompt injection, role override |
| Topic control | `nvidia/llama-3.1-nemoguard-8b-topic-control`: `POST https://integrate.api.nvidia.com/v1/chat/completions`, system = topic rules, user = question -> `on-topic` / `off-topic` | every question | off-topic questions, questions about models, prompts, databases or credentials, and requests to change, cancel, approve or release data |
| Content safety | `nvidia/llama-3.1-nemoguard-8b-content-safety`: same endpoint, Nemotron safety prompt -> `{"User Safety": .., "Response Safety": ..}` | every question and every answer | unsafe content (23 categories) |

- The three input checks run in parallel. A blocked question gets a polite reply, and the model is never called.
- **Fail closed:** without `NVIDIA_API_KEY`, or when a guard call fails or returns something unreadable, the chat endpoint returns `503 GUARDRAIL_<code>` and nothing reaches the model. The dashboard shows "The assistant isn't available right now".
- **Kept in code** (these are request limits, not content rules): 2,000-character limit, 8 questions per user per minute, and the "figures not found in the data" warning.
- **Sources of the contracts:** the model pages on build.nvidia.com. The NeMo Guardrails Python library was not used: it needs the C++ `annoy` build (no compiler on this machine) and would downgrade `langchain-core` from 1.6 to 0.3.
- **Tests:** `tests/test_nemoguard.py` uses mocked HTTP and checks the request contracts, each block reason, the output check, fail-closed behaviour, and that the key never appears in errors.

## Data connectors (`backend/app/connectors.py`, `backend/app/integrations_api.py`, UI: Integrations > Connections)

| Type | Driver | Connection test | Lists | Reads |
|---|---|---|---|---|
| SAP S/4HANA | OData v2/v4 over HTTPS (basic auth, optional `sap-client`, optional `APIKey` for the SAP sandbox) | `GET <service>/$metadata` | entity sets | `GET <service>/<EntitySet>?$top=n&$format=json`; `/Date(ms)/` converted to ISO |
| PostgreSQL | psycopg 3 | `SELECT 1` | tables and views | `SELECT * ... LIMIT n` built by SQLAlchemy from the reflected table |
| MySQL / MariaDB | PyMySQL | `SELECT 1` | tables and views | same |
| SQL Server | pymssql | `SELECT 1` | tables and views | same |
| Oracle | python-oracledb (thin) | `SELECT 1 FROM DUAL` | tables and views | same |

**Flow**
1. An admin adds a connection.
2. **Test** opens a real connection.
3. **Import data**: pick a source table and the planning dataset it feeds. Columns are matched by name (ignoring case), and missing ones can be matched by hand.
4. The rows are copied into a staging table `ext_*` in the app database (Neon), with types and date formats converted to the built-in data's.
5. Under **Data used for planning**, switch the dataset to that import. The app first builds planning inputs from it and refuses an import the planner cannot read.
6. Switching back to **Built-in data** is always possible.
7. Removing a connection drops its staging tables and resets any dataset that used it.

**Rules**
- **Read-only:** no connector ever writes to the external system. Only listed tables can be read (no free-form SQL). At most 200,000 rows per import, in one request or query.
- **Credentials:** encrypted with `CONNECTOR_KEY` (Fernet) before they are saved. They are never returned by the API, and a blank password on edit keeps the saved one. Driver errors are cut to their first line.
- **Roles:** reading the page needs `read`. Adding, testing, importing and switching need `integrate`, which only the new **admin** role has. Every change is written to the audit log (`GET /api/integrations/activity`).
- **Tables in Neon:** `connectors`, `connector_syncs`, `dataset_sources`, and one `ext_*` table per import.

**Verified live (2026-11-12)**
- PostgreSQL connector against the Neon database: connect, list, preview, import 1,500 order rows, switch planning to them (1,183 open orders before and after), switch back, delete.
- SAP OData connector against the public OData v2 Northwind service, which uses the same protocol as SAP Gateway: 26 entity sets listed, preview, a wrong-shape import rejected with the 12 missing columns.
- No SAP S/4HANA system was available to test against.

**Not built**
- Publishing approved plans back to SAP or an MES. Release stays blocked until a verified write contract exists.
- Scheduled or automatic re-imports.
- Paging beyond 200,000 rows.

**Logos:** `ui/public/logos/`. Sources and licenses are in `SOURCES.txt` (Simple Icons CC0, Devicon MIT). They are served locally.

**Settings:** `NVIDIA_API_KEY`, `CONNECTOR_KEY` (see `backend/.env.example`).
