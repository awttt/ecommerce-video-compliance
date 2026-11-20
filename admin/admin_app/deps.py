from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from admin_app.db import get_db
from admin_app.models import Member, MemberToken, Tenant
from admin_app.permissions import has_perm
from admin_app.security import decode_token, sha256_text
from admin_app.timeutil import utcnow


def current_user(request: Request, db: Session = Depends(get_db)) -> Member:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        token = request.cookies.get("evc_admin_access", "")
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        payload = decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="登录已失效") from exc
    if payload.get("typ") not in ("access", "member"):
        raise HTTPException(status_code=401, detail="登录已失效")
    try:
        member_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="登录已失效") from exc
    member = db.get(Member, member_id)
    if member is None or member.deleted_at is not None or member.status != "active":
        raise HTTPException(status_code=401, detail="请先登录")
    if member.tenant_id is not None:
        tenant = db.get(Tenant, member.tenant_id)
        if tenant is None or tenant.status != "active":
            raise HTTPException(status_code=403, detail="公司已停用")
    if payload.get("typ") == "member":
        stored = db.query(MemberToken).filter(MemberToken.token_hash == sha256_text(token)).one_or_none()
        if stored is None or stored.revoked_at is not None or stored.expires_at < utcnow():
            raise HTTPException(status_code=401, detail="设备令牌已失效")
    return member


def require(perm: str):
    def dependency(user: Member = Depends(current_user)) -> Member:
        if not has_perm(user.role, perm):
            raise HTTPException(status_code=403, detail="没有权限")
        return user

    return dependency


def client_ip(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""
