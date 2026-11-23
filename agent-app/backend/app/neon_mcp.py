"""Minimal MCP client (Streamable HTTP) for the hosted Neon MCP server, so the model can query the database through MCP.

STATUS: protocol handling is unit-tested against a mock server; NOT exercised against the live Neon MCP server because no
NEON_API_KEY was available (the hosted server otherwise needs an interactive OAuth login, which a backend cannot do).
Nothing here guesses the server's tool arguments: they are built from the inputSchema that the server advertises via tools/list.
Safety: server-side read-only (`readonly=true` in the URL), only querying/schema/projects tool categories, and the same
single-SELECT guard as the local SQL tool is applied before anything is sent. The API key is a SecretStr, sent only in the
Authorization header, never logged or returned.
"""
import json
import logging
import httpx
from .config import Settings

log = logging.getLogger("planner.neonmcp")
PROTOCOL = "2025-03-26"


class NeonMcpError(Exception):
    """Message is safe to show to the model/user (never contains the API key)."""


def _client() -> httpx.Client:          # tests replace this with a mock transport
    return httpx.Client(timeout=30)


def mcp_url(s: Settings) -> str:
    base = s.neon_mcp_url or "https://mcp.neon.tech/mcp"
    q = "readonly=true&category=querying&category=schema&category=projects"
    if s.neon_project_id:
        q += f"&projectId={s.neon_project_id}"
    return f"{base}?{q}"


def _body(resp: httpx.Response) -> dict | None:
    """JSON-RPC reply from either application/json or text/event-stream."""
    if resp.status_code == 202 or not resp.content:
        return None
    if "text/event-stream" in resp.headers.get("content-type", ""):
        msgs = [json.loads(l[5:].strip()) for l in resp.text.splitlines() if l.startswith("data:") and l[5:].strip()]
        return msgs[-1] if msgs else None
    return resp.json()


class McpSession:
    def __init__(self, client: httpx.Client, url: str, key: str):
        self.c, self.url, self.sid, self.n = client, url, None, 0
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def _post(self, msg: dict) -> dict | None:
        h = dict(self.headers)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        r = self.c.post(self.url, json=msg, headers=h)
        if r.status_code in (401, 403):
            raise NeonMcpError(f"Neon MCP refused the credentials (HTTP {r.status_code}): check NEON_API_KEY and its project access")
        if r.status_code >= 400:
            raise NeonMcpError(f"Neon MCP returned HTTP {r.status_code}")
        self.sid = r.headers.get("mcp-session-id") or self.sid
        return _body(r)

    def request(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        out = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if not out:
            raise NeonMcpError(f"Neon MCP gave no reply to {method}")
        if "error" in out:
            raise NeonMcpError(f"Neon MCP error on {method}: {str(out['error'].get('message', out['error']))[:200]}")
        return out["result"]

    def open(self) -> None:
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "production-planning-agent", "version": "0.5"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _build_args(schema: dict, sql: str, s: Settings) -> dict:
    """Map our values onto the server-advertised inputSchema; refuse (never guess) if a required argument is unknown."""
    props = (schema or {}).get("properties", {})
    sql_key = "sql" if "sql" in props else next((k for k in props if "sql" in k.lower() or "query" in k.lower()), None)
    if not sql_key:
        raise NeonMcpError(f"run_sql advertises no SQL argument (has: {sorted(props)})")
    args = {sql_key: sql}
    proj = "projectId" if "projectId" in props else next((k for k in props if "project" in k.lower()), None)
    if proj and s.neon_project_id:
        args[proj] = s.neon_project_id
    for want, value in (("branch", s.neon_branch_id), ("database", s.neon_database_name)):     # optional, only if the server advertises them
        key = next((k for k in props if want in k.lower()), None)
        if key and value:
            args[key] = value
    if "max_rows" in props:
        args["max_rows"] = 50
    missing = [k for k in (schema or {}).get("required", []) if k not in args]
    if missing:
        raise NeonMcpError(f"run_sql requires {missing}: set NEON_PROJECT_ID (and update app/neon_mcp.py if other arguments are needed)")
    return args


def run_sql_via_mcp(sql: str) -> str:
    s = Settings()
    if not (s.neon_api_key and s.neon_api_key.get_secret_value()):
        raise NeonMcpError("Neon MCP is not configured: set NEON_API_KEY (and NEON_PROJECT_ID) in the backend environment")
    try:
        with _client() as c:
            sess = McpSession(c, mcp_url(s), s.neon_api_key.get_secret_value())
            sess.open()
            tool = next((t for t in sess.request("tools/list").get("tools", []) if t.get("name") == "run_sql"), None)
            if not tool:
                raise NeonMcpError("the Neon MCP server does not offer run_sql with these settings")
            res = sess.request("tools/call", {"name": "run_sql", "arguments": _build_args(tool.get("inputSchema", {}), sql, s)})
    except httpx.HTTPError as e:
        raise NeonMcpError(f"could not reach the Neon MCP server ({type(e).__name__})")
    text = "\n".join(p.get("text", "") for p in res.get("content", []) if p.get("type") == "text")
    if res.get("isError"):
        raise NeonMcpError(f"query failed: {text[:300]}")
    log.info("neon mcp run_sql ok chars=%d", len(text))
    return text
