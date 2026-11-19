from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Device,
    Finding,
    Invite,
    KeywordEntry,
    KeywordSubmission,
    Member,
    QuotaAllocation,
    ReviewJob,
    Tenant,
    TenantRules,
)
from app.money import yuan
from app.permissions import PERMISSIONS, ROLE_LABEL
from app.service import seats_used
from app.timeutil import iso

ROLE_STATUS = {
    "active": "正常",
    "disabled": "已停用",
    "invited": "待接受",
}
TENANT_STATUS = {"active": "在营", "disabled": "已停用"}
KEYWORD_STATUS = {"pending": "待审", "approved": "已通过", "rejected": "已驳回"}
JOB_STATUS = {"queued": "排队", "running": "识别中", "done": "已完成", "failed": "失败"}


def member_brief(db: Session, member: Member | None) -> dict | None:
    if member is None:
        return None
    tenant = db.get(Tenant, member.tenant_id) if member.tenant_id else None
    return {
        "id": member.id,
        "email": member.email,
        "name": member.name,
        "role": member.role,
        "role_label": ROLE_LABEL.get(member.role, member.role),
        "tenant_id": member.tenant_id,
        "tenant_name": tenant.name if tenant else "",
        "permissions": sorted(PERMISSIONS.get(member.role, set())),
    }


def tenant_dict(db: Session, tenant: Tenant) -> dict:
    month = 0
    for member in db.query(Member).filter(Member.tenant_id == tenant.id, Member.deleted_at.is_(None)):
        month += member.month_cost_cents
    return {
        "id": tenant.id,
        "name": tenant.name,
        "status": tenant.status,
        "status_label": TENANT_STATUS.get(tenant.status, tenant.status),
        "seats_used": seats_used(db, tenant.id),
        "seat_quota": tenant.seat_quota,
        "relay_group_id": tenant.relay_group_id,
        "relay_group_name": tenant.relay_group_name,
        "pool_total": yuan(tenant.pool_total_cents),
        "pool_remaining": yuan(tenant.pool_balance_cents),
        "allocated": yuan(tenant.allocated_total_cents),
        "month_cost": yuan(month),
        "created_at": iso(tenant.created_at),
    }


def member_dict(db: Session, member: Member) -> dict:
    tenant = db.get(Tenant, member.tenant_id) if member.tenant_id else None
    devices = (
        db.query(Device)
        .filter(Device.member_id == member.id, Device.revoked_at.is_(None))
        .count()
    )
    shown = member.balance_cents if member.balance_cents > 0 else 0
    return {
        "id": member.id,
        "tenant_id": member.tenant_id,
        "tenant_name": tenant.name if tenant else "",
        "email": member.email,
        "name": member.name,
        "role": member.role,
        "role_label": ROLE_LABEL.get(member.role, member.role),
        "relay_user_id": member.relay_user_id,
        "group_name": tenant.relay_group_name if tenant else "",
        "status": member.status,
        "status_label": ROLE_STATUS.get(member.status, member.status),
        "balance": yuan(shown),
        "overdraft": bool(member.balance_cents < 0 or member.overdraft),
        "month_cost": yuan(member.month_cost_cents),
        "lifetime_cost": yuan(member.lifetime_cost_cents),
        "month_requests": member.month_requests,
        "month_input_tokens": member.month_input_tokens,
        "month_output_tokens": member.month_output_tokens,
        "devices": devices,
        "created_at": iso(member.created_at),
    }


def device_dict(device: Device) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "client_version": device.client_version,
        "last_seen": iso(device.last_seen),
        "revoked_at": iso(device.revoked_at),
        "active": device.revoked_at is None,
    }


def invite_dict(row: Invite, tenant_name: str) -> dict:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "tenant_name": tenant_name,
        "email": row.email,
        "name": row.name,
        "role": row.role,
        "role_label": ROLE_LABEL.get(row.role, row.role),
        "initial_amount": yuan(row.initial_amount_cents),
        "expires_at": iso(row.expires_at),
        "used_at": iso(row.used_at),
    }


def quota_dict(db: Session, row: QuotaAllocation) -> dict:
    member = db.get(Member, row.member_id) if row.member_id else None
    operator = db.get(Member, row.operator_id) if row.operator_id else None
    labels = {"processing": "处理中", "success": "成功", "failed": "失败"}
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "member_id": row.member_id,
        "member_name": member.name if member else "全体",
        "amount": yuan(row.amount_cents),
        "direction": row.direction,
        "direction_label": "退回" if row.direction == "subtract" else "分配",
        "period": row.period,
        "remark": row.remark,
        "idempotency_key": row.idempotency_key,
        "operator_name": operator.name if operator else "",
        "status": row.status,
        "status_label": labels.get(row.status, row.status),
        "summary": row.summary,
        "created_at": iso(row.created_at),
    }


def submission_dict(db: Session, row: KeywordSubmission) -> dict:
    author = db.get(Member, row.member_id)
    tenant = db.get(Tenant, row.tenant_id) if row.tenant_id else None
    return {
        "id": row.id,
        "text": row.text,
        "category": row.category,
        "source": row.source,
        "scope": row.scope,
        "scope_label": "全局" if row.scope == "global" else "公司",
        "status": row.status,
        "status_label": KEYWORD_STATUS.get(row.status, row.status),
        "member_id": row.member_id,
        "member_name": author.name if author else "",
        "tenant_id": row.tenant_id,
        "tenant_name": tenant.name if tenant else "全局",
        "remark": row.remark,
        "entry_id": row.entry_id,
        "created_at": iso(row.created_at),
        "reviewed_at": iso(row.reviewed_at),
    }


def entry_dict(db: Session, row: KeywordEntry) -> dict:
    tenant = db.get(Tenant, row.tenant_id) if row.tenant_id else None
    return {
        "id": row.id,
        "text": row.text,
        "category": row.category,
        "source": row.source,
        "scope": "global" if row.tenant_id is None else "tenant",
        "scope_label": "全局" if row.tenant_id is None else "公司",
        "tenant_name": tenant.name if tenant else "全局",
        "version": row.version,
        "deleted": row.deleted_at is not None,
    }


def rules_dict(row: TenantRules | None) -> dict:
    return {
        "categories": list(row.categories) if row else ["价格", "规格", "品牌", "夸大宣传"],
        "ignore_phrases": list(row.ignore_phrases) if row else [],
        "note": row.note if row else "",
        "version": row.version if row else 0,
        "updated_at": iso(row.updated_at) if row else None,
    }


def audit_dict(db: Session, row: AuditLog) -> dict:
    actor = db.get(Member, row.actor_id) if row.actor_id else None
    tenant = db.get(Tenant, row.tenant_id) if row.tenant_id else None
    return {
        "id": row.id,
        "created_at": iso(row.created_at),
        "actor_name": actor.name if actor else "系统",
        "role": row.role,
        "role_label": ROLE_LABEL.get(row.role, row.role),
        "tenant_name": tenant.name if tenant else "—",
        "action": row.action,
        "object": row.object,
        "summary": row.summary,
        "before": row.before,
        "after": row.after,
        "ip": row.ip,
    }


def finding_dict(row: Finding) -> dict:
    return {
        "时间点": row.time_range,
        "类别": row.category,
        "来源": row.source,
        "原文": row.text,
        "位置": row.position,
        "置信度": row.confidence,
        "判定理由": row.reason,
        "备注": row.note,
    }


def job_dict(db: Session, job: ReviewJob, *, with_findings: bool = False) -> dict:
    member = db.get(Member, job.member_id)
    tenant = db.get(Tenant, job.tenant_id)
    hits = db.query(Finding).filter(Finding.job_id == job.id).count()
    body = {
        "id": job.id,
        "filename": job.filename,
        "tenant_id": job.tenant_id,
        "tenant_name": tenant.name if tenant else "",
        "member_id": job.member_id,
        "member_name": member.name if member else "",
        "duration_label": job.duration_label,
        "status": job.status,
        "status_label": JOB_STATUS.get(job.status, job.status),
        "frame_done": job.frame_done,
        "frame_total": job.frame_total,
        "hit_count": hits,
        "message": job.message,
        "has_file": bool(job.file_path),
        "created_at": iso(job.created_at),
    }
    if with_findings:
        rows = db.query(Finding).filter(Finding.job_id == job.id).order_by(Finding.sort, Finding.id).all()
        body["findings"] = [finding_dict(row) for row in rows]
    return body
