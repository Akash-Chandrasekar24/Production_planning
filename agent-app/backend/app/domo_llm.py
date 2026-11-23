"""Domo AI Text Generation as a LangChain LLM (guide section 6.4, Appendix A).

POST https://{DOMO_SUBDOMAIN}.domo.com/api/ai/v1/text/generation with header X-DOMO-Developer-Token.
STATUS: written to the documented contract but NOT exercised against the live service from this repo (no DOMO_TOKEN was available
when it was built); unit tests use a fake transport. The token is read from Settings only and is never logged or returned.
"""
import logging
from typing import Any, List, Optional
import httpx
from langchain_core.language_models.llms import LLM
from .config import Settings

log = logging.getLogger("planner.domo")


class DomoError(Exception):
    """Domo call failed or is not configured. `code` is safe to show to users; message never contains the token."""
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


# Guide section 11 "structured JSON output": the exact responseFormat "type" must be confirmed in the Domo Playground.
JSON_ACTION_FORMAT = {"type": "json", "schema": {"type": "object", "properties": {
    "action": {"type": "string", "description": "tool name or 'final'"},
    "input": {"type": "object", "description": "tool arguments", "properties": {}},
    "answer": {"type": "string", "description": "final answer"}}, "required": ["action"]}}


class DomoTextLLM(LLM):
    temperature: float = 0.0          # guide hardening: temperature 0 for valid JSON actions
    max_tokens: int = 1500
    system: Optional[str] = None
    session_id: Optional[str] = None  # only sent when DOMO_USE_SESSION_ID=true: live test showed Domo 404s on session IDs it did not create

    @property
    def _llm_type(self) -> str:
        return "domo-text-generation"

    def _post(self, s: Settings, prompt: str, model: str | None, stop) -> str:
        payload: dict = {"input": prompt, "temperature": self.temperature, "maxTokens": self.max_tokens}
        if model:
            payload["model"] = model
        if self.system:
            payload["system"] = self.system
        if self.session_id and s.domo_use_session_id:
            payload["sessionId"] = self.session_id
        if s.domo_json_mode:
            payload["responseFormat"] = JSON_ACTION_FORMAT
        try:
            r = httpx.post(f"https://{s.domo_subdomain}.domo.com/api/ai/v1/text/generation", json=payload, timeout=120,
                           headers={"X-DOMO-Developer-Token": s.domo_token.get_secret_value(), "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise DomoError("LLM_UNREACHABLE", f"Could not reach Domo ({type(e).__name__})")
        if r.status_code == 403:
            raise DomoError("LLM_FORBIDDEN", "Domo returned 403: check the token and that the model is enabled for Text Generation", 403)
        if r.status_code == 409:
            raise DomoError("LLM_CONFLICT", "Domo returned 409: model or session configuration conflict", 409)
        if r.status_code >= 400:
            raise DomoError("LLM_ERROR", f"Domo returned HTTP {r.status_code}", r.status_code)
        body = r.json()
        u = body.get("modelProviderUsage") or {}
        log.info("domo usage in=%s out=%s total=%s model=%s", u.get("inputTokens"), u.get("outputTokens"), u.get("totalTokens"), body.get("modelId"))
        out = body["output"]
        for s_ in stop or []:
            out = out.split(s_)[0]
        return out

    def _call(self, prompt: str, stop: Optional[List[str]] = None, run_manager=None, **kwargs: Any) -> str:
        s = Settings()
        if not (s.domo_subdomain and s.domo_token and s.domo_token.get_secret_value()):
            raise DomoError("LLM_NOT_CONFIGURED", "Domo is not configured: set DOMO_SUBDOMAIN and DOMO_TOKEN in the backend environment")
        # primary model first; if that call fails for any reason, retry the same request ONCE on the fallback model
        models = [m for i, m in enumerate([s.domo_model, s.domo_fallback_model]) if m and m not in ([s.domo_model] if i else [])] or [None]
        for n, model in enumerate(models):
            try:
                return self._post(s, prompt, model, stop)
            except DomoError as e:
                if n == len(models) - 1:
                    raise
                log.warning("domo primary model failed (%s); falling back to %s", e.code, models[n + 1])
