"""One error envelope for every failure: {"error": {"code", "message", "detail"}, "request_id"}."""
import logging
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("planner.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail=None):
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _resp(request: Request, status: int, code: str, message: str, detail=None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "detail": detail},
                         "request_id": getattr(request.state, "request_id", None)}, status_code=status)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(request, e: ApiError):
        return _resp(request, e.status, e.code, e.message, e.detail)

    @app.exception_handler(RequestValidationError)
    async def _val(request, e: RequestValidationError):
        fields = [{"field": ".".join(str(x) for x in err["loc"] if x != "body"), "problem": err["msg"]} for err in e.errors()]
        return _resp(request, 422, "INVALID_REQUEST", "Request failed validation", fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request, e: StarletteHTTPException):
        return _resp(request, e.status_code, f"HTTP_{e.status_code}", str(e.detail))

    @app.exception_handler(FileNotFoundError)
    async def _nofile(request, e):
        log.error("data files unavailable")
        return _resp(request, 503, "DATA_UNAVAILABLE", "Source data files could not be loaded; set PLANNING_DATA_DIR or restore Data/")

    @app.exception_handler(Exception)
    async def _any(request, e):
        log.error("unhandled %s", type(e).__name__)           # type only: never log messages/bodies
        return _resp(request, 500, "INTERNAL_ERROR", "Unexpected server error")
