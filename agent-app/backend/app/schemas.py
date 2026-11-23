"""API request/response schemas (domain models are reused for Schedule / Scenario)."""
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class GenerateRequest(BaseModel):
    as_of: str | None = Field(None, description="ISO datetime the plan starts; default = day after the last inventory snapshot")
    horizon_days: int = Field(60, ge=1, le=365)

    @field_validator("as_of")
    @classmethod
    def _iso(cls, v):
        if v is not None:
            datetime.fromisoformat(v)          # ValueError -> 422 INVALID_REQUEST
        return v


class ApprovalRequest(BaseModel):
    note: str | None = Field(None, max_length=500)
    acknowledge_risks: bool = Field(False, description="Required when the schedule carries risks (late jobs, unscheduled demand, error diagnostics)")


class DecisionResult(BaseModel):
    id: str
    status: str
    decided_by: str
    acknowledged_risks: list[dict] = []


class ScheduleSummary(BaseModel):
    id: str
    name: str
    base_id: str | None = None
    adopted: bool = False
    fully_approved: bool = False                # a what-if someone added to the plan: counts as a plan
    status: str
    feasibility: str
    as_of: str
    created_by: str
    created_at: str | None = None
    decided_by: str | None = None
    summary: dict = {}
    line_utilisation: dict = {}


class ScenarioResult(BaseModel):
    id: str
    comparison: list[dict]


class DQIssue(BaseModel):
    code: str
    count: int
    severity: str
    message: str


class AuditEvent(BaseModel):
    ts: str
    actor: str
    role: str
    action: str
    detail: dict


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str | None = None
