"""租户、成员、额度和用量。租户范围以登录账号为准，不读请求体里的租户来放权。"""

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin_app.errors import DomainError
from admin_app.models import (
    AppSetting,
    AuditLog,
    Device,
    Invite,
    Member,
    MemberToken,
    QuotaAllocation,
    SessionToken,
    Tenant,
    UsageSnapshot,
)
from admin_app.money import to_cents, yuan
from admin_app.permissions import ROLE_LABEL, has_perm
from admin_app.relay import LOOKUP_FAILED, RATE_LIMITED, apply_credentials, get_relay, relay_mode
from admin_app.security import hash_password, new_invite_token, sha256_text
from admin_app.timeutil import business_today, business_timezone_name, iso, shanghai_now, utcnow

# 一次查询最长跨度。含首尾，正好覆盖一个闰年。
MAX_CONSUMPTION_DAYS = 366
# 撞上限流后再等这么久，重试当前这一个成员。测试会把它改成 0。
SYNC_RATE_LIMIT_WAIT = 5.0

logger = logging.getLogger("admin_app")


def audit(
    db: Session,
    actor: Member | None,
    action: str,
    obj: str,
    summary: str,
    *,
    tenant_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str = "",
) -> None:
    db.add(
        AuditLog(
            tenant_id=tenant_id if tenant_id is not None else (actor.tenant_id if actor else None),
            actor_id=actor.id if actor else None,
            role=actor.role if actor else "",
            action=action,
            object=obj,
            summary=summary,
            before=before,
            after=after,
            ip=ip or "",
        )
    )


def _require(actor: Member, perm: str) -> None:
    if not has_perm(actor.role, perm):
        raise DomainError(403, "没有权限")


def resolve_tenant(actor: Member, requested: int | None) -> int | None:
    """平台管理员可以指定公司。其他角色只能看自己的公司，传别人的编号直接拒绝。"""
    if actor.role == "platform_admin":
        return requested
    if actor.tenant_id is None:
        raise DomainError(403, "没有公司范围")
    if requested is not None and requested != actor.tenant_id:
        raise DomainError(403, "不能查看或修改其他公司的数据")
    return actor.tenant_id


def _tenant(db: Session, tenant_id: int) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise DomainError(404, "公司不存在")
    return tenant


def seats_used(db: Session, tenant_id: int) -> int:
    return (
        db.query(Member)
        .filter(
            Member.tenant_id == tenant_id,
            Member.status == "active",
            Member.deleted_at.is_(None),
            Member.role != "platform_admin",
        )
        .count()
    )


def _period() -> str:
    return shanghai_now().strftime("%Y-%m")


def _sync_member_balance(relay, member: Member) -> None:
    """把成员本地余额对齐中转站。读不到时跳过，不阻断建号。"""
    if not member.relay_user_id:
        return
    try:
        snapshot = relay.user_snapshot(member.relay_user_id)
    except DomainError:
        return
    if snapshot is None:
        return
    balance = int(snapshot["balance_cents"])
    member.relay_balance_cents = balance
    member.relay_status = str(snapshot.get("status") or "")[:16]
    member.relay_checked_at = utcnow()
    member.balance_cents = balance
    member.overdraft = balance < 0


def _wallet_owner(db: Session, tenant: Tenant) -> Member:
    """公司的钱放在公司管理员自己的中转站账号里，分配和回收都在这个账号上进出。"""
    owner = (
        db.query(Member)
        .filter(
            Member.tenant_id == tenant.id,
            Member.role == "tenant_admin",
            Member.deleted_at.is_(None),
        )
        .order_by(Member.id)
        .first()
    )
    if owner is None:
        raise DomainError(400, "这家公司还没有公司管理员，不能分配额度")
    return owner


def _password_ok(password: str) -> None:
    if len(password or "") < 6 or len(password) > 72:
        raise DomainError(400, "密码需要在 6 到 72 位之间")


# 中转站已经建号或绑号，但本地提交失败。重试会按邮箱再查，不会再开一个号。
SAVE_FAILED = "中转站账号已处理，但后台保存失败。请用同一邮箱再提交一次，不会重复建号。"


def _lookup_relay_user(relay, email: str) -> dict | None:
    """查中转站。任何失败都抛错，调用方不能把失败当成新用户。"""
    try:
        found = relay.find_user_by_email(email)
    except DomainError as exc:
        if exc.detail == LOOKUP_FAILED:
            raise
        raise DomainError(502, LOOKUP_FAILED) from exc
    except Exception as exc:
        raise DomainError(502, LOOKUP_FAILED) from exc
    if found is None:
        return None
    if not isinstance(found, dict) or not str(found.get("id") or "").strip():
        raise DomainError(502, LOOKUP_FAILED)
    return found


def _bind_existing(db: Session, found: dict) -> tuple[str, dict]:
    status = str(found.get("status") or "").strip().lower()
    if status and status != "active":
        raise DomainError(400, "这个账号已停用，请联系平台管理员启用")
    relay_user_id = str(found["id"])
    taken = (
        db.query(Member)
        .filter(Member.relay_user_id == relay_user_id, Member.deleted_at.is_(None))
        .first()
    )
    if taken is not None:
        raise DomainError(400, "这个账号已经绑定了其他成员")
    # 已有账号的密码在中转站那边，后台不知道，也不该重置；密钥等成员自己登录后写入。
    return relay_user_id, {}


def _relay_account(db: Session, relay, email: str, password: str) -> tuple[str, dict, bool]:
    """先按邮箱查中转站。已有就绑定，没有才用现有建号接口开通。

    返回 (relay_user_id, credentials, linked_existing)。
    查询失败直接报错。建号请求发出后如果响应丢失，会再查一次：查到就绑定，避免重复建号。
    """
    found = _lookup_relay_user(relay, email)
    if found is not None:
        relay_user_id, credentials = _bind_existing(db, found)
        return relay_user_id, credentials, True
    _password_ok(password)
    try:
        created = relay.create_user(email, password)
    except Exception as exc:
        again = _existing_after_create_error(relay, email)
        if again is not None:
            relay_user_id, credentials = _bind_existing(db, again)
            return relay_user_id, credentials, True
        if isinstance(exc, DomainError):
            raise
        raise DomainError(502, "建号失败，请稍后再试。本次没有写入后台。") from None
    if not isinstance(created, tuple) or len(created) != 2:
        raise DomainError(502, "开通账号失败，服务没有返回用户编号")
    relay_user_id, credentials = created
    if not str(relay_user_id or "").strip():
        raise DomainError(502, "开通账号失败，服务没有返回用户编号")
    return str(relay_user_id), credentials if isinstance(credentials, dict) else {}, False


def _existing_after_create_error(relay, email: str) -> dict | None:
    """建号报错后再查一次。查询也失败时返回 None，调用方必须沿用原来的建号错误，不能当成新建成功。"""
    try:
        found = relay.find_user_by_email(email)
    except Exception:
        return None
    if isinstance(found, dict) and str(found.get("id") or "").strip():
        return found
    return None


def _commit_bound_member(db: Session) -> None:
    """中转站已经处理完才提交本地。写库失败就回滚，留下的中转站账号按邮箱再提交即可绑上。"""
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise DomainError(500, SAVE_FAILED) from None


def create_tenant(
    db: Session,
    actor: Member,
    *,
    name: str,
    admin_email: str,
    admin_name: str,
    admin_password: str,
    seats: int,
    ip: str = "",
) -> tuple[Tenant, Member, bool]:
    _require(actor, "tenant.manage")
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 40:
        raise DomainError(400, "公司名称需要在 1 到 40 个字之间")
    email = admin_email.strip().lower()
    if "@" not in email or len(email) > 120:
        raise DomainError(400, "管理员邮箱不正确")
    if db.query(Tenant).filter(Tenant.name == cleaned).first():
        raise DomainError(400, "已经有这家公司")
    if db.query(Member).filter(Member.email == email).first():
        raise DomainError(400, "这个邮箱已经开通")
    if seats < 1 or seats > 10000:
        raise DomainError(400, "席位数需要在 1 到 10000 之间")
    from admin_app.relay import group_id_of, group_name_of

    # 公司不单独充值。总额等于所有成员余额之和，初始余额以管理员在中转站的余额为准。
    tenant = Tenant(
        name=cleaned,
        status="active",
        seat_quota=seats,
        relay_group_id=group_id_of(db),
        relay_group_name=group_name_of(db),
        pool_balance_cents=0,
        pool_total_cents=0,
        allocated_total_cents=0,
    )
    db.add(tenant)
    db.flush()
    relay = get_relay(db)
    relay_user_id, credentials, linked = _relay_account(db, relay, email, admin_password)
    admin_member = Member(
        tenant_id=tenant.id,
        email=email,
        name=(admin_name or email.split("@", 1)[0])[:40],
        role="tenant_admin",
        password_hash=hash_password(admin_password) if admin_password else "",
        relay_user_id=relay_user_id,
        status="active",
    )
    apply_credentials(admin_member, credentials)
    _sync_member_balance(relay, admin_member)
    db.add(admin_member)
    audit(db, actor, "tenant.create", cleaned, f"新建公司 {cleaned}", tenant_id=tenant.id, ip=ip)
    audit(
        db,
        actor,
        "member.create",
        email,
        f"创建公司管理员 {admin_member.name}",
        tenant_id=tenant.id,
        after={"relay_user_id": relay_user_id, "role": "tenant_admin", "linked_existing": linked},
        ip=ip,
    )
    _commit_bound_member(db)
    db.refresh(tenant)
    return tenant, admin_member, linked


def set_tenant_status(
    db: Session,
    actor: Member,
    tenant_id: int,
    status: str,
    *,
    disable_members: bool = False,
    ip: str = "",
) -> Tenant:
    _require(actor, "tenant.manage")
    if status not in ("active", "disabled"):
        raise DomainError(400, "状态不正确")
    tenant = _tenant(db, tenant_id)
    before = tenant.status
    tenant.status = status
    audit(
        db,
        actor,
        "tenant.status",
        tenant.name,
        f"{tenant.name} {before} → {status}",
        tenant_id=tenant.id,
        before={"status": before},
        after={"status": status},
        ip=ip,
    )
    db.commit()
    if status == "disabled" and disable_members:
        members = (
            db.query(Member)
            .filter(Member.tenant_id == tenant.id, Member.status == "active", Member.deleted_at.is_(None))
            .all()
        )
        for member in members:
            disable_member(db, actor, member.id, ip=ip)
        tenant = _tenant(db, tenant_id)
    return tenant


def _pending_invites(db: Session, tenant_id: int) -> int:
    now = utcnow()
    return (
        db.query(Invite)
        .filter(Invite.tenant_id == tenant_id, Invite.used_at.is_(None), Invite.expires_at > now)
        .count()
    )


def invite_member(
    db: Session,
    actor: Member,
    *,
    email: str,
    name: str,
    role: str,
    tenant_id: int | None,
    initial_amount: object = 0,
    ip: str = "",
) -> tuple[Invite, str]:
    _require(actor, "member.manage")
    if role not in ("tenant_admin", "member"):
        raise DomainError(400, "角色只能是公司管理员或普通用户")
    target = resolve_tenant(actor, tenant_id)
    if target is None:
        raise DomainError(400, "请选择公司")
    tenant = _tenant(db, target)
    if tenant.status != "active":
        raise DomainError(400, "公司已停用")
    address = email.strip().lower()
    if "@" not in address:
        raise DomainError(400, "邮箱不正确")
    if db.query(Member).filter(Member.email == address).first():
        raise DomainError(400, "这个邮箱已经开通")
    if seats_used(db, tenant.id) + _pending_invites(db, tenant.id) >= tenant.seat_quota:
        raise DomainError(400, "席位已满，不能再邀请")
    cents = to_cents(initial_amount)
    if cents < 0:
        raise DomainError(400, "初始额度不能是负数")
    token = new_invite_token()
    row = Invite(
        tenant_id=tenant.id,
        token_hash=sha256_text(token),
        email=address,
        name=(name or address.split("@", 1)[0]).strip()[:40] or address,
        role=role,
        initial_amount_cents=cents,
        expires_at=utcnow() + timedelta(days=7),
    )
    db.add(row)
    audit(db, actor, "member.invite", address, f"邀请 {address}", tenant_id=tenant.id, ip=ip)
    db.commit()
    db.refresh(row)
    return row, token


def create_member(
    db: Session,
    actor: Member,
    *,
    email: str,
    name: str,
    password: str,
    role: str,
    tenant_id: int | None,
    ip: str = "",
) -> tuple[Member, bool]:
    _require(actor, "member.manage")
    if role not in ("tenant_admin", "member"):
        raise DomainError(400, "角色只能是公司管理员或普通用户")
    if actor.role == "tenant_admin" and role != "member":
        raise DomainError(403, "公司管理员只能创建普通员工")
    target = resolve_tenant(actor, tenant_id)
    if target is None:
        raise DomainError(400, "请选择公司")
    tenant = _tenant(db, target)
    if tenant.status != "active":
        raise DomainError(400, "公司已停用")
    address = email.strip().lower()
    if "@" not in address:
        raise DomainError(400, "邮箱不正确")
    if db.query(Member).filter(Member.email == address).first():
        raise DomainError(400, "这个邮箱已经开通")
    if seats_used(db, tenant.id) >= tenant.seat_quota:
        raise DomainError(400, "席位已满")
    relay = get_relay(db)
    relay_user_id, credentials, linked = _relay_account(db, relay, address, password)
    member = Member(
        tenant_id=tenant.id,
        email=address,
        name=(name or address.split("@", 1)[0]).strip()[:40],
        role=role,
        password_hash=hash_password(password) if password else "",
        relay_user_id=relay_user_id,
        status="active",
    )
    apply_credentials(member, credentials)
    db.add(member)
    audit(
        db,
        actor,
        "member.create",
        member.email,
        f"创建成员 {member.name}",
        tenant_id=tenant.id,
        after={"relay_user_id": relay_user_id, "role": role, "linked_existing": linked},
        ip=ip,
    )
    _commit_bound_member(db)
    db.refresh(member)
    return member, linked


def accept_invite(db: Session, token: str, password: str, name: str = "") -> tuple[Member, str, bool]:
    _password_ok(password)
    row = db.query(Invite).filter(Invite.token_hash == sha256_text(token or "")).one_or_none()
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        raise DomainError(400, "邀请链接无效或已过期")
    tenant = _tenant(db, row.tenant_id)
    if tenant.status != "active":
        raise DomainError(400, "公司已停用")
    if seats_used(db, tenant.id) >= tenant.seat_quota:
        raise DomainError(400, "席位已满")
    if db.query(Member).filter(Member.email == row.email).first():
        raise DomainError(400, "这个邮箱已经开通")
    relay = get_relay(db)
    relay_user_id, credentials, linked = _relay_account(db, relay, row.email, password)
    member = Member(
        tenant_id=tenant.id,
        email=row.email,
        name=(name or row.name or row.email.split("@", 1)[0])[:40],
        role=row.role,
        password_hash=hash_password(password),
        relay_user_id=relay_user_id,
        status="active",
    )
    apply_credentials(member, credentials)
    row.used_at = utcnow()
    db.add(member)
    audit(
        db,
        member,
        "member.create",
        member.email,
        f"开通成员 {member.name}",
        tenant_id=tenant.id,
        after={"relay_user_id": relay_user_id, "role": member.role, "linked_existing": linked},
    )
    _commit_bound_member(db)
    db.refresh(member)
    warning = ""
    if row.initial_amount_cents > 0:
        try:
            allocate(
                db,
                member if member.role == "tenant_admin" else _first_admin(db, tenant.id) or member,
                member_id=member.id,
                amount=yuan(row.initial_amount_cents),
                idempotency_key=f"invite-{row.id}",
                remark="开通时的初始额度",
                period=_period(),
                direction="add",
            )
        except DomainError as exc:
            warning = exc.detail
    return member, warning, linked


def _first_admin(db: Session, tenant_id: int) -> Member | None:
    return (
        db.query(Member)
        .filter(Member.tenant_id == tenant_id, Member.role == "tenant_admin", Member.status == "active")
        .order_by(Member.id)
        .first()
    )


def _managed_member(db: Session, actor: Member, member_id: int) -> Member:
    _require(actor, "member.manage")
    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None:
        raise DomainError(404, "成员不存在")
    if member.role == "platform_admin":
        raise DomainError(403, "不能在这里改平台管理员")
    resolve_tenant(actor, member.tenant_id)
    return member


def disable_member(db: Session, actor: Member, member_id: int, ip: str = "") -> Member:
    member = _managed_member(db, actor, member_id)
    if member.id == actor.id:
        raise DomainError(400, "不能禁用自己")
    if member.status == "disabled":
        return member
    before_seats = seats_used(db, member.tenant_id or 0)
    relay = get_relay(db)
    if member.relay_user_id:
        relay.set_status(member.relay_user_id, "disabled")
    now = utcnow()
    member.status = "disabled"
    db.query(Device).filter(Device.member_id == member.id, Device.revoked_at.is_(None)).update(
        {Device.revoked_at: now}
    )
    db.query(MemberToken).filter(MemberToken.member_id == member.id, MemberToken.revoked_at.is_(None)).update(
        {MemberToken.revoked_at: now}
    )
    db.query(SessionToken).filter(SessionToken.member_id == member.id, SessionToken.revoked_at.is_(None)).update(
        {SessionToken.revoked_at: now}
    )
    after_seats = before_seats - 1
    audit(
        db,
        actor,
        "member.disable",
        member.name,
        f"停用 {member.name}，席位 {before_seats} → {after_seats}",
        tenant_id=member.tenant_id,
        before={"seats": before_seats},
        after={"seats": after_seats, "relay_user_id": member.relay_user_id},
        ip=ip,
    )
    db.commit()
    db.refresh(member)
    return member


def enable_member(db: Session, actor: Member, member_id: int, ip: str = "") -> Member:
    member = _managed_member(db, actor, member_id)
    if member.status == "active":
        return member
    tenant = _tenant(db, member.tenant_id or 0)
    if tenant.status != "active":
        raise DomainError(400, "公司已停用")
    if seats_used(db, tenant.id) >= tenant.seat_quota:
        raise DomainError(400, "席位已满")
    if member.relay_user_id:
        get_relay(db).set_status(member.relay_user_id, "active")
    member.status = "active"
    audit(db, actor, "member.enable", member.name, f"恢复 {member.name}", tenant_id=tenant.id, ip=ip)
    db.commit()
    db.refresh(member)
    return member


def revoke_device(db: Session, actor: Member, device_id: int, ip: str = "") -> Device:
    _require(actor, "member.manage")
    device = db.get(Device, device_id)
    if device is None:
        raise DomainError(404, "设备不存在")
    member = db.get(Member, device.member_id)
    if member is None:
        raise DomainError(404, "成员不存在")
    resolve_tenant(actor, member.tenant_id)
    if device.revoked_at is None:
        device.revoked_at = utcnow()
        db.query(MemberToken).filter(
            MemberToken.device_id == device.id, MemberToken.revoked_at.is_(None)
        ).update({MemberToken.revoked_at: device.revoked_at})
        audit(
            db,
            actor,
            "device.revoke",
            device.name,
            f"下线 {member.name} 的 {device.name}",
            tenant_id=member.tenant_id,
            ip=ip,
        )
        db.commit()
    db.refresh(device)
    return device


def _member_for_quota(db: Session, actor: Member, member_id: int) -> Member:
    _require(actor, "quota.allocate")
    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None or member.tenant_id is None:
        raise DomainError(404, "成员不存在")
    resolve_tenant(actor, member.tenant_id)
    if not member.relay_user_id:
        raise DomainError(400, "这个成员的账号还没有开通")
    return member


def allocate(
    db: Session,
    actor: Member,
    *,
    member_id: int,
    amount: object,
    idempotency_key: str,
    remark: str = "",
    period: str = "",
    direction: str = "add",
    ip: str = "",
) -> QuotaAllocation:
    _require(actor, "quota.allocate")
    key = (idempotency_key or "").strip()
    if not key or len(key) > 80:
        raise DomainError(400, "请求缺少编号，请刷新页面后重试")
    if direction not in ("add", "subtract"):
        raise DomainError(400, "方向不正确")
    cents = to_cents(amount)
    if cents <= 0:
        raise DomainError(400, "金额需要大于 0")
    if cents > 100_000_000:
        raise DomainError(400, "单笔金额过大")
    existing = db.query(QuotaAllocation).filter(QuotaAllocation.idempotency_key == key).one_or_none()
    if existing is not None:
        if existing.member_id != member_id or existing.amount_cents != cents or existing.direction != direction:
            raise DomainError(409, "这笔请求已经提交过，请刷新页面后重试")
        if existing.status == "success":
            return existing
        existing.status = "processing"
        db.commit()
        return _finish_quota(db, actor, existing, ip)
    member = _member_for_quota(db, actor, member_id)
    row = QuotaAllocation(
        tenant_id=member.tenant_id,
        member_id=member.id,
        amount_cents=cents,
        direction=direction,
        period=(period or _period())[:16],
        remark=(remark or "").strip()[:200],
        idempotency_key=key,
        operator_id=actor.id,
        status="processing",
        relay_applied=False,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(QuotaAllocation).filter(QuotaAllocation.idempotency_key == key).one()
        if existing.status == "success":
            return existing
        return _finish_quota(db, actor, existing, ip)
    db.refresh(row)
    return _finish_quota(db, actor, row, ip)


def allocate_batch(
    db: Session,
    actor: Member,
    *,
    member_ids: list[int],
    amount: object,
    idempotency_key: str,
    remark: str = "",
    period: str = "",
    direction: str = "add",
    ip: str = "",
) -> list[tuple[int, QuotaAllocation | None, str]]:
    """按同一金额逐个分配或退回。每人用独立幂等键，重复提交不会重复扣款。

    返回 (成员编号, 记录, 错误说明)。错误说明为空表示这一笔成功。某一笔失败不影响已经完成的其他成员。
    """
    _require(actor, "quota.allocate")
    key = (idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise DomainError(400, "请求缺少编号，请刷新页面后重试")
    if direction not in ("add", "subtract"):
        raise DomainError(400, "方向不正确")
    cents = to_cents(amount)
    if cents <= 0:
        raise DomainError(400, "金额需要大于 0")
    if cents > 100_000_000:
        raise DomainError(400, "单笔金额过大")
    ids: list[int] = []
    seen: set[int] = set()
    for raw in member_ids:
        try:
            member_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise DomainError(400, "成员不正确") from exc
        if member_id in seen:
            continue
        seen.add(member_id)
        ids.append(member_id)
    if not ids:
        raise DomainError(400, "请选择成员")
    if len(ids) > 20:
        raise DomainError(400, "一次最多选择 20 名成员")
    companies: set[int] = set()
    for member_id in ids:
        member = db.get(Member, member_id)
        if member is None or member.deleted_at is not None or member.tenant_id is None:
            continue
        companies.add(member.tenant_id)
    if len(companies) > 1:
        raise DomainError(400, "一次只能给同一家公司的成员分配")
    results: list[tuple[int, QuotaAllocation | None, str]] = []
    for member_id in ids:
        try:
            row = allocate(
                db,
                actor,
                member_id=member_id,
                amount=amount,
                idempotency_key=f"{key}:{member_id}",
                remark=remark,
                period=period,
                direction=direction,
                ip=ip,
            )
        except DomainError as exc:
            results.append((member_id, None, exc.detail))
            continue
        results.append((member_id, row, ""))
    return results


def _finish_quota(db: Session, actor: Member, row: QuotaAllocation, ip: str) -> QuotaAllocation:
    if row.status == "success":
        return row
    member = db.get(Member, row.member_id or 0)
    tenant = db.get(Tenant, row.tenant_id)
    if member is None or tenant is None:
        raise DomainError(404, "额度记录缺少成员或公司")
    relay = get_relay(db)
    owner = _wallet_owner(db, tenant)
    if member.id == owner.id:
        row.status = "failed"
        db.commit()
        raise DomainError(400, "公司管理员的余额就是公司余额，不需要分配给自己")
    if row.direction == "add" and member.status != "active":
        row.status = "failed"
        db.commit()
        raise DomainError(400, "成员已停用，不能再分配")
    if row.direction == "add" and owner.balance_cents < row.amount_cents:
        row.status = "failed"
        db.commit()
        raise DomainError(400, "公司管理员的余额不足，请先充值")
    if row.direction == "subtract" and row.amount_cents > max(member.balance_cents, 0):
        row.status = "failed"
        db.commit()
        raise DomainError(400, "成员可退回的余额不足")
    try:
        # 分配和回收都是公司管理员与成员之间的转账，公司总额不变。
        # 两个方向用独立的幂等键，中途失败时重试只会补做没完成的那一半。
        if row.direction == "add":
            out_key, in_key = f"{row.idempotency_key}:out", f"{row.idempotency_key}:in"
        else:
            out_key, in_key = f"{row.idempotency_key}:out", f"{row.idempotency_key}:in"
        out_id = owner.relay_user_id if row.direction == "add" else member.relay_user_id
        in_id = member.relay_user_id if row.direction == "add" else owner.relay_user_id
        if not row.relay_applied:
            if not relay.was_applied(out_key, out_id):
                relay.subtract_balance(out_id, row.amount_cents, out_key)
            if not relay.was_applied(in_key, in_id):
                relay.add_balance(in_id, row.amount_cents, in_key)
            row.relay_applied = True
        if row.direction == "add":
            owner.balance_cents -= row.amount_cents
            member.balance_cents += row.amount_cents
            if member.balance_cents >= 0:
                member.overdraft = False
            summary = f"分配 ¥{yuan(row.amount_cents):.2f} 给 {member.name}"
            action = "quota.allocate"
        else:
            member.balance_cents -= row.amount_cents
            owner.balance_cents += row.amount_cents
            summary = f"从 {member.name} 退回 ¥{yuan(row.amount_cents):.2f}"
            action = "quota.reclaim"
        row.status = "success"
        row.summary = summary
        audit(
            db,
            actor,
            action,
            member.name,
            summary,
            tenant_id=tenant.id,
            after={"amount": yuan(row.amount_cents), "direction": row.direction},
            ip=ip,
        )
        db.commit()
        db.refresh(row)
        return row
    except DomainError:
        db.rollback()
        fresh = db.get(QuotaAllocation, row.id)
        if fresh is not None and fresh.status != "success":
            fresh.status = "failed"
            db.commit()
        raise
    except Exception as exc:
        db.rollback()
        fresh = db.get(QuotaAllocation, row.id)
        member = db.get(Member, row.member_id or 0)
        applied_now = relay.was_applied(row.idempotency_key, member.relay_user_id if member else "")
        if applied_now and fresh is not None:
            fresh.relay_applied = True
            fresh.status = "processing"
            db.commit()
            raise DomainError(503, "这笔额度已经处理，记录还没写完。请重新提交一次，不会重复扣款。") from exc
        if fresh is not None and fresh.status != "success":
            fresh.status = "failed"
            db.commit()
        raise DomainError(502, "额度操作失败，请稍后再试") from exc


def visible_member_ids(db: Session, actor: Member, tenant_id: int | None) -> list[int] | None:
    """None 表示平台看全部。"""
    if actor.role == "platform_admin":
        if tenant_id is None:
            return None
        return [row.id for row in db.query(Member.id).filter(Member.tenant_id == tenant_id).all()]
    if actor.role == "tenant_admin":
        resolve_tenant(actor, tenant_id)
        return [row.id for row in db.query(Member.id).filter(Member.tenant_id == actor.tenant_id).all()]
    return [actor.id]


def reconcile(db: Session, tenant_ids: list[int]) -> dict:
    """钱以中转站为准：每个成员本地余额对齐中转站余额，差额为 0 才算平。

    公司总额 = 所有成员余额之和（公司管理员也是成员），消耗会让余额下降。
    """
    balance = cost = lifetime_cost = previous = 0
    names: list[str] = []
    parts = []
    ok = True
    for tenant_id in tenant_ids:
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            continue
        part_balance = 0
        part_cost = 0
        part_lifetime_cost = 0
        part_names: list[str] = []
        part_ok = True
        members = (
            db.query(Member)
            .filter(Member.tenant_id == tenant_id, Member.deleted_at.is_(None), Member.role != "platform_admin")
            .all()
        )
        for member in members:
            if member.status == "invited":
                continue
            part_cost += member.month_cost_cents
            part_lifetime_cost += max(member.lifetime_cost_cents, member.month_cost_cents)
            if member.balance_cents < 0 or member.overdraft:
                part_names.append(member.name)
                part_ok = False
                continue
            part_balance += member.balance_cents
            # 同步过就要求本地余额和中转站一致；没同步过的不做判断。
            if member.relay_checked_at is not None and member.balance_cents != member.relay_balance_cents:
                part_ok = False
        ok = ok and part_ok
        balance += part_balance
        cost += part_cost
        lifetime_cost += part_lifetime_cost
        previous += tenant.previous_month_cost_cents
        names.extend(part_names)
        parts.append(
            {
                "tenant_id": tenant.id,
                "name": tenant.name,
                "ok": part_ok,
                "balance": yuan(part_balance),
                "month_cost": yuan(part_cost),
                "cumulative_cost": yuan(part_lifetime_cost),
                # 公司总额 = 余额 + 已消耗；池剩余就是当前余额。
                "allocated": yuan(part_balance + part_lifetime_cost),
                "pool_remaining": yuan(part_balance),
                "pool_total": yuan(part_balance + part_lifetime_cost),
                "overdraft_members": part_names,
            }
        )
    overall = not names and ok
    return {
        "ok": overall,
        "balance": yuan(balance),
        "month_cost": yuan(cost),
        "cumulative_cost": yuan(lifetime_cost),
        # 公司总额 = 余额 + 已消耗；可分配就是当前余额。
        "allocated": yuan(balance + lifetime_cost),
        "pool_remaining": yuan(balance),
        "pool_total": yuan(balance + lifetime_cost),
        "previous_month_cost": yuan(previous),
        "overdraft": bool(names),
        "overdraft_members": names,
        "tenants": parts,
    }


def _relay_members(db: Session, actor: Member, tenant_id: int | None) -> list[Member]:
    query = db.query(Member).filter(Member.deleted_at.is_(None), Member.role != "platform_admin")
    if actor.role == "member":
        return query.filter(Member.id == actor.id).all()
    target = resolve_tenant(actor, tenant_id)
    if target is not None:
        query = query.filter(Member.tenant_id == target)
    elif actor.role != "platform_admin":
        query = query.filter(Member.tenant_id == actor.tenant_id)
    return query.order_by(Member.id).all()


def relay_report(db: Session, actor: Member, tenant_id: int | None, mode: str | None = None) -> dict:
    """只用最近一次同步落下的快照，打开页面不打中转站。"""
    members = _relay_members(db, actor, tenant_id)
    rows: list[dict] = []
    local_total = relay_total = month_total = lifetime_total = 0
    checked_at = None
    expected = 0
    for member in members:
        if not member.relay_user_id:
            continue
        expected += 1
        diff = member.relay_balance_cents - member.balance_cents if member.relay_checked_at else 0
        status_ok = not member.relay_status or member.relay_status == member.status
        ok = bool(member.relay_checked_at) and diff == 0 and status_ok
        local_total += member.balance_cents
        relay_total += member.relay_balance_cents
        month_total += member.month_cost_cents
        lifetime_total += member.lifetime_cost_cents
        if member.relay_checked_at and (checked_at is None or member.relay_checked_at > checked_at):
            checked_at = member.relay_checked_at
        rows.append(
            {
                "member_id": member.id,
                "name": member.name,
                "email": member.email,
                "relay_user_id": member.relay_user_id,
                "local_balance": yuan(member.balance_cents),
                "relay_balance": yuan(member.relay_balance_cents),
                "diff": yuan(diff),
                "local_status": member.status,
                "relay_status": member.relay_status,
                "month_cost": yuan(member.month_cost_cents),
                "lifetime_cost": yuan(member.lifetime_cost_cents),
                "checked_at": iso(member.relay_checked_at),
                "ok": ok,
            }
        )
    return {
        "mode": mode or relay_mode(),
        "synced": checked_at is not None,
        "checked_at": iso(checked_at),
        "ok": bool(rows) and all(row["ok"] for row in rows),
        "members": rows,
        "totals": {
            "local_balance": yuan(local_total),
            "relay_balance": yuan(relay_total),
            "diff": yuan(relay_total - local_total),
            "month_cost": yuan(month_total),
            "lifetime_cost": yuan(lifetime_total),
            "members": expected,
        },
    }


def _sync_daily_snapshots(db: Session, relayer, member: Member, days: int = 7) -> bool:
    """按天拉最近 N 天的用量，写进快照表，供总览的 7 天趋势图使用。

    某一天失败就跳过。碰到限流返回 False，调用方停止后面的人，避免把次数再打光。
    """
    today = shanghai_now().date()
    for offset in range(days):
        day = today - timedelta(days=offset)
        try:
            stats = relayer.usage_summary(member.relay_user_id, day, day)
        except DomainError as exc:
            if exc.detail == RATE_LIMITED:
                return False
            continue
        record_usage_snapshot(
            db,
            tenant_id=member.tenant_id,
            member_id=member.id,
            day=day,
            requests=int(stats["requests"]),
            input_tokens=int(stats["input_tokens"]),
            output_tokens=int(stats["output_tokens"]),
            cost_cents=int(stats["cost_cents"]),
        )
    return True


def _read_member_relay(relayer, member: Member, today: date, month_start: date, lifetime_start: date) -> dict:
    """只读中转站，不改成员。账号不存在时 missing 为 True。"""
    snapshot = relayer.user_snapshot(member.relay_user_id)
    if snapshot is None:
        return {"missing": True}
    return {
        "missing": False,
        "snapshot": snapshot,
        "month": relayer.usage_summary(member.relay_user_id, month_start, today),
        "lifetime": relayer.usage_summary(member.relay_user_id, lifetime_start, today),
    }


def _store_member_balance(member: Member, now, payload: dict) -> None:
    member.relay_checked_at = now
    if payload["missing"]:
        member.relay_balance_cents = 0
        member.relay_status = "missing"
        return
    snapshot = payload["snapshot"]
    member.relay_balance_cents = int(snapshot["balance_cents"])
    member.relay_status = str(snapshot.get("status") or "")[:16]
    # 钱以中转站为准：同步时把本地余额对齐中转站，消耗自然从余额里减掉。
    member.balance_cents = int(snapshot["balance_cents"])
    member.overdraft = member.balance_cents < 0


def _store_member_usage(member: Member, payload: dict) -> None:
    month = payload["month"]
    lifetime = payload["lifetime"]
    member.month_requests = int(month["requests"])
    member.month_input_tokens = int(month["input_tokens"])
    member.month_output_tokens = int(month["output_tokens"])
    member.month_cost_cents = int(month["cost_cents"])
    member.lifetime_cost_cents = max(int(lifetime["cost_cents"]), member.month_cost_cents)


def sync_relay(db: Session, actor: Member, tenant_id: int | None, ip: str = "") -> dict:
    """从中转站拉余额和用量，写成成员快照。只读中转站，不动本地账本。

    余额先读完再拉按日用量。按日用量大约占掉七成请求，以前和余额交织在一起，
    中转站一返回 429，整次同步就失败，已经读到的余额也会回滚。
    """
    _require(actor, "quota.allocate")
    members = _relay_members(db, actor, tenant_id)
    relayer = get_relay(db)
    if relayer.mode == "local":
        raise DomainError(400, "当前是演示环境，不能同步余额")
    today = shanghai_now().date()
    month_start = today.replace(day=1)
    lifetime_start = date(2000, 1, 1)
    now = utcnow()
    synced = 0
    missing: list[str] = []
    failed: list[str] = []
    pulled: list[tuple[Member, dict]] = []
    limited = False
    for member in members:
        if not member.relay_user_id:
            continue
        if limited:
            failed.append(member.name)
            continue
        payload, error = _pull_member(relayer, member, today, month_start, lifetime_start)
        if error is not None and error.detail == RATE_LIMITED:
            time.sleep(SYNC_RATE_LIMIT_WAIT)
            payload, error = _pull_member(relayer, member, today, month_start, lifetime_start)
        if error is not None:
            failed.append(member.name)
            limited = error.detail == RATE_LIMITED
            continue
        _store_member_balance(member, now, payload)
        if payload["missing"]:
            missing.append(member.name)
            continue
        pulled.append((member, payload))
        synced += 1
    for member, _payload in pulled:
        # 按日快照会重算月度合计，循环结束后再用中转站的权威值覆盖。
        if not _sync_daily_snapshots(db, relayer, member):
            logger.warning("同步余额时中转站限流，剩余成员的按日用量这次不拉")
            break
    for member, payload in pulled:
        _store_member_usage(member, payload)
    if synced == 0 and failed:
        raise DomainError(502, RATE_LIMITED if limited else "读取失败，请稍后再试")
    note = f"更新 {synced} 名成员的余额和用量"
    if missing:
        note += f"，{len(missing)} 个账号未找到"
    if failed:
        note += f"，{len(failed)} 人暂时没有读到"
    audit(
        db,
        actor,
        "usage.refresh",
        "usage",
        note,
        tenant_id=resolve_tenant(actor, tenant_id),
        after={"synced": synced, "missing": missing, "failed": failed},
        ip=ip,
    )
    db.commit()
    report = relay_report(db, actor, tenant_id, mode=relayer.mode)
    report["missing"] = missing
    report["failed"] = failed
    return report


def _pull_member(relayer, member: Member, today: date, month_start: date, lifetime_start: date):
    try:
        return _read_member_relay(relayer, member, today, month_start, lifetime_start), None
    except DomainError as exc:
        return None, exc


def trend(db: Session, member_ids: list[int] | None) -> list[dict]:
    end = shanghai_now().date()
    start = end - timedelta(days=6)
    query = db.query(UsageSnapshot).filter(UsageSnapshot.day >= start, UsageSnapshot.day <= end)
    if member_ids is not None:
        if not member_ids:
            return []
        query = query.filter(UsageSnapshot.member_id.in_(member_ids))
    buckets: dict[str, int] = {}
    for row in query.all():
        key = row.day.isoformat()
        buckets[key] = buckets.get(key, 0) + row.cost_cents
    days = []
    for offset in range(6, -1, -1):
        day = end - timedelta(days=offset)
        key = day.isoformat()
        days.append({"date": key, "cost": yuan(buckets.get(key, 0))})
    return days


def resolve_consumption_range(start: date | None, end: date | None, today: date | None = None) -> tuple[date, date]:
    """缺省是业务时区的本月 1 日到今天。两边都要给，且不能超过 366 天。"""
    clock = today or business_today()
    if start is None and end is None:
        return clock.replace(day=1), clock
    if start is None or end is None:
        raise DomainError(400, "开始日期和结束日期需要同时填写")
    if start > end:
        raise DomainError(400, "开始日期不能晚于结束日期")
    if (end - start).days + 1 > MAX_CONSUMPTION_DAYS:
        raise DomainError(400, "查询区间不能超过 366 天")
    return start, end


def _month_last_day(day: date) -> date:
    if day.month == 12:
        nxt = date(day.year + 1, 1, 1)
    else:
        nxt = date(day.year, day.month + 1, 1)
    return nxt - timedelta(days=1)


def previous_month_window(start: date, end: date, today: date) -> tuple[date, date] | None:
    """整月，或本月 1 日到今天，才和上一个日历月比。其他区间不比。

    整月对整月。本月还没过完时，上月只取到同一个日号，短月截到月末。
    """
    if start.day != 1 or (start.year, start.month) != (end.year, end.month):
        return None
    last = _month_last_day(start)
    month_to_date = (start.year, start.month) == (today.year, today.month) and end == today
    if end != last and not month_to_date:
        return None
    previous_last = start - timedelta(days=1)
    previous_first = previous_last.replace(day=1)
    if end == last:
        return previous_first, previous_last
    return previous_first, previous_first.replace(day=min(end.day, previous_last.day))


def _as_day(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _consumption_scope(db: Session, actor: Member, tenant_id: int | None) -> tuple[int | None, int | None, list[int]]:
    """返回 (成员过滤, 公司过滤, 要列出的公司)。平台不传公司时两个过滤都是 None。"""
    if not any(has_perm(actor.role, item) for item in ("usage.read.all", "usage.read.tenant", "usage.read.self")):
        raise DomainError(403, "没有权限")
    if actor.role == "member":
        if tenant_id is not None and tenant_id != actor.tenant_id:
            raise DomainError(403, "不能查看或修改其他公司的数据")
        tenant_ids = [actor.tenant_id] if actor.tenant_id else []
        return actor.id, actor.tenant_id, tenant_ids
    target = resolve_tenant(actor, tenant_id)
    if actor.role == "platform_admin" and target is None:
        return None, None, [row.id for row in db.query(Tenant.id).order_by(Tenant.id).all()]
    if target is None:
        return None, None, []
    return None, target, [target]


def _snapshots_between(
    db: Session,
    start: date,
    end: date,
    member_id: int | None,
    tenant_id: int | None,
) -> list[UsageSnapshot]:
    query = db.query(UsageSnapshot).filter(UsageSnapshot.day >= start, UsageSnapshot.day <= end)
    if member_id is not None:
        query = query.filter(UsageSnapshot.member_id == member_id)
    if tenant_id is not None:
        query = query.filter(UsageSnapshot.tenant_id == tenant_id)
    return query.all()


def consumption_report(
    db: Session,
    actor: Member,
    start: date | None,
    end: date | None,
    tenant_id: int | None = None,
) -> dict:
    """按区间汇总实际消耗。

    只加 usage_snapshots 的每日扣费。同步用量时按天写下的就是这张账，能按任意区间重算。
    不读成员表的 month_cost_cents：演示数据会把那一列写成固定数，也不能代表选中的区间。
    relay_ledger 只记额度划入划出，不是识别扣费，不计入消耗。
    """
    today = business_today()
    start, end = resolve_consumption_range(start, end, today)
    member_id, scoped_tenant, tenant_ids = _consumption_scope(db, actor, tenant_id)
    rows = _snapshots_between(db, start, end, member_id, scoped_tenant)
    by_member: dict[int, int] = {}
    by_tenant: dict[int, int] = {}
    by_day: dict[date, int] = {}
    total_cents = 0
    for row in rows:
        cents = max(0, int(row.cost_cents or 0))
        total_cents += cents
        by_member[row.member_id] = by_member.get(row.member_id, 0) + cents
        by_tenant[row.tenant_id] = by_tenant.get(row.tenant_id, 0) + cents
        day = _as_day(row.day)
        by_day[day] = by_day.get(day, 0) + cents

    company_ids = list(tenant_ids)
    for item_id in by_tenant:
        if item_id not in company_ids:
            company_ids.append(item_id)
    tenant_rows = db.query(Tenant).filter(Tenant.id.in_(company_ids)).all() if company_ids else []
    tenant_names = {item.id: item.name for item in tenant_rows}
    companies = [
        {"tenant_id": item_id, "name": tenant_names.get(item_id, "未知公司"), "cost": yuan(by_tenant.get(item_id, 0))}
        for item_id in company_ids
    ]
    companies.sort(key=lambda item: (-int(round(item["cost"] * 100)), item["name"], item["tenant_id"]))

    member_query = db.query(Member).filter(Member.deleted_at.is_(None), Member.role != "platform_admin")
    if member_id is not None:
        member_query = member_query.filter(Member.id == member_id)
    elif scoped_tenant is not None:
        member_query = member_query.filter(Member.tenant_id == scoped_tenant)
    listed = member_query.all()
    listed_ids = {item.id for item in listed}
    extra_ids = [item_id for item_id in by_member if item_id not in listed_ids]
    extras = db.query(Member).filter(Member.id.in_(extra_ids)).all() if extra_ids else []
    people = listed + extras
    for person in people:
        if person.tenant_id and person.tenant_id not in tenant_names:
            company = db.get(Tenant, person.tenant_id)
            if company is not None:
                tenant_names[company.id] = company.name
    members = [
        {
            "member_id": person.id,
            "name": person.name,
            "tenant_id": person.tenant_id,
            "tenant_name": tenant_names.get(person.tenant_id or 0, ""),
            "cost": yuan(by_member.get(person.id, 0)),
        }
        for person in people
    ]
    members.sort(key=lambda item: (-int(round(item["cost"] * 100)), item["name"], item["member_id"]))

    days = []
    cursor = start
    while cursor <= end:
        days.append({"date": cursor.isoformat(), "cost": yuan(by_day.get(cursor, 0))})
        cursor += timedelta(days=1)

    comparison = None
    window = previous_month_window(start, end, today)
    if window is not None:
        previous_start, previous_end = window
        previous_rows = _snapshots_between(db, previous_start, previous_end, member_id, scoped_tenant)
        previous_cents = sum(max(0, int(row.cost_cents or 0)) for row in previous_rows)
        comparison = {
            "label": "较上月",
            "previous_start": previous_start.isoformat(),
            "previous_end": previous_end.isoformat(),
            "previous_total": yuan(previous_cents),
        }
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": business_timezone_name(),
        "total": yuan(total_cents),
        "comparison": comparison,
        "by_day": days,
        "by_company": companies,
        "by_member": members,
    }


def record_usage_snapshot(
    db: Session,
    *,
    tenant_id: int,
    member_id: int,
    day: date,
    requests: int,
    input_tokens: int,
    output_tokens: int,
    cost_cents: int,
) -> UsageSnapshot:
    member = db.query(Member).filter(Member.id == member_id, Member.tenant_id == tenant_id).one_or_none()
    if member is None:
        raise DomainError(404, "成员不存在")
    row = (
        db.query(UsageSnapshot)
        .filter(UsageSnapshot.tenant_id == tenant_id, UsageSnapshot.member_id == member_id, UsageSnapshot.day == day)
        .one_or_none()
    )
    previous_cost = row.cost_cents if row else 0
    if row is None:
        row = UsageSnapshot(tenant_id=tenant_id, member_id=member_id, day=day)
        db.add(row)
    row.requests = max(0, int(requests))
    row.input_tokens = max(0, int(input_tokens))
    row.output_tokens = max(0, int(output_tokens))
    row.cost_cents = max(0, int(cost_cents))
    member.lifetime_cost_cents = max(0, member.lifetime_cost_cents + row.cost_cents - previous_cost)
    db.flush()
    current_month = shanghai_now().date().replace(day=1)
    if day.year == current_month.year and day.month == current_month.month:
        month_rows = (
            db.query(UsageSnapshot)
            .filter(
                UsageSnapshot.member_id == member_id,
                UsageSnapshot.day >= current_month,
                UsageSnapshot.day <= shanghai_now().date(),
            )
            .all()
        )
        member.month_cost_cents = sum(item.cost_cents for item in month_rows)
        member.month_requests = sum(item.requests for item in month_rows)
        member.month_input_tokens = sum(item.input_tokens for item in month_rows)
        member.month_output_tokens = sum(item.output_tokens for item in month_rows)
    db.flush()
    return row


def bind_device(
    db: Session,
    *,
    email: str,
    relay_user_id: str,
    device_name: str,
    client_version: str = "",
) -> tuple[str, Member, Device]:
    """对上公司成员后签发设备令牌。对不上就拒绝，客户端继续用本机规则。"""
    from admin_app.security import new_member_token

    address = (email or "").strip().lower()
    member = db.query(Member).filter(Member.email == address).one_or_none()
    if (
        member is None
        or member.deleted_at is not None
        or not relay_user_id
        or member.relay_user_id != relay_user_id
    ):
        raise DomainError(403, "对不上公司成员，继续使用本机已有规则")
    if member.status != "active":
        raise DomainError(403, "成员已停用")
    tenant = db.get(Tenant, member.tenant_id or 0)
    if tenant is None or tenant.status != "active":
        raise DomainError(403, "公司已停用")
    device = Device(
        member_id=member.id,
        name=(device_name or "未命名设备")[:80],
        last_seen=utcnow(),
        client_version=(client_version or "")[:40],
    )
    db.add(device)
    db.flush()
    raw = new_member_token(member_id=member.id, role=member.role, tenant_id=member.tenant_id)
    db.add(
        MemberToken(
            member_id=member.id,
            token_hash=sha256_text(raw),
            device_id=device.id,
            expires_at=utcnow() + timedelta(days=30),
        )
    )
    db.commit()
    db.refresh(device)
    return raw, member, device


def usage_rows(db: Session, actor: Member, tenant_id: int | None) -> list[Member]:
    query = db.query(Member).filter(Member.deleted_at.is_(None), Member.role != "platform_admin")
    if actor.role == "member":
        return query.filter(Member.id == actor.id).all()
    target = resolve_tenant(actor, tenant_id)
    if target is not None:
        query = query.filter(Member.tenant_id == target)
    elif actor.role != "platform_admin":
        query = query.filter(Member.tenant_id == actor.tenant_id)
    return query.order_by(Member.month_cost_cents.desc()).all()