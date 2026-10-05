from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["mild", "moderate", "serious", "critical"]
TriageAction = Literal["request_info", "exclude", "investigate", "suspend"]


class ReportSource(BaseModel):
    channel: str = Field(min_length=1, max_length=60)
    external_ref: str = Field(default="", max_length=160)
    reporter: str = Field(default="", max_length=120)


class AdverseEventCreate(BaseModel):
    product_code: str = Field(min_length=2, max_length=64)
    session_id: int | None = None
    site_code: str = Field(default="", max_length=120)
    symptoms: list[str] = Field(min_length=1, max_length=50)
    severity: Severity
    event_occurred_at: str = Field(min_length=5, max_length=40)
    description: str = Field(default="", max_length=4000)
    source: ReportSource
    duplicate_of_report_no: str | None = Field(default=None, max_length=40)

    @field_validator("product_code", "site_code")
    @classmethod
    def normalize_lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("symptoms")
    @classmethod
    def strip_symptoms(cls, value: list[str]) -> list[str]:
        symptoms = [item.strip().lower() for item in value if item.strip()]
        if not symptoms:
            raise ValueError("至少填写一个症状")
        return list(dict.fromkeys(symptoms))


class TriageDecision(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    action: TriageAction
    note: str = Field(default="", max_length=4000)


class ReportSupplement(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=60)
    external_ref: str = Field(default="", max_length=160)
    content: str = Field(min_length=1, max_length=8000)


class InvestigationConclusion(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    conclusion: str = Field(min_length=4, max_length=8000)


class SuspensionIssue(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=4, max_length=4000)


class LiftApproval(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=2, max_length=4000)


class NotificationAck(BaseModel):
    recipient: str = Field(min_length=1, max_length=120)
    channel: str = Field(default="site", max_length=60)
    actor: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=2000)
    occurred_at: str | None = Field(default=None, max_length=40)


class RuleUpdate(BaseModel):
    min_count: int | None = Field(default=None, ge=1, le=1000)
    window_seconds: int | None = Field(default=None, ge=60, le=365 * 24 * 3600)
    active: bool | None = None
