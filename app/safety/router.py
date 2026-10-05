from __future__ import annotations

from fastapi import APIRouter, Query

from app.safety.schemas import LiftSignature, NoticeAck, ReportSubmit, ReportSupplement, RuleConfig, TriageDecision
from app.safety.service import SafetyService

router = APIRouter(prefix="/api/safety", tags=["跨场地安全处置链"])


def service() -> SafetyService:
    return SafetyService()


@router.get("/rules")
def get_rule():
    return service().get_rule()


@router.put("/rules")
def configure_rule(payload: RuleConfig):
    return service().configure_rule(payload.model_dump())


@router.post("/reports", status_code=201)
def submit_report(payload: ReportSubmit):
    return service().submit_report(payload.model_dump())


@router.get("/reports")
def list_reports(product_code: str | None = None, status: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_reports(product_code=product_code, status=status, limit=limit)}


@router.get("/reports/{report_id}")
def report_detail(report_id: int):
    return service().report_detail(report_id)


@router.post("/reports/{report_id}/supplements", status_code=201)
def add_supplement(report_id: int, payload: ReportSupplement):
    return service().add_supplement(report_id, payload.model_dump())


@router.post("/reports/{report_id}/triage", status_code=201)
def triage(report_id: int, payload: TriageDecision):
    return service().triage(report_id, payload.model_dump())


@router.post("/actions/{action_id}/investigation")
def finalize_investigation(action_id: int, payload: LiftSignature):
    # 复用审阅人+理由的入参结构：写定调查结论，不改写既有时序。
    return service().finalize_investigation(action_id, payload.reviewer, payload.reason)


@router.post("/actions/{action_id}/lift-signatures", status_code=201)
def lift_signature(action_id: int, payload: LiftSignature):
    return service().lift_signature(action_id, payload.model_dump())


@router.get("/actions/{action_id}")
def action_detail(action_id: int):
    return service().action_detail(action_id)


@router.get("/actions")
def list_actions(product_code: str | None = None):
    return {"items": service().list_actions(product_code)}


@router.post("/notices/deliver-pending")
def deliver_pending(limit: int = Query(default=100, ge=1, le=1000)):
    return service().deliver_pending_notices(limit=limit)


@router.get("/notices")
def list_notices(
    status: str | None = None,
    site_code: str | None = None,
    product_code: str | None = None,
    session_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": service().list_notices(
        status=status, site_code=site_code, product_code=product_code, session_id=session_id, limit=limit,
    )}


@router.post("/notices/{notice_id}/acknowledge")
def acknowledge_notice(notice_id: int, payload: NoticeAck):
    return service().acknowledge_notice(notice_id, payload.site_code)


@router.get("/products/{product_code}/dossier")
def product_dossier(product_code: str):
    return service().product_dossier(product_code)
