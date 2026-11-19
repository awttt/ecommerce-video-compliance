import csv
import io
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import auth_relay
from app.config import get_settings
from app.db import get_db
from app.deps import client_ip, current_user, require
from app.models import (
    AppSetting,
    AuditLog,
    Device,
    Invite,
    KeywordSubmission,
    Member,
    QuotaAllocation,
    ReviewJob,
    SessionToken,
    Tenant,
    TenantRules,
)
from app.present import (
    audit_dict,
    device_dict,
    entry_dict,
    invite_dict,
    job_dict,
    member_brief,
    member_dict,
    quota_dict,
    rules_dict,
    submission_dict,
    tenant_dict,
)
from app.security import (
    new_access_token,
    new_refresh_value,
    sha256_text,
)
from app.service import (
    accept_invite,
    allocate,
    audit,
    approve_keyword,
    bind_device,
    create_tenant,
    create_member,
    delete_keyword,
    disable_member,
    enable_member,
    invite_member,
    pending_count,
    reconcile,
    register_job,
    reject_keyword,
    resolve_tenant,
    revoke_device,
    rules_payload,
    save_rule_settings,
    set_queue_paused,
    set_tenant_status,
    submit_keyword,
    trend,
    usage_rows,
    visible_member_ids,
)
from app.timeutil import utcnow

router = APIRouter(prefix="/v1")
_COLUMNS = ("时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注")


class LoginBody(BaseModel):
    email: str
    password: str


class TotpBody(BaseModel):
    challenge: str = ""
    totp_code: str = ""


class RefreshBody(BaseModel):
    refresh_token: str


class TenantBody(BaseModel):
    name: str
    admin_email: str
    admin_name: str = ""
    admin_password: str
    pool: float = 0
    seats: int = 10


class StatusBody(BaseModel):
    status: str
    disable_members: bool = False


class InviteBody(BaseModel):
    email: str
    name: str = ""
    role: str = "member"
    tenant_id: int | None = None
    initial_amount: float = 0


class CreateMemberBody(BaseModel):
    email: str
    name: str = ""
    password: str
    role: str = "member"
    tenant_id: int | None = None


class AcceptBody(BaseModel):
    password: str
    name: str = ""


class QuotaBody(BaseModel):
    member_id: int
    amount: float
    idempotency_key: str
    remark: str = ""
    period: str = ""
    direction: str = "add"


class KeywordBody(BaseModel):
    text: str
    category: str
    source: str = "口播"
    scope: str = "tenant"
    tenant_id: int | None = None


class ReviewBody(BaseModel):
    remark: str = ""
    force: bool = False


class RulesBody(BaseModel):
    tenant_id: int | None = None
    categories: list[str] = Field(default_factory=list)
    ignore_phrases: list[str] = Field(default_factory=list)
    note: str = ""


class SettingsBody(BaseModel):
    relay_base_url: str
    relay_gateway_url: str
    group_name: str
    group_id: str = ""
    backend_mode_enabled: bool = False
    registration_enabled: bool = False
    email_verify_ok: bool = True


class BindBody(BaseModel):
    email: str
    relay_user_id: str
    device_name: str = ""
    client_version: str = ""


def _issue(db: Session, member: Member, response: Response) -> dict:
    access = new_access_token(member_id=member.id, role=member.role, tenant_id=member.tenant_id)
    refresh = new_refresh_value()
    db.add(
        SessionToken(
            member_id=member.id,
            token_hash=sha256_text(refresh),
            expires_at=utcnow() + timedelta(days=14),
        )
    )
    db.commit()
    response.set_cookie(
        "evc_admin_access",
        access,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=12 * 3600,
    )
    user = member_brief(db, member) or {}
    user["pending_keywords"] = pending_count(db, member)
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "user": user}


def _clear_cookie(response: Response) -> None:
    response.delete_cookie("evc_admin_access", path="/")


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "evc-admin"}


def _login_error(exc: auth_relay.RelayLoginError) -> HTTPException:
    status = exc.status if 400 <= exc.status <= 599 else 502
    if exc.message == "邮箱或密码不正确":
        status = 401
    return HTTPException(status_code=status, detail=exc.message)


def _relay_user_id(profile: dict) -> str:
    for key in ("id", "user_id"):
        value = profile.get(key)
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:40]
    return ""


def _prepare_member(db: Session, email: str, profile: dict) -> Member:
    member = db.query(Member).filter(Member.email == email).one_or_none()
    relay_id = _relay_user_id(profile)
    if email == auth_relay.SUPER_ADMIN_EMAIL:
        created = member is None
        previous = None if member is None else (member.role, member.tenant_id, member.status, member.deleted_at)
        if member is None:
            member = Member(
                tenant_id=None,
                email=email,
                name="总管理员",
                role="platform_admin",
                password_hash="",
                relay_user_id=relay_id,
                status="active",
            )
            db.add(member)
        else:
            member.role = "platform_admin"
            member.tenant_id = None
            member.status = "active"
            member.deleted_at = None
            if relay_id:
                member.relay_user_id = relay_id
            if not (member.name or "").strip():
                member.name = "总管理员"
        db.flush()
        if created or previous != ("platform_admin", None, "active", None):
            audit(db, member, "auth.super_admin", email, "确认总管理员")
        return member
    if member is None or member.deleted_at is not None:
        raise HTTPException(status_code=403, detail="这个账号没有后台权限")
    if relay_id and not member.relay_user_id:
        member.relay_user_id = relay_id
    return member


def _ensure_can_enter(db: Session, member: Member) -> None:
    if member.status != "active":
        raise HTTPException(status_code=403, detail="账号已停用")
    if member.tenant_id is not None:
        tenant = db.get(Tenant, member.tenant_id)
        if tenant is None or tenant.status != "active":
            raise HTTPException(status_code=403, detail="公司已停用")


def _session_from_relay(db: Session, response: Response, email: str, data: dict) -> dict:
    if not str(data.get("access_token") or "").strip():
        raise HTTPException(status_code=502, detail="中转站没有返回登录凭证")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    relay_email = str(user.get("email") or "").strip().lower()
    if relay_email:
        try:
            email = auth_relay.normalize_email(relay_email)
        except auth_relay.RelayLoginError:
            pass
    member = _prepare_member(db, email, user)
    _ensure_can_enter(db, member)
    return _issue(db, member, response)


@router.post("/auth/login")
def login(body: LoginBody, response: Response, db: Session = Depends(get_db)) -> dict:
    try:
        email = auth_relay.normalize_email(body.email)
    except auth_relay.RelayLoginError as exc:
        raise _login_error(exc) from exc
    if not body.password:
        raise HTTPException(status_code=400, detail="请填写密码")
    try:
        data = auth_relay.login_password(email, body.password)
    except auth_relay.RelayLoginError as exc:
        raise _login_error(exc) from exc
    if data.get("requires_2fa"):
        temp = str(data.get("temp_token") or "").strip()
        if not temp:
            raise HTTPException(status_code=502, detail="这个账号开启了两步验证，请重新登录")
        return {
            "requires_2fa": True,
            "challenge": auth_relay.remember_2fa(email, temp),
            "email_masked": str(data.get("user_email_masked") or ""),
            "message": "请输入身份验证器中的 6 位数字",
        }
    return _session_from_relay(db, response, email, data)


@router.post("/auth/login/2fa")
def login_2fa(body: TotpBody, response: Response, db: Session = Depends(get_db)) -> dict:
    code = body.totp_code.strip()
    if re.fullmatch(r"\d{6}", code) is None:
        raise HTTPException(status_code=400, detail="请输入 6 位数字验证码")
    try:
        temp, email = auth_relay.peek_2fa(body.challenge.strip())
        data = auth_relay.login_totp(temp, code)
    except auth_relay.RelayLoginError as exc:
        raise _login_error(exc) from exc
    if data.get("requires_2fa"):
        raise HTTPException(status_code=400, detail="这个账号开启了两步验证，请重新登录")
    result = _session_from_relay(db, response, email, data)
    auth_relay.drop_2fa(body.challenge.strip())
    return result


@router.post("/auth/refresh")
def refresh(body: RefreshBody, response: Response, db: Session = Depends(get_db)) -> dict:
    from fastapi import HTTPException

    stored = (
        db.query(SessionToken)
        .filter(SessionToken.token_hash == sha256_text(body.refresh_token or ""))
        .one_or_none()
    )
    if stored is None or stored.revoked_at is not None or stored.expires_at < utcnow():
        raise HTTPException(status_code=401, detail="登录已失效")
    member = db.get(Member, stored.member_id)
    if member is None or member.status != "active":
        raise HTTPException(status_code=401, detail="请先登录")
    stored.revoked_at = utcnow()
    db.commit()
    return _issue(db, member, response)


@router.post("/auth/logout")
def logout(body: RefreshBody, response: Response, db: Session = Depends(get_db)) -> dict:
    stored = (
        db.query(SessionToken)
        .filter(SessionToken.token_hash == sha256_text(body.refresh_token or ""))
        .one_or_none()
    )
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = utcnow()
        db.commit()
    _clear_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
def me(user: Member = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    body = member_brief(db, user) or {}
    body["pending_keywords"] = pending_count(db, user)
    setting = db.get(AppSetting, 1)
    body["queue_paused"] = bool(setting.queue_paused) if setting else False
    return body


@router.get("/dashboard")
def dashboard(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    target = resolve_tenant(user, tenant_id) if user.role != "member" else user.tenant_id
    if user.role == "platform_admin" and tenant_id is None:
        tenant_ids = [row.id for row in db.query(Tenant.id).all()]
        member_ids = None
    elif user.role == "member":
        tenant_ids = [user.tenant_id] if user.tenant_id else []
        member_ids = [user.id]
    else:
        tenant_ids = [target] if target else []
        member_ids = visible_member_ids(db, user, target)
    report = reconcile(db, tenant_ids)
    if user.role == "member":
        own = db.get(Member, user.id)
        report = {
            "ok": True,
            "balance": member_dict(db, own)["balance"] if own else 0,
            "month_cost": member_dict(db, own)["month_cost"] if own else 0,
            "cumulative_cost": member_dict(db, own)["lifetime_cost"] if own else 0,
            "allocated": 0,
            "pool_remaining": 0,
            "previous_month_cost": 0,
            "overdraft": bool(own and (own.balance_cents < 0 or own.overdraft)),
            "overdraft_members": [own.name] if own and (own.balance_cents < 0 or own.overdraft) else [],
            "tenants": [],
        }
    pending_query = db.query(KeywordSubmission).filter(KeywordSubmission.status == "pending")
    if user.role == "tenant_admin":
        pending_query = pending_query.filter(KeywordSubmission.tenant_id == user.tenant_id)
    elif user.role == "member":
        pending_query = pending_query.filter(KeywordSubmission.member_id == user.id)
    pending_rows = pending_query.order_by(KeywordSubmission.created_at.desc()).limit(8).all()
    custom_query = db.query(KeywordSubmission).filter(
        KeywordSubmission.status == "pending",
        KeywordSubmission.scope == "tenant",
    )
    if user.role == "tenant_admin":
        custom_query = custom_query.filter(KeywordSubmission.tenant_id == user.tenant_id)
    elif user.role == "member":
        custom_query = custom_query.filter(KeywordSubmission.member_id == user.id)
    custom_pending = custom_query.count()
    audits = db.query(AuditLog)
    if user.role == "tenant_admin":
        audits = audits.filter(AuditLog.tenant_id == user.tenant_id)
    elif user.role == "member":
        audits = audits.filter(AuditLog.actor_id == user.id)
    recent = audits.order_by(AuditLog.created_at.desc()).limit(6).all()
    return {
        "reconcile": report,
        "pending_keywords": pending_count(db, user),
        "custom_pending": custom_pending,
        "trend": trend(db, member_ids),
        "recent": [audit_dict(db, row) for row in recent],
        "pending": [submission_dict(db, row) for row in pending_rows],
    }


@router.get("/tenants")
def list_tenants(user: Member = Depends(require("tenant.manage")), db: Session = Depends(get_db)) -> list:
    rows = db.query(Tenant).order_by(Tenant.id).all()
    return [tenant_dict(db, row) for row in rows]


@router.post("/tenants")
def post_tenant(
    body: TenantBody,
    request: Request,
    user: Member = Depends(require("tenant.manage")),
    db: Session = Depends(get_db),
) -> dict:
    tenant, member = create_tenant(
        db,
        user,
        name=body.name,
        admin_email=body.admin_email,
        admin_name=body.admin_name,
        admin_password=body.admin_password,
        pool=body.pool,
        seats=body.seats,
        ip=client_ip(request),
    )
    return {"tenant": tenant_dict(db, tenant), "admin": member_dict(db, member)}


@router.put("/tenants/{tenant_id}/status")
def put_tenant_status(
    tenant_id: int,
    body: StatusBody,
    request: Request,
    user: Member = Depends(require("tenant.manage")),
    db: Session = Depends(get_db),
) -> dict:
    tenant = set_tenant_status(
        db,
        user,
        tenant_id,
        body.status,
        disable_members=body.disable_members,
        ip=client_ip(request),
    )
    return tenant_dict(db, tenant)


@router.get("/members")
def list_members(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> list:
    if user.role == "member":
        return [member_dict(db, user)]
    target = resolve_tenant(user, tenant_id)
    query = db.query(Member).filter(Member.deleted_at.is_(None), Member.role != "platform_admin")
    if target is not None:
        query = query.filter(Member.tenant_id == target)
    return [member_dict(db, row) for row in query.order_by(Member.id).all()]


@router.post("/members/invite")
def post_invite(
    body: InviteBody,
    request: Request,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> dict:
    row, token = invite_member(
        db,
        user,
        email=body.email,
        name=body.name,
        role=body.role,
        tenant_id=body.tenant_id,
        initial_amount=body.initial_amount,
        ip=client_ip(request),
    )
    tenant = db.get(Tenant, row.tenant_id)
    payload = invite_dict(row, tenant.name if tenant else "")
    payload["invite_token"] = token
    return payload


@router.post("/members")
def post_member(
    body: CreateMemberBody,
    request: Request,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> dict:
    member = create_member(
        db,
        user,
        email=body.email,
        name=body.name,
        password=body.password,
        role=body.role,
        tenant_id=body.tenant_id,
        ip=client_ip(request),
    )
    return member_dict(db, member)


@router.get("/invites")
def list_invites(
    tenant_id: int | None = None,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> list:
    target = resolve_tenant(user, tenant_id)
    query = db.query(Invite).filter(Invite.used_at.is_(None))
    if target is not None:
        query = query.filter(Invite.tenant_id == target)
    rows = query.order_by(Invite.id.desc()).all()
    names = {row.id: row.name for row in db.query(Tenant).all()}
    return [invite_dict(row, names.get(row.tenant_id, "")) for row in rows]


@router.post("/invites/{token}/accept")
def post_accept(token: str, body: AcceptBody, db: Session = Depends(get_db)) -> dict:
    member, warning = accept_invite(db, token, body.password, body.name)
    return {"member": member_dict(db, member), "warning": warning}


@router.post("/members/{member_id}/disable")
def post_disable(
    member_id: int,
    request: Request,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> dict:
    member = disable_member(db, user, member_id, ip=client_ip(request))
    return member_dict(db, member)


@router.post("/members/{member_id}/enable")
def post_enable(
    member_id: int,
    request: Request,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> dict:
    member = enable_member(db, user, member_id, ip=client_ip(request))
    return member_dict(db, member)


@router.get("/members/{member_id}/devices")
def list_devices(
    member_id: int,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> list:
    member = db.get(Member, member_id)
    if member is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="成员不存在")
    resolve_tenant(user, member.tenant_id)
    rows = db.query(Device).filter(Device.member_id == member_id).order_by(Device.id).all()
    return [device_dict(row) for row in rows]


@router.post("/devices/{device_id}/revoke")
def post_revoke(
    device_id: int,
    request: Request,
    user: Member = Depends(require("member.manage")),
    db: Session = Depends(get_db),
) -> dict:
    device = revoke_device(db, user, device_id, ip=client_ip(request))
    return device_dict(device)


@router.post("/devices/bind")
def post_bind(body: BindBody, db: Session = Depends(get_db)) -> dict:
    raw, member, device = bind_device(
        db,
        email=body.email,
        relay_user_id=body.relay_user_id,
        device_name=body.device_name,
        client_version=body.client_version,
    )
    return {"member_token": raw, "member_id": member.id, "device": device_dict(device)}


@router.get("/quotas/summary")
def quota_summary(
    tenant_id: int | None = None,
    user: Member = Depends(require("quota.allocate")),
    db: Session = Depends(get_db),
) -> dict:
    target = resolve_tenant(user, tenant_id)
    ids = [target] if target is not None else [row.id for row in db.query(Tenant.id).filter(Tenant.status == "active")]
    if target is None and user.role != "platform_admin":
        ids = [user.tenant_id] if user.tenant_id else []
    report = reconcile(db, ids)
    report["tenant_id"] = target
    return report


@router.get("/quotas/records")
def quota_records(
    tenant_id: int | None = None,
    user: Member = Depends(require("quota.allocate")),
    db: Session = Depends(get_db),
) -> list:
    target = resolve_tenant(user, tenant_id)
    query = db.query(QuotaAllocation)
    if target is not None:
        query = query.filter(QuotaAllocation.tenant_id == target)
    elif user.role != "platform_admin":
        query = query.filter(QuotaAllocation.tenant_id == user.tenant_id)
    rows = query.order_by(QuotaAllocation.id.desc()).limit(100).all()
    return [quota_dict(db, row) for row in rows]


@router.post("/quotas/allocate")
def post_quota(
    body: QuotaBody,
    request: Request,
    user: Member = Depends(require("quota.allocate")),
    db: Session = Depends(get_db),
) -> dict:
    row = allocate(
        db,
        user,
        member_id=body.member_id,
        amount=body.amount,
        idempotency_key=body.idempotency_key,
        remark=body.remark,
        period=body.period,
        direction=body.direction,
        ip=client_ip(request),
    )
    return quota_dict(db, row)


@router.get("/rules")
def get_rules(
    response: Response,
    since: int = 0,
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    payload = rules_payload(db, user, since, tenant_id)
    response.headers["ETag"] = f'"{payload["version"]}"'
    return payload


@router.get("/rules/settings")
def get_rule_settings(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    target = user.tenant_id if user.role != "platform_admin" else tenant_id
    if user.role != "platform_admin":
        resolve_tenant(user, tenant_id)
    if target is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="请选择公司")
    return rules_dict(db.get(TenantRules, target))


@router.put("/rules/settings")
def put_rule_settings(
    body: RulesBody,
    request: Request,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = save_rule_settings(
        db,
        user,
        tenant_id=body.tenant_id,
        categories=body.categories,
        ignore_phrases=body.ignore_phrases,
        note=body.note,
        ip=client_ip(request),
    )
    return rules_dict(row)


@router.get("/rules/submissions")
def list_submissions(
    status: str = "",
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> list:
    query = db.query(KeywordSubmission)
    if status:
        query = query.filter(KeywordSubmission.status == status)
    if user.role == "member":
        query = query.filter(KeywordSubmission.member_id == user.id)
    elif user.role == "tenant_admin":
        query = query.filter(KeywordSubmission.tenant_id == user.tenant_id, KeywordSubmission.scope == "tenant")
    rows = query.order_by(KeywordSubmission.id.desc()).limit(300).all()
    return [submission_dict(db, row) for row in rows]


@router.post("/rules/submissions")
def post_submission(
    body: KeywordBody,
    request: Request,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = submit_keyword(
        db,
        user,
        text=body.text,
        category=body.category,
        source=body.source,
        scope=body.scope,
        tenant_id=body.tenant_id,
        ip=client_ip(request),
    )
    return submission_dict(db, row)


@router.post("/rules/submissions/{submission_id}/approve")
def post_approve(
    submission_id: int,
    body: ReviewBody,
    request: Request,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = approve_keyword(
        db,
        user,
        submission_id,
        remark=body.remark,
        force=body.force,
        ip=client_ip(request),
    )
    return submission_dict(db, row)


@router.post("/rules/submissions/{submission_id}/reject")
def post_reject(
    submission_id: int,
    body: ReviewBody,
    request: Request,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = reject_keyword(db, user, submission_id, body.remark, ip=client_ip(request))
    return submission_dict(db, row)


@router.delete("/rules/entries/{entry_id}")
def remove_entry(
    entry_id: int,
    request: Request,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    row = delete_keyword(db, user, entry_id, ip=client_ip(request))
    return entry_dict(db, row)


@router.get("/usage/summary")
def usage_summary(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    members = usage_rows(db, user, tenant_id)
    requests = sum(row.month_requests for row in members)
    tokens_in = sum(row.month_input_tokens for row in members)
    tokens_out = sum(row.month_output_tokens for row in members)
    cost = sum(row.month_cost_cents for row in members)
    active = [row for row in members if row.month_cost_cents > 0]
    return {
        "requests": requests,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "tokens": tokens_in + tokens_out,
        "cost": round(cost / 100, 2),
        "active_members": len(active),
        "average": round((cost / 100) / len(active), 2) if active else 0,
    }


@router.get("/usage/by-member")
def usage_by_member(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> list:
    members = usage_rows(db, user, tenant_id)
    total = sum(row.month_cost_cents for row in members) or 1
    rows = []
    for member in members:
        shown = member_dict(db, member)
        rows.append(
            {
                "id": member.id,
                "name": member.name,
                "relay_user_id": member.relay_user_id,
                "requests": member.month_requests,
                "tokens": member.month_input_tokens + member.month_output_tokens,
                "cost": shown["month_cost"],
                "share": round(member.month_cost_cents / total, 4),
            }
        )
    return rows


@router.get("/usage/reconcile")
def usage_reconcile(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    if user.role == "member":
        return dashboard(tenant_id, user, db)["reconcile"]
    target = resolve_tenant(user, tenant_id)
    if user.role == "platform_admin" and target is None:
        ids = [row.id for row in db.query(Tenant.id).all()]
    else:
        ids = [target] if target else []
    return reconcile(db, ids)


@router.get("/usage/export")
def usage_export(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    members = usage_rows(db, user, tenant_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["成员", "中转站用户", "请求数", "输入 Token", "输出 Token", "消耗（元）"])
    for member in members:
        writer.writerow(
            [
                member.name,
                member.relay_user_id,
                member.month_requests,
                member.month_input_tokens,
                member.month_output_tokens,
                f"{member.month_cost_cents / 100:.2f}",
            ]
        )
    data = buffer.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        iter([data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage.csv"},
    )


@router.get("/audit-logs")
def audit_logs(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> list:
    query = db.query(AuditLog)
    if user.role == "member":
        query = query.filter(AuditLog.actor_id == user.id)
    elif user.role == "tenant_admin":
        resolve_tenant(user, tenant_id)
        query = query.filter(AuditLog.tenant_id == user.tenant_id)
    elif tenant_id is not None:
        query = query.filter(AuditLog.tenant_id == tenant_id)
    rows = query.order_by(AuditLog.id.desc()).limit(200).all()
    return [audit_dict(db, row) for row in rows]


def _settings_payload(db: Session) -> dict:
    row = db.get(AppSetting, 1)
    env = get_settings()
    backend_on = bool(row.backend_mode_enabled) if row else False
    registration_on = bool(row.registration_enabled) if row else False
    email_ok = bool(row.email_verify_ok) if row else True
    return {
        "relay_base_url": row.relay_base_url if row else env.relay_base_url,
        "relay_gateway_url": row.relay_gateway_url if row else env.relay_gateway_url,
        "group_name": row.group_name if row else env.relay_group_name,
        "group_id": row.group_id if row else env.relay_group_id,
        "admin_key_configured": bool(env.relay_admin_key),
        "backend_mode_enabled": backend_on,
        "registration_enabled": registration_on,
        "email_verify_ok": email_ok,
        "queue_paused": bool(row.queue_paused) if row else False,
        "timezone": env.timezone,
        "checks": [
            {
                "key": "backend_mode_enabled",
                "label": "后台模式 backend_mode_enabled",
                "ok": not backend_on,
                "state": "必须关闭" if not backend_on else "当前为开启",
                "detail": "开启时非 admin 登录会直接 403，客户端无法使用。",
            },
            {
                "key": "registration",
                "label": "自助注册 registration",
                "ok": not registration_on,
                "state": "必须关闭" if not registration_on else "当前为开启",
                "detail": "否则员工可自行注册，绕过公司归属。",
            },
            {
                "key": "email_verify",
                "label": "邮箱验证 email_verify",
                "ok": email_ok,
                "state": "管理员建号不受影响" if email_ok else "需要再确认",
                "detail": "建议成员使用真实企业邮箱。",
            },
        ],
    }


@router.get("/settings")
def get_settings_route(user: Member = Depends(require("settings.write")), db: Session = Depends(get_db)) -> dict:
    del user
    return _settings_payload(db)


@router.put("/settings")
def put_settings(
    body: SettingsBody,
    request: Request,
    user: Member = Depends(require("settings.write")),
    db: Session = Depends(get_db),
) -> dict:
    from app.service import audit

    row = db.get(AppSetting, 1)
    if row is None:
        row = AppSetting(id=1)
        db.add(row)
    row.relay_base_url = body.relay_base_url.strip().rstrip("/")
    row.relay_gateway_url = body.relay_gateway_url.strip().rstrip("/")
    row.group_name = body.group_name.strip() or "电商审核专属-bot创建"
    row.group_id = body.group_id.strip()
    row.backend_mode_enabled = body.backend_mode_enabled
    row.registration_enabled = body.registration_enabled
    row.email_verify_ok = body.email_verify_ok
    row.updated_at = utcnow()
    audit(db, user, "settings.update", "relay", "更新接入设置", ip=client_ip(request))
    db.commit()
    return _settings_payload(db)


@router.post("/settings/test")
def test_settings(user: Member = Depends(require("settings.write")), db: Session = Depends(get_db)) -> dict:
    del user
    env = get_settings()
    if not env.relay_admin_key:
        return {"ok": False, "mode": "local", "message": "未配置管理密钥，当前是本地演示，不会请求中转站。"}
    import httpx

    row = db.get(AppSetting, 1)
    base = (row.relay_base_url if row else env.relay_base_url).rstrip("/")
    try:
        response = httpx.get(
            base + "/admin/users/9223372036854775807/balance-history",
            headers={"x-api-key": env.relay_admin_key, "User-Agent": "evc-admin/0.1.0"},
            timeout=8,
        )
    except httpx.HTTPError:
        return {"ok": False, "mode": "remote", "message": "连不上中转站"}
    if response.status_code in (401, 403):
        return {"ok": False, "mode": "remote", "message": "管理密钥无效"}
    if response.status_code == 404:
        return {"ok": True, "mode": "remote", "message": "管理密钥有效，中转站连接正常"}
    if response.status_code >= 400:
        return {"ok": False, "mode": "remote", "message": f"中转站返回 HTTP {response.status_code}"}
    return {"ok": True, "mode": "remote", "message": "已连上中转站"}


def _job_or_404(db: Session, user: Member, job_id: int) -> ReviewJob:
    from fastapi import HTTPException

    job = db.get(ReviewJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    if user.role == "member" and job.member_id != user.id:
        raise HTTPException(status_code=404, detail="视频不存在")
    if user.role == "tenant_admin" and job.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="视频不存在")
    return job


@router.get("/jobs")
def list_jobs(
    tenant_id: int | None = None,
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> list:
    query = db.query(ReviewJob)
    if user.role == "member":
        query = query.filter(ReviewJob.member_id == user.id)
    elif user.role == "tenant_admin":
        query = query.filter(ReviewJob.tenant_id == user.tenant_id)
    elif tenant_id is not None:
        query = query.filter(ReviewJob.tenant_id == tenant_id)
    rows = query.order_by(ReviewJob.id.desc()).limit(50).all()
    return [job_dict(db, row) for row in rows]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: Member = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return job_dict(db, _job_or_404(db, user, job_id), with_findings=True)


@router.post("/jobs")
def post_job(
    request: Request,
    file: UploadFile = File(...),
    member_id: int | None = Form(None),
    user: Member = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    job = register_job(
        db,
        user,
        filename=file.filename or "",
        member_id=member_id,
        chunks=file.file,
        ip=client_ip(request),
    )
    return job_dict(db, job)


@router.get("/jobs/{job_id}/media")
def job_media(job_id: int, user: Member = Depends(current_user), db: Session = Depends(get_db)) -> FileResponse:
    from fastapi import HTTPException

    job = _job_or_404(db, user, job_id)
    if not job.file_path or not Path(job.file_path).is_file():
        raise HTTPException(status_code=404, detail="没有视频文件")
    suffix = Path(job.filename).suffix.lower()
    media = "video/webm" if suffix == ".webm" else "video/ogg" if suffix == ".ogg" else "video/mp4"
    return FileResponse(job.file_path, media_type=media, filename=job.filename)


@router.get("/jobs/{job_id}/export")
def export_job(job_id: int, user: Member = Depends(current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    job = _job_or_404(db, user, job_id)
    detail = job_dict(db, job, with_findings=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "结果"
    sheet.append(list(_COLUMNS))
    for row in detail["findings"]:
        sheet.append([row[column] for column in _COLUMNS])
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    filename = f"{Path(job.filename).stem}_合规结果.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/queue")
def get_queue(user: Member = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    del user
    row = db.get(AppSetting, 1)
    return {"paused": bool(row.queue_paused) if row else False}


@router.post("/queue/pause")
def pause_queue(request: Request, user: Member = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"paused": set_queue_paused(db, user, True, ip=client_ip(request))}


@router.post("/queue/resume")
def resume_queue(request: Request, user: Member = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"paused": set_queue_paused(db, user, False, ip=client_ip(request))}


@router.get("/search")
def search(q: str = "", user: Member = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    text = q.strip()
    if not text:
        return {"members": [], "keywords": []}
    member_query = db.query(Member).filter((Member.name.contains(text)) | (Member.email.contains(text)))
    keyword_query = db.query(KeywordSubmission).filter(KeywordSubmission.text.contains(text))
    if user.role == "member":
        member_query = member_query.filter(Member.id == user.id)
        keyword_query = keyword_query.filter(KeywordSubmission.member_id == user.id)
    elif user.role == "tenant_admin":
        member_query = member_query.filter(Member.tenant_id == user.tenant_id)
        keyword_query = keyword_query.filter(KeywordSubmission.tenant_id == user.tenant_id)
    return {
        "members": [member_dict(db, row) for row in member_query.limit(8).all()],
        "keywords": [submission_dict(db, row) for row in keyword_query.limit(8).all()],
    }
