# Production Planning Agent (agent-app)

The UI is a widget dashboard: sidebar views (Dashboard, Schedule, Scenarios, Review & approve, Details, Materials & capacity, Data overview); the Dashboard has key-figure cards with change vs the previous period, charts, a products table, an AI assistant card, and an **Add widget** drawer (16 widgets; the choice is remembered per browser). Figures come from `/api/insights` (data facts, period comparison) and the selected plan. No technical details are shown in the UI.

React (Vite) + FastAPI + LangChain + Domo AI Services + PostgreSQL, following `AI_Agent_Development_Setup_Guide.docx`.
Conformance details and known gaps: `docs/guide-conformance.md`.

## Layout
- `backend/`: FastAPI API and the planning agents (deploys to Render; `render.yaml` at this folder's root)
- `frontend/`: React (Vite) app (deploys to Vercel; `frontend/vercel.json`)

## Deploy
**Backend on Render**: push this folder to a Git repository, then in Render: New > Blueprint > pick the repository (it reads `render.yaml`).
Enter the secret values it asks for: `DATABASE_URL` (Neon), `CORS_ORIGINS` and `APP_URL` (the Vercel address), `ADMIN_*`, `CONNECTOR_KEY`,
`DOMO_*`, `NVIDIA_*`, `SMTP_*`. One web service runs the API and the agents (`RUN_MODE=all`); check `https://<service>.onrender.com/health`.

**Frontend on Vercel**: New Project > import the same repository > Root Directory `frontend` (Vite is detected).
Add the environment variable `VITE_API_URL=https://<service>.onrender.com`, then deploy. The app calls the API there directly.
After the first Vercel deploy, put its address in Render's `CORS_ORIGINS` and `APP_URL`.

```
cd agent-app/backend
python -m venv .venv && .venv\Scripts\activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env                                 # fill DOMO_TOKEN, DATABASE_URL, READONLY_DATABASE_URL
uvicorn app.main:app --reload --port 8000              # API docs: http://localhost:8000/docs

cd ../frontend && npm install && npm run dev                 # http://localhost:5173  (proxies /api to :8000)
# or: npm run build  -> the backend then also serves the UI (frontend/dist) at http://localhost:8000/
```
### Running as several processes (production)
The API and the agents can run apart, coordinated through the database (`app/coord.py`: leases, a data version, durable jobs):
```
# API only, as many workers as needed (stateless: no agent loops, no in-memory jobs)
set RUN_MODE=api&& uvicorn app.main:app --port 8000 --workers 4
# the agents: background rounds, scheduled reports and the upload job queue (a second one stands by and takes over if it stops)
python -m app.worker
```
`RUN_MODE=all` (the default) keeps everything in one process, as before. Either way, only one process leads each background loop,
plan changes are serialised across processes, each email is sent once, and caches are dropped everywhere within 2 s of a change.

Tests: `cd agent-app/backend && python -m pytest` (uses a throwaway local database; never touches Neon).
First run against a database: `python -m app.data` copies the CSVs in `Data/` into `src_*` tables.
SQL tool role: run `backend/db.sql` once as the DB owner, then set `READONLY_DATABASE_URL`.
