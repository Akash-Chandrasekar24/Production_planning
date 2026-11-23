"""Neon MCP binding, tested against a mock MCP server. The live Neon server needs NEON_API_KEY (not available), so it is not exercised."""
import json
import httpx
import pytest
from app import neon_mcp, tools


def mock_neon(monkeypatch, seen, props=None, required=("sql", "projectId"), error=False):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                     "session": request.headers.get("mcp-session-id"), "method": method, "args": (body.get("params") or {}).get("arguments")})
        if method == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "sess-1"},
                                  json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            schema = {"properties": props or {"sql": {}, "projectId": {}}, "required": list(required)}
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "run_sql", "inputSchema": schema}]}}
            return httpx.Response(200, text="event: message\ndata: " + json.dumps(msg) + "\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"isError": error, "content": [{"type": "text", "text": '[{"n": 15}]'}]}})
    monkeypatch.setattr(neon_mcp, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def configure(monkeypatch):
    monkeypatch.setenv("NEON_API_KEY", "neon-sekret-key")
    monkeypatch.setenv("NEON_PROJECT_ID", "proj-123")


def test_protocol_order_session_auth_readonly_and_schema_driven_args(monkeypatch):
    configure(monkeypatch)
    seen = []
    mock_neon(monkeypatch, seen)
    out = tools.neon_run_sql.invoke({"query": "select count(*) as n from src_dim_machine"})
    assert out == '[{"n": 15}]'
    assert [s["method"] for s in seen] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert all("readonly=true" in s["url"] and "projectId=proj-123" in s["url"] and s["auth"] == "Bearer neon-sekret-key" for s in seen)
    assert seen[0]["session"] is None and all(s["session"] == "sess-1" for s in seen[1:])      # session id carried after initialize
    assert seen[-1]["args"] == {"sql": "select count(*) as n from src_dim_machine", "projectId": "proj-123"}   # from the advertised schema


def test_arguments_follow_advertised_names_and_unknown_required_args_are_refused(monkeypatch):
    configure(monkeypatch)
    seen = []
    mock_neon(monkeypatch, seen, props={"query": {}, "project_id": {}}, required=("query", "project_id"))
    assert tools.neon_run_sql.invoke({"query": "select 1"}) == '[{"n": 15}]'
    assert seen[-1]["args"] == {"query": "select 1", "project_id": "proj-123"}
    with pytest.raises(neon_mcp.NeonMcpError):
        neon_mcp._build_args({"properties": {"sql": {}, "mystery": {}}, "required": ["sql", "mystery"]}, "select 1", neon_mcp.Settings())


def test_writes_are_blocked_before_any_network_call(monkeypatch):
    configure(monkeypatch)
    seen = []
    mock_neon(monkeypatch, seen)
    for q in ("delete from src_dim_machine", "select 1; drop table x", "update src_dim_machine set machine_name='x'"):
        assert tools.neon_run_sql.invoke({"query": q}).startswith("ERROR: only a single read-only SELECT")
    assert seen == []


def test_server_errors_and_missing_config_are_clear_and_never_leak_the_key(monkeypatch):
    configure(monkeypatch)
    mock_neon(monkeypatch, [], error=True)
    r = tools.neon_run_sql.invoke({"query": "select 1"})
    assert r.startswith("ERROR: query failed") and "neon-sekret-key" not in r
    monkeypatch.setattr(neon_mcp, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401))))
    r = tools.neon_run_sql.invoke({"query": "select 1"})
    assert "refused the credentials" in r and "neon-sekret-key" not in r
    monkeypatch.setenv("NEON_API_KEY", "")
    assert "not configured" in tools.neon_run_sql.invoke({"query": "select 1"})


def test_get_schema_falls_back_to_mcp_when_no_local_role(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(tools, "ro_engine", None)
    seen = []
    mock_neon(monkeypatch, seen)
    assert tools.get_schema.invoke({}) == '[{"n": 15}]' and seen[-1]["args"]["sql"].startswith("select table_name, string_agg")
    monkeypatch.setenv("NEON_API_KEY", "")
    assert "disabled" in tools.get_schema.invoke({})                                    # neither route configured -> clear message
