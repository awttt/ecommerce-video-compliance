from datetime import datetime

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from admin_app.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    seat_quota: Mapped[int] = mapped_column(Integer, default=10)
    relay_group_id: Mapped[str] = mapped_column(String(40), default="")
    relay_group_name: Mapped[str] = mapped_column(String(80), default="")
    pool_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    pool_total_cents: Mapped[int] = mapped_column(Integer, default=0)
    allocated_total_cents: Mapped[int] = mapped_column(Integer, default=0)
    previous_month_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Member(Base):
    __tablename__ = "tenant_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(Text, default="")
    relay_user_id: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    month_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    previous_month_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    month_requests: Mapped[int] = mapped_column(Integer, default=0)
    month_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    month_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    overdraft: Mapped[bool] = mapped_column(Boolean, default=False)
    # 最近一次从中转站读到的快照，只用于对账展示，不参与本地记账。
    relay_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    relay_status: Mapped[str] = mapped_column(String(16), default="")
    relay_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 识别引擎用的中转站凭据。只在服务端保存，接口不返回。
    relay_api_key: Mapped[str] = mapped_column(Text, default="")
    relay_access_token: Mapped[str] = mapped_column(Text, default="")
    relay_refresh_token: Mapped[str] = mapped_column(Text, default="")
    relay_token_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(40), default="")
    role: Mapped[str] = mapped_column(String(32))
    initial_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("tenant_members.id"))
    name: Mapped[str] = mapped_column(String(80))
    last_seen: Mapped[datetime | None] = mapped_column(nullable=True)
    client_version: Mapped[str] = mapped_column(String(40), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class MemberToken(Base):
    __tablename__ = "member_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("tenant_members.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SessionToken(Base):
    __tablename__ = "session_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("tenant_members.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class QuotaAllocation(Base):
    __tablename__ = "quota_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_members.id"), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(16), default="add")
    period: Mapped[str] = mapped_column(String(16), default="")
    remark: Mapped[str] = mapped_column(String(200), default="")
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_members.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="processing")
    relay_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UsageSnapshot(Base):
    __tablename__ = "usage_snapshots"
    __table_args__ = (UniqueConstraint("tenant_id", "member_id", "day", name="uq_usage_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("tenant_members.id"))
    day: Mapped[datetime] = mapped_column(Date)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)


class CompanyRule(Base):
    """一家公司当前生效的审核规则。一行一家公司，默认词也写在这里。"""

    __tablename__ = "company_rules"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    phrases: Mapped[list] = mapped_column(JSON, default=list)
    ignore_phrases: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("tenant_members.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class CompanyRuleSubmission(Base):
    """员工提交的一条规则改动。通过后才改 company_rules 里对应的那一条。"""

    __tablename__ = "company_rule_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("tenant_members.id"))
    op: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(16))
    item_id: Mapped[str] = mapped_column(String(16), default="")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str] = mapped_column(String(200), default="")
    base_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_members.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SharedRule(Base):
    """全公司共用的违规词。通过后所有成员都能看到，并进入检测。"""

    __tablename__ = "shared_rules"
    __table_args__ = (
        Index(
            "uq_shared_rules_active_word",
            "word",
            unique=True,
            sqlite_where=text("status IN ('pending', 'approved')"),
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(200), default="")
    member_id: Mapped[int] = mapped_column(ForeignKey("tenant_members.id"))
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_members.id"), nullable=True)
    review_comment: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_members.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64))
    object: Mapped[str] = mapped_column(String(120), default="")
    summary: Mapped[str] = mapped_column(String(200), default="")
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    relay_base_url: Mapped[str] = mapped_column(String(200), default="")
    relay_gateway_url: Mapped[str] = mapped_column(String(200), default="")
    group_name: Mapped[str] = mapped_column(String(80), default="")
    group_id: Mapped[str] = mapped_column(String(40), default="")
    backend_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    registration_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verify_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class RelayAccount(Base):
    __tablename__ = "relay_accounts"

    relay_user_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    groups: Mapped[list] = mapped_column(JSON, default=list)


class RelayLedger(Base):
    __tablename__ = "relay_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    relay_user_id: Mapped[str] = mapped_column(String(40), default="")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
