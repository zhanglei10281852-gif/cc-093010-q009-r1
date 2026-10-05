from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["mild", "moderate", "severe", "life_threatening", "death"]
SEVERITY_VALUES = ("mild", "moderate", "severe", "life_threatening", "death")


class ReportSubmit(BaseModel):
    report_key: str = Field(min_length=6, max_length=120, description="提交方生成的幂等键，同一事件跨渠道复用")
    product_code: str = Field(min_length=2, max_length=64)
    site_code: str = Field(default="", max_length=64)
    session_id: int | None = Field(default=None, ge=1)
    severity: Severity
    symptoms: list[str] = Field(min_length=1, max_length=50)
    occurrence_at: str = Field(min_length=4, max_length=40, description="事件发生时间（ISO 8601）")
    description: str = Field(default="", max_length=4000)
    channel: str = Field(min_length=1, max_length=80, description="上报渠道，如现场系统/热线/监管通报")
    reporter_ref: str = Field(default="", max_length=120, description="渠道内报告人或报告编号摘要")
    signal_key: str = Field(default="", max_length=120, description="同类信号归并键，缺省按症状集合归并")

    @model_validator(mode="after")
    def normalize_symptoms(self) -> "ReportSubmit":
        cleaned = [item.strip() for item in self.symptoms if item.strip()]
        if not cleaned:
            raise ValueError("至少需要一项症状描述")
        self.symptoms = cleaned
        return self


class ReportSupplement(BaseModel):
    """补充材料或迟到回执；迟到回执只能追加到原时间线。"""

    actor: str = Field(min_length=1, max_length=120)
    kind: Literal["supplement", "late_receipt"] = "supplement"
    summary: str = Field(min_length=1, max_length=400)
    detail: dict = Field(default_factory=dict)
    occurred_at: str | None = Field(default=None, max_length=40, description="回执对应的原始发生时间，用于标记迟到")


class TriageDecision(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    decision: Literal["request_information", "exclude_relation", "escalate_investigation", "trigger_suspension"]
    reason: str = Field(min_length=2, max_length=2000)
    related_finding: str = Field(default="", max_length=2000, description="因果关联判断；排除关联时必填")

    @model_validator(mode="after")
    def require_finding_for_exclusion(self) -> "TriageDecision":
        if self.decision == "exclude_relation" and not self.related_finding.strip():
            raise ValueError("排除关联必须给出医学依据")
        return self


class LiftSignature(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=2000, description="第一名审阅人登记解除理由；第二名可留空")


class RuleConfig(BaseModel):
    severe_severities: list[Severity] = Field(min_length=1, max_length=5)
    cluster_signal_count: int = Field(ge=2, le=100)
    cluster_window_hours: int = Field(ge=1, le=24 * 365)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_severities(self) -> "RuleConfig":
        if any(value not in SEVERITY_VALUES for value in self.severe_severities):
            raise ValueError("严重程度取值不合法")
        return self


class NoticeAck(BaseModel):
    site_code: str = Field(min_length=1, max_length=120)
