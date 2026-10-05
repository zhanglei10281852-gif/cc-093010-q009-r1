from __future__ import annotations

from fastapi import APIRouter, Query

from app.safety.schemas import (
    AdverseEventCreate,
    LiftApproval,
    NotificationAck,
    ReportSupplement,
    RuleUpdate,
    TriageDecision,
    InvestigationConclusion,
)
from app.safety.service import SafetyService

router = APIRouter(prefix="/api/safety", tags=["跨场地安全处置链"])


def service() -> SafetyService:
    return SafetyService()


@router.post("/reports", status_code=201)
def create_report(payload: AdverseEventCreate):
    return service().create_report(payload.model_dump())


@router.get("/reports")
def list_reports(product_code: str | None = None, status: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_reports(product_code=product_code, status=status, limit=limit)}


@router.get("/reports/{report_no}")
def report_detail(report_no: str):
    return service().report_detail(report_no=report_no)


@router.post("/reports/{report_no}/supplement", status_code=201)
def add_supplement(report_no: str, payload: ReportSupplement):
    return service().add_supplement(report_no, payload.model_dump())


@router.post("/reports/{report_no}/triage")
def triage(report_no: str, payload: TriageDecision):
    return service().triage(report_no, payload.model_dump())


@router.post("/investigations/{investigation_no}/conclude")
def conclude_investigation(investigation_no: str, payload: InvestigationConclusion):
    return service().conclude_investigation(investigation_no, payload.model_dump())


@router.post("/decisions/{decision_no}/lift-approvals")
def approve_lift(decision_no: str, payload: LiftApproval):
    return service().approve_lift(decision_no, payload.model_dump())


@router.post("/decisions/{decision_no}/acknowledge")
def acknowledge(decision_no: str, payload: NotificationAck):
    return service().acknowledge(decision_no, payload.model_dump())


@router.post("/notifications/deliver-pending")
def deliver_pending(limit: int = Query(default=100, ge=1, le=500)):
    return service().deliver_pending(limit=limit)


@router.post("/notifications/requeue-failed")
def requeue_failed():
    return service().requeue_failed_notifications()


@router.get("/products/{product_code}/overview")
def product_overview(product_code: str):
    return service().product_safety_overview(product_code)


@router.get("/rules")
def list_rules():
    return {"items": service().list_rules()}


@router.patch("/rules/{code}")
def update_rule(code: str, payload: RuleUpdate, actor: str = Query(..., min_length=1)):
    return service().update_rule(code, payload.model_dump(exclude_unset=True), actor)
