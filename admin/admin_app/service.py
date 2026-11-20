"""租户、成员、额度、命中词和用量。租户范围以登录账号为准，不读请求体里的租户来放权。"""

import re
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin_app.errors import DomainError
from admin_app.models import (
    AppMeta,
    AppSetting,
    AuditLog,
    Device,
    Invite,
    KeywordEntry,
    KeywordSubmission,
    Member,
    MemberToken,
    QuotaAllocation,
    SessionToken,
    Tenant,
    TenantRules,
    UsageSnapshot,
)
from admin_app.money import to_cents, yuan
from admin_app.permissions import ROLE_LABEL, has_perm
from admin_app.relay import apply_credentials, get_relay, relay_mode
from admin_app.security import hash_password, new_invite_token, sha256_text
from admin_app.timeutil import iso, shanghai_now, utcnow

CATEGORIES = ("价格", "规格", "品牌", "夸大宣传")
SOURCES = ("口播", "画面")
DEFAULT_CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"]
_NOTE_BLOCK = ("允许推测", "可以推测", "允许猜测", "可以猜测")


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


def _clean_phrase(text: str) -> str:
    cleaned = re.sub(r"\s+", "", (text or "").strip())
    if not cleaned or len(cleaned) > 40:
        raise DomainError(400, "规则文字需要在 1 到 40 个字之间")
    return cleaned


def _check_phrase(text: str, category: str, source: str) -> str:
    cleaned = _clean_phrase(text)
    if category not in CATEGORIES:
        raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
    if source not in SOURCES:
        raise DomainError(400, "来源只能是口播或画面")
    if category == "夸大宣传" and source != "口播":
        raise DomainError(400, "夸大宣传只看口播，画面不报这一类")
    return cleaned


def _next_version(db: Session) -> int:
    row = db.get(AppMeta, "rules_version")
    if row is None:
        row = AppMeta(key="rules_version", value="0")
        db.add(row)
        db.flush()
    current = int(row.value or "0") + 1
    row.value = str(current)
    return current


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
    if len(password or "") < 8 or len(password) > 72:
        raise DomainError(400, "密码需要在 8 到 72 位之间")


def _relay_account(
    db: Session,
    relay,
    email: str,
    password: str,
    link_existing: bool,
) -> tuple[str, dict]:
    """建新号，或把中转站里已经注册过的账号绑进来。返回 (relay_user_id, credentials)。"""
    if not link_existing:
        return relay.create_user(email, password)
    found = relay.find_user_by_email(email)
    if found is None:
        raise DomainError(400, "中转站里没有这个邮箱，请先让该账号在中转站注册，或关闭「绑定已有账号」")
    status = str(found.get("status") or "").strip().lower()
    if status and status != "active":
        raise DomainError(400, "这个中转站账号已停用，请先在中转站启用")
    relay_user_id = str(found["id"])
    taken = (
        db.query(Member)
        .filter(Member.relay_user_id == relay_user_id, Member.deleted_at.is_(None))
        .first()
    )
    if taken is not None:
        raise DomainError(400, "这个中转站账号已经绑定了其他成员")
    # 已有账号的密码在中转站那边，后台不知道，也不该重置；密钥等成员自己登录后写入。
    return relay_user_id, {}


def create_tenant(
    db: Session,
    actor: Member,
    *,
    name: str,
    admin_email: str,
    admin_name: str,
    admin_password: str,
    seats: int,
    link_existing: bool = False,
    ip: str = "",
) -> tuple[Tenant, Member]:
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
    if not link_existing:
        _password_ok(admin_password)
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
    relay_user_id, credentials = _relay_account(db, relay, email, admin_password, link_existing)
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
    db.add(
        TenantRules(
            tenant_id=tenant.id,
            categories=list(DEFAULT_CATEGORIES),
            ignore_phrases=[],
            note="",
            version=1,
        )
    )
    audit(db, actor, "tenant.create", cleaned, f"新建公司 {cleaned}", tenant_id=tenant.id, ip=ip)
    audit(db, actor, "member.create", email, f"创建公司管理员 {admin_member.name}", tenant_id=tenant.id, after={"relay_user_id": relay_user_id, "role": "tenant_admin"}, ip=ip)
    db.commit()
    db.refresh(tenant)
    return tenant, admin_member


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
    link_existing: bool = False,
    ip: str = "",
) -> Member:
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
    if not link_existing:
        _password_ok(password)
    relay = get_relay(db)
    relay_user_id, credentials = _relay_account(db, relay, address, password, link_existing)
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
    audit(db, actor, "member.create", member.email, f"创建成员 {member.name}", tenant_id=tenant.id, after={"relay_user_id": relay_user_id, "role": role}, ip=ip)
    db.commit()
    db.refresh(member)
    return member


def accept_invite(db: Session, token: str, password: str, name: str = "") -> tuple[Member, str]:
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
    relay_user_id, credentials = relay.create_user(row.email, password)
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
        after={"relay_user_id": relay_user_id, "role": member.role},
    )
    db.commit()
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
    return member, warning


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
        raise DomainError(400, "这个成员还没有中转站账号")
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
        raise DomainError(400, "缺少幂等键")
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
            raise DomainError(409, "这个幂等键已经用于另一笔额度")
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
        raise DomainError(400, "公司管理员在中转站的余额不足，请先在 token4u 充值")
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
            raise DomainError(503, "中转站已处理这笔额度，本地记账未完成。请用同一个幂等键重试，不会再次动账。") from exc
        if fresh is not None and fresh.status != "success":
            fresh.status = "failed"
            db.commit()
        raise DomainError(502, "中转站额度操作失败") from exc


def _active_phrase_count(db: Session, tenant_id: int | None) -> int:
    global_count = (
        db.query(KeywordEntry)
        .filter(KeywordEntry.tenant_id.is_(None), KeywordEntry.deleted_at.is_(None))
        .count()
    )
    if tenant_id is None:
        return global_count
    local = (
        db.query(KeywordEntry)
        .filter(KeywordEntry.tenant_id == tenant_id, KeywordEntry.deleted_at.is_(None))
        .count()
    )
    return global_count + local


def submit_keyword(
    db: Session,
    actor: Member,
    *,
    text: str,
    category: str,
    source: str,
    scope: str,
    tenant_id: int | None,
    ip: str = "",
) -> KeywordSubmission:
    _require(actor, "keyword.submit")
    cleaned = _check_phrase(text, category, source)
    if scope not in ("global", "tenant"):
        raise DomainError(400, "作用域只能是全局或本公司")
    if scope == "global":
        if actor.role != "platform_admin":
            raise DomainError(403, "只有平台管理员可以提交全局命中词")
        target = None
    else:
        target = resolve_tenant(actor, tenant_id)
        if target is None:
            raise DomainError(400, "请选择公司")
    _ensure_unique_phrase(db, cleaned, category, source, target)
    row = KeywordSubmission(
        tenant_id=target,
        member_id=actor.id,
        text=cleaned,
        category=category,
        source=source,
        scope=scope,
        status="pending",
    )
    db.add(row)
    audit(db, actor, "keyword.submit", cleaned, f"提交命中词「{cleaned}」", tenant_id=target, ip=ip)
    db.commit()
    db.refresh(row)
    return row


def _ensure_unique_phrase(db: Session, text: str, category: str, source: str, tenant_id: int | None) -> None:
    entry = (
        db.query(KeywordEntry)
        .filter(
            KeywordEntry.text == text,
            KeywordEntry.category == category,
            KeywordEntry.source == source,
            KeywordEntry.deleted_at.is_(None),
            KeywordEntry.tenant_id.is_(None) if tenant_id is None else KeywordEntry.tenant_id == tenant_id,
        )
        .first()
    )
    pending = (
        db.query(KeywordSubmission)
        .filter(
            KeywordSubmission.text == text,
            KeywordSubmission.category == category,
            KeywordSubmission.source == source,
            KeywordSubmission.status == "pending",
            KeywordSubmission.tenant_id.is_(None) if tenant_id is None else KeywordSubmission.tenant_id == tenant_id,
        )
        .first()
    )
    if entry or pending:
        raise DomainError(400, "已经有一条相同的命中词")


def _load_submission(db: Session, actor: Member, submission_id: int) -> KeywordSubmission:
    row = db.get(KeywordSubmission, submission_id)
    if row is None:
        raise DomainError(404, "提交不存在")
    if actor.role == "member" and row.member_id != actor.id:
        raise DomainError(403, "只能查看自己的提交")
    if actor.role == "tenant_admin":
        resolve_tenant(actor, row.tenant_id if row.scope == "tenant" else actor.tenant_id)
        if row.scope == "global":
            raise DomainError(403, "公司管理员不能审批全局词")
    return row


def approve_keyword(
    db: Session,
    actor: Member,
    submission_id: int,
    *,
    remark: str = "",
    force: bool = False,
    ip: str = "",
) -> KeywordSubmission:
    _require(actor, "keyword.review")
    row = _load_submission(db, actor, submission_id)
    if row.status != "pending":
        raise DomainError(400, "这条已经审过了")
    if row.scope == "global" and not has_perm(actor.role, "keyword.review.global"):
        raise DomainError(403, "公司管理员不能审批全局词")
    note = (remark or "").strip()[:200]
    if row.member_id == actor.id:
        if not (force and actor.role == "platform_admin"):
            raise DomainError(403, "不能审批自己提交的词")
        if not note:
            raise DomainError(400, "强制通过需要填写备注")
    if _active_phrase_count(db, row.tenant_id) >= 300:
        raise DomainError(400, "通过的命中词全局加公司不能超过 300 条")
    version = _next_version(db)
    entry = KeywordEntry(
        tenant_id=row.tenant_id,
        text=row.text,
        category=row.category,
        source=row.source,
        version=version,
    )
    db.add(entry)
    db.flush()
    row.status = "approved"
    row.reviewer_id = actor.id
    row.reviewed_at = utcnow()
    row.remark = note
    row.entry_id = entry.id
    rules = db.get(TenantRules, row.tenant_id) if row.tenant_id else None
    if rules is not None:
        rules.version = version
        rules.updated_at = utcnow()
    audit(
        db,
        actor,
        "keyword.approve",
        row.text,
        f"通过命中词「{row.text}」",
        tenant_id=row.tenant_id,
        before={"status": "pending"},
        after={"status": "approved", "version": version, "force": force},
        ip=ip,
    )
    db.commit()
    db.refresh(row)
    return row


def reject_keyword(db: Session, actor: Member, submission_id: int, remark: str, ip: str = "") -> KeywordSubmission:
    _require(actor, "keyword.review")
    row = _load_submission(db, actor, submission_id)
    if row.status != "pending":
        raise DomainError(400, "这条已经审过了")
    if row.member_id == actor.id and actor.role != "platform_admin":
        raise DomainError(403, "不能审批自己提交的词")
    note = (remark or "").strip()
    if not note:
        raise DomainError(400, "驳回需要填写备注")
    row.status = "rejected"
    row.reviewer_id = actor.id
    row.reviewed_at = utcnow()
    row.remark = note[:200]
    audit(
        db,
        actor,
        "keyword.reject",
        row.text,
        f"驳回命中词「{row.text}」",
        tenant_id=row.tenant_id,
        before={"status": "pending"},
        after={"status": "rejected"},
        ip=ip,
    )
    db.commit()
    db.refresh(row)
    return row


def delete_keyword(db: Session, actor: Member, entry_id: int, ip: str = "") -> KeywordEntry:
    _require(actor, "keyword.review")
    entry = db.get(KeywordEntry, entry_id)
    if entry is None or entry.deleted_at is not None:
        raise DomainError(404, "命中词不存在")
    if entry.tenant_id is None and not has_perm(actor.role, "keyword.review.global"):
        raise DomainError(403, "公司管理员不能删除全局词")
    if entry.tenant_id is not None:
        resolve_tenant(actor, entry.tenant_id)
    version = _next_version(db)
    entry.deleted_at = utcnow()
    entry.version = version
    audit(
        db,
        actor,
        "keyword.delete",
        entry.text,
        f"删除命中词「{entry.text}」",
        tenant_id=entry.tenant_id,
        after={"version": version},
        ip=ip,
    )
    db.commit()
    db.refresh(entry)
    return entry


def save_rule_settings(
    db: Session,
    actor: Member,
    *,
    tenant_id: int | None,
    categories: list[str],
    ignore_phrases: list[str],
    note: str,
    ip: str = "",
) -> TenantRules:
    if actor.role not in ("platform_admin", "tenant_admin"):
        raise DomainError(403, "没有权限")
    target = resolve_tenant(actor, tenant_id)
    if target is None:
        raise DomainError(400, "请选择公司")
    unique: list[str] = []
    for item in categories:
        if item not in CATEGORIES:
            raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
        if item not in unique:
            unique.append(item)
    cleaned_ignore: list[str] = []
    for item in ignore_phrases:
        text = re.sub(r"\s+", "", str(item).strip())
        if not text:
            continue
        if len(text) > 40:
            raise DomainError(400, "忽略词需要在 1 到 40 个字之间")
        if text not in cleaned_ignore:
            cleaned_ignore.append(text)
    if len(cleaned_ignore) > 30:
        raise DomainError(400, "忽略词最多 30 条")
    body = (note or "").strip()
    if len(body) > 400:
        raise DomainError(400, "补充说明不能超过 400 字")
    if any(token in body for token in _NOTE_BLOCK):
        raise DomainError(400, "补充说明不能放宽禁止推测")
    if not unique and _active_phrase_count(db, target) == 0:
        raise DomainError(400, "至少保留一个类别，或一条已通过的命中词")
    rules = db.get(TenantRules, target)
    if rules is None:
        rules = TenantRules(tenant_id=target, categories=[], ignore_phrases=[], note="")
        db.add(rules)
    rules.categories = unique
    rules.ignore_phrases = cleaned_ignore
    rules.note = body
    rules.version = _next_version(db)
    rules.updated_at = utcnow()
    audit(db, actor, "rules.update", str(target), "更新公司审核规则", tenant_id=target, ip=ip)
    db.commit()
    db.refresh(rules)
    return rules


def rules_payload(db: Session, actor: Member, since: int, tenant_id: int | None) -> dict:
    if actor.role == "member":
        target = actor.tenant_id
    else:
        target = resolve_tenant(actor, tenant_id)
    meta = db.get(AppMeta, "rules_version")
    current = int(meta.value) if meta and meta.value else 0
    query = db.query(KeywordEntry).filter(KeywordEntry.version > since)
    if target is None and actor.role == "platform_admin":
        scoped = query
    else:
        scoped = query.filter((KeywordEntry.tenant_id.is_(None)) | (KeywordEntry.tenant_id == target))
    phrases = []
    removed = []
    for entry in scoped.order_by(KeywordEntry.version, KeywordEntry.id).all():
        item = {
            "id": entry.id,
            "text": entry.text,
            "category": entry.category,
            "source": entry.source,
            "scope": "global" if entry.tenant_id is None else "tenant",
            "scope_label": "全局" if entry.tenant_id is None else "公司",
            "version": entry.version,
        }
        if entry.deleted_at is None:
            phrases.append(item)
        else:
            removed.append(entry.id)
    rules = db.get(TenantRules, target) if target else None
    return {
        "version": current,
        "categories": list(rules.categories) if rules else list(DEFAULT_CATEGORIES),
        "ignore_phrases": list(rules.ignore_phrases) if rules else [],
        "note": rules.note if rules else "",
        "phrases": phrases,
        "removed_ids": removed,
        "speculation_policy": "只报告画面里看得见的文字，或口播里连续说出来的原话。禁止推测优先于补充说明。",
    }


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


def _sync_daily_snapshots(db: Session, relayer, member: Member, days: int = 7) -> None:
    """按天拉最近 N 天的用量，写进快照表，供总览的 7 天趋势图使用。

    逐个自然日请求中转站；某一天失败就跳过，不影响其他天，也不影响同步主流程。
    """
    today = shanghai_now().date()
    for offset in range(days):
        day = today - timedelta(days=offset)
        try:
            stats = relayer.usage_summary(member.relay_user_id, day, day)
        except DomainError:
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


def sync_relay(db: Session, actor: Member, tenant_id: int | None, ip: str = "") -> dict:
    """从中转站拉余额和用量，写成成员快照。只读中转站，不动本地账本。"""
    _require(actor, "quota.allocate")
    members = _relay_members(db, actor, tenant_id)
    relayer = get_relay(db)
    if relayer.mode == "local":
        raise DomainError(400, "未配置中转站管理密钥，当前是本地演示，不能从中转站同步")
    today = shanghai_now().date()
    month_start = today.replace(day=1)
    lifetime_start = date(2000, 1, 1)
    now = utcnow()
    synced = 0
    missing: list[str] = []
    for member in members:
        if not member.relay_user_id:
            continue
        snapshot = relayer.user_snapshot(member.relay_user_id)
        member.relay_checked_at = now
        if snapshot is None:
            member.relay_balance_cents = 0
            member.relay_status = "missing"
            missing.append(member.name)
            continue
        month = relayer.usage_summary(member.relay_user_id, month_start, today)
        lifetime = relayer.usage_summary(member.relay_user_id, lifetime_start, today)
        # 先写 7 天明细快照；它的 upsert 会重算月度合计，所以后面再用中转站的权威值覆盖。
        _sync_daily_snapshots(db, relayer, member)
        member.relay_balance_cents = int(snapshot["balance_cents"])
        member.relay_status = str(snapshot.get("status") or "")[:16]
        # 钱以中转站为准：同步时把本地余额对齐中转站，消耗自然从余额里减掉。
        member.balance_cents = int(snapshot["balance_cents"])
        member.overdraft = member.balance_cents < 0
        member.month_requests = int(month["requests"])
        member.month_input_tokens = int(month["input_tokens"])
        member.month_output_tokens = int(month["output_tokens"])
        member.month_cost_cents = int(month["cost_cents"])
        member.lifetime_cost_cents = max(int(lifetime["cost_cents"]), member.month_cost_cents)
        synced += 1
    audit(
        db,
        actor,
        "relay.sync",
        "relay",
        f"从中转站同步 {synced} 名成员" + (f"，{len(missing)} 个账号在中转站不存在" if missing else ""),
        tenant_id=resolve_tenant(actor, tenant_id),
        after={"synced": synced, "missing": missing},
        ip=ip,
    )
    db.commit()
    report = relay_report(db, actor, tenant_id, mode=relayer.mode)
    report["missing"] = missing
    return report


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


def pending_count(db: Session, actor: Member) -> int:
    query = db.query(KeywordSubmission).filter(KeywordSubmission.status == "pending")
    if actor.role == "platform_admin":
        return query.count()
    if actor.role == "tenant_admin":
        return query.filter(
            KeywordSubmission.tenant_id == actor.tenant_id,
            KeywordSubmission.scope == "tenant",
        ).count()
    return query.filter(KeywordSubmission.member_id == actor.id).count()


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