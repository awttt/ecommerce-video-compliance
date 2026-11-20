from sqlalchemy.orm import Session

from admin_app.models import (
    AuditLog,
    Device,
    Invite,
    KeywordEntry,
    KeywordSubmission,
    Member,
    QuotaAllocation,
    Tenant,
    TenantRules,
)
from admin_app.money import yuan
from admin_app.permissions import PERMISSIONS, ROLE_LABEL
from admin_app.service import seats_used
from admin_app.timeutil import iso

ROLE_STATUS = {
    "active": "正常",
    "disabled": "已停用",
    "invited": "待接受",
}
TENANT_STATUS = {"active": "在营", "disabled": "已停用"}
KEYWORD_STATUS = {"pending": "待审", "approved": "已通过", "rejected": "已驳回"}


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
    lifetime = 0
    relay_balance = 0
    local_balance = 0
    checked_at = None
    for member in db.query(Member).filter(Member.tenant_id == tenant.id, Member.deleted_at.is_(None)):
        month += member.month_cost_cents
        lifetime += max(member.lifetime_cost_cents, member.month_cost_cents)
        # 公司总额 = 所有成员余额之和（公司管理员也是成员）。
        local_balance += max(member.balance_cents, 0)
        if member.relay_checked_at is not None:
            relay_balance += member.relay_balance_cents
            if checked_at is None or member.relay_checked_at > checked_at:
                checked_at = member.relay_checked_at
    return {
        "id": tenant.id,
        "name": tenant.name,
        "status": tenant.status,
        "status_label": TENANT_STATUS.get(tenant.status, tenant.status),
        "seats_used": seats_used(db, tenant.id),
        "seat_quota": tenant.seat_quota,
        "relay_group_id": tenant.relay_group_id,
        "relay_group_name": tenant.relay_group_name,
        # 公司不单独充值：总额 = 余额 + 已消耗，可分配就是当前余额。
        "pool_total": yuan(local_balance + lifetime),
        "pool_remaining": yuan(local_balance),
        "allocated": yuan(local_balance + lifetime),
        "month_cost": yuan(month),
        "relay_balance": yuan(relay_balance),
        "relay_diff": yuan(relay_balance - local_balance) if checked_at else None,
        "relay_checked_at": iso(checked_at),
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
        "relay_balance": yuan(member.relay_balance_cents),
        "relay_diff": yuan(member.relay_balance_cents - member.balance_cents) if member.relay_checked_at else None,
        "relay_status": member.relay_status,
        "relay_checked_at": iso(member.relay_checked_at),
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
