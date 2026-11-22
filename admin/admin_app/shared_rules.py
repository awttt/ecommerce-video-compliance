"""全公司共用的违规词。提交、审批和可见范围都在这里。"""

import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin_app.errors import DomainError
from admin_app.models import Member, SharedRule, Tenant
from admin_app.permissions import has_perm
from admin_app.service import audit
from admin_app.timeutil import iso, utcnow

CATEGORIES = ("价格", "规格", "品牌", "夸大宣传")
STATUSES = ("pending", "approved", "rejected")
STATUS_LABEL = {"pending": "待审", "approved": "已通过", "rejected": "已驳回"}


def _require_submit(actor: Member) -> None:
    if not has_perm(actor.role, "shared_rule.submit"):
        raise DomainError(403, "没有权限")


def _require_review(actor: Member) -> None:
    if not has_perm(actor.role, "shared_rule.review"):
        raise DomainError(403, "没有权限")


def _word(value: str) -> str:
    cleaned = re.sub(r"\s+", "", (value or "").strip())
    if not cleaned or len(cleaned) > 40:
        raise DomainError(400, "违规词需要在 1 到 40 个字之间")
    return cleaned


def _category(value: str) -> str:
    if value not in CATEGORIES:
        raise DomainError(400, "类别只能是价格、规格、品牌、夸大宣传")
    return value


def _note(value: str, *, required: bool, empty: str) -> str:
    text = (value or "").strip()
    if required and not text:
        raise DomainError(400, empty)
    if len(text) > 200:
        raise DomainError(400, "说明不能超过 200 字")
    return text


def shared_rule_dict(db: Session, row: SharedRule) -> dict:
    author = db.get(Member, row.member_id)
    reviewer = db.get(Member, row.reviewer_id) if row.reviewer_id else None
    tenant = db.get(Tenant, row.tenant_id) if row.tenant_id else None
    return {
        "id": row.id,
        "word": row.word,
        "category": row.category,
        "description": row.description,
        "member_id": row.member_id,
        "member_name": author.name if author else "",
        "tenant_id": row.tenant_id,
        "tenant_name": tenant.name if tenant else "",
        "status": row.status,
        "status_label": STATUS_LABEL.get(row.status, row.status),
        "reviewer_id": row.reviewer_id,
        "reviewer_name": reviewer.name if reviewer else "",
        "review_comment": row.review_comment,
        "created_at": iso(row.created_at),
        "reviewed_at": iso(row.reviewed_at),
    }


def _active_word(db: Session, word: str) -> SharedRule | None:
    return (
        db.query(SharedRule)
        .filter(SharedRule.word == word, SharedRule.status.in_(("pending", "approved")))
        .first()
    )


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DomainError(400, "已经有一条相同的违规词在审核或已通过") from exc


def submit_shared_rule(
    db: Session,
    actor: Member,
    *,
    word: str,
    category: str,
    description: str = "",
    ip: str = "",
) -> SharedRule:
    _require_submit(actor)
    cleaned = _word(word)
    chosen = _category(category)
    note = _note(description, required=False, empty="")
    if _active_word(db, cleaned) is not None:
        raise DomainError(400, "已经有一条相同的违规词在审核或已通过")
    row = SharedRule(
        word=cleaned,
        category=chosen,
        description=note,
        member_id=actor.id,
        tenant_id=actor.tenant_id,
        status="pending",
    )
    db.add(row)
    audit(
        db,
        actor,
        "shared_rule.submit",
        cleaned,
        f"提交共享违规词「{cleaned}」",
        tenant_id=actor.tenant_id,
        after={"status": "pending", "category": chosen},
        ip=ip,
    )
    _commit(db)
    db.refresh(row)
    return row


def list_own_shared_rules(db: Session, actor: Member) -> list[SharedRule]:
    _require_submit(actor)
    return (
        db.query(SharedRule)
        .filter(SharedRule.member_id == actor.id)
        .order_by(SharedRule.id.desc())
        .limit(300)
        .all()
    )


def list_approved_shared_rules(db: Session, actor: Member) -> list[SharedRule]:
    _require_submit(actor)
    return (
        db.query(SharedRule)
        .filter(SharedRule.status == "approved")
        .order_by(SharedRule.id.desc())
        .limit(300)
        .all()
    )


def list_pending_shared_rules(db: Session, actor: Member) -> list[SharedRule]:
    _require_review(actor)
    return (
        db.query(SharedRule)
        .filter(SharedRule.status == "pending")
        .order_by(SharedRule.id)
        .limit(300)
        .all()
    )


def _pending(db: Session, rule_id: int) -> SharedRule:
    row = db.get(SharedRule, rule_id)
    if row is None:
        raise DomainError(404, "违规词不存在")
    if row.status != "pending":
        raise DomainError(400, "这条已经审过了")
    return row


def approve_shared_rule(
    db: Session,
    actor: Member,
    rule_id: int,
    *,
    comment: str = "",
    force: bool = False,
    ip: str = "",
) -> SharedRule:
    _require_review(actor)
    row = _pending(db, rule_id)
    note = _note(comment, required=False, empty="")
    if row.member_id == actor.id:
        if not (force and actor.role == "platform_admin" and note):
            raise DomainError(403, "不能审批自己提交的违规词")
    row.status = "approved"
    row.reviewer_id = actor.id
    row.review_comment = note
    row.reviewed_at = utcnow()
    audit(
        db,
        actor,
        "shared_rule.approve",
        row.word,
        f"通过共享违规词「{row.word}」",
        tenant_id=row.tenant_id,
        before={"status": "pending"},
        after={"status": "approved", "force": force},
        ip=ip,
    )
    _commit(db)
    db.refresh(row)
    return row


def reject_shared_rule(
    db: Session,
    actor: Member,
    rule_id: int,
    *,
    comment: str,
    ip: str = "",
) -> SharedRule:
    _require_review(actor)
    row = _pending(db, rule_id)
    if row.member_id == actor.id and actor.role != "platform_admin":
        raise DomainError(403, "不能审批自己提交的违规词")
    note = _note(comment, required=True, empty="驳回需要填写原因")
    row.status = "rejected"
    row.reviewer_id = actor.id
    row.review_comment = note
    row.reviewed_at = utcnow()
    audit(
        db,
        actor,
        "shared_rule.reject",
        row.word,
        f"驳回共享违规词「{row.word}」",
        tenant_id=row.tenant_id,
        before={"status": "pending"},
        after={"status": "rejected"},
        ip=ip,
    )
    _commit(db)
    db.refresh(row)
    return row
