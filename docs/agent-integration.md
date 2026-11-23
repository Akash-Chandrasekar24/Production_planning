# Agent workflow, Domo LLM and guardrails (Phase 3)

## Verification status

| Integration | Status | Evidence |
|---|---|---|
| LangGraph workflow | **Built and tested** | `app/agent.py`, `tests/test_agent.py` |
| Domo LLM | **NOT verified, stubbed** | No Domo SDK, config, docs or env vars exist in this project. `DomoLLM` raises `NOT_CONFIGURED` / `NOT_VERIFIED` and sends nothing. |
| Domo guardrails | **NOT verified, stubbed** | Same: `DomoGuardrail` sends nothing. |

No endpoint, SDK method, header or request format was guessed. The tests use in-test fakes for the model and guardrail to exercise every path.

## What is needed to connect (fill in `app/integrations.py`, `DomoLLM.complete` / `DomoGuardrail.check`)

1. Domo LLM: endpoint URL, how the developer token is passed (header name/scheme), request body (model name, messages, parameters), response shape (where the text is), timeout/rate limits.
2. Guardrails: how to invoke the instance guardrail (endpoint or SDK call), request body, and what an allow / block verdict looks like.
3. Set the configuration below on the backend only.

## Configuration (names provisional; values never committed)

| Name | Purpose |
|---|---|
| `DOMO_DEVELOPER_TOKEN` | developer token; read as `SecretStr`, used only inside the adapters |
| `DOMO_LLM_ENDPOINT` | LLM service location |
| `DOMO_LLM_MODEL` | model identifier |
| `DOMO_GUARDRAIL_ENDPOINT` | guardrail service location |

Set as environment variables or in an untracked `.env` (see `.env.example`, `.gitignore` excludes `.env`). The token is never in responses, logs, frontend code, or `repr(Settings())`.

## Graph

`START -> authorize -> guard_input -> compute -> facts -> explain -> guard_output -> finalize -> END`. Any node that sets a non-OK status (`REFUSED`, `BLOCKED_INPUT`, `NOT_FOUND`, `ERROR`) jumps straight to `finalize`.

| Node | Does | Failure path |
|---|---|---|
| authorize | role -> intent check (`Policy.agent_permissions`), question length, flags action-like requests | `REFUSED` (403) |
| guard_input | guardrail on the planner's question | blocked -> `BLOCKED_INPUT` (422); unavailable -> model skipped, `GUARDRAIL_<code>`, facts still returned |
| compute | calls `data.to_inputs`, `planning.plan`, `scenarios.run` (deterministic) | unknown schedule -> `NOT_FOUND` (404); error -> `ERROR` (500, type name only) |
| facts | compact, grounded facts: metrics, feasibility, unscheduled by reason, diagnostics, shortfalls, late jobs, scenario comparison, constraints not enforced | none |
| explain | one Domo LLM call with system rules + question (marked untrusted) + facts JSON | `LLM_NOT_CONFIGURED` / `LLM_NOT_VERIFIED` / `LLM_ERROR`: text omitted |
| guard_output | rejects text with numbers absent from the facts, then runs the output guardrail | `WITHHELD_UNGROUNDED_NUMBERS`, `BLOCKED_BY_GUARDRAIL`, `GUARDRAIL_<code>` (fail closed) |
| finalize | sets `explanation_status` | none |

Intents: `plan` (new baseline), `scenario` (needs `schedule_id` + `scenario`), `ask` (needs `schedule_id`, `question`), `explain`. Roles: `planner` all four; `viewer` only `ask`/`explain`. Anything else (approve, release, cancel_order, edit_bom, ...) is not an intent and is refused.

## Safety boundaries (enforced in code)

- The LLM has no tools and no write path; it only returns text. Approval stays in `POST /proposals/{id}/approve|reject`, which requires the planner role. The agent creates schedules only as `PENDING_APPROVAL`.
- All numbers and feasibility come from the planning service; model text with unknown numbers (2+ digits) is withheld.
- Prompts are not access control: permissions are checked in `authorize` and in the approval endpoint.
- Question and explanation text are not logged; logs carry intent, role, status codes only.
- Schedule facts and a deterministic summary are returned even when the model or guardrail is down.

## API for Phase 4

`POST /agent` with headers `X-Role`, `X-User` (placeholder for real auth) and body `{intent, question?, schedule_id?, scenario?, as_of?, horizon_days?}`. Response: `status`, `error`, `schedule_id`, `facts`, `deterministic_summary`, `explanation`, `explanation_status`, `notes`, `approval_required: true`. Show `deterministic_summary`/`facts` always; show `explanation` only when `explanation_status == "GENERATED"`, otherwise show the status. Programmatic use: `agent.run(req, role, user)`.

## Known gaps

Role comes from a header and state is in memory (`app/store.py`); replace with real auth and persistence before sharing. No retries/timeouts/concurrency cap for Domo yet (another project here observed Domo queues parallel calls; cap in-flight calls when the adapter is implemented).
