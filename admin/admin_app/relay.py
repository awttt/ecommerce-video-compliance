"""中转站管理接口。没有管理密钥时走本地账本，不访问网络，也不读取中转站。"""

import hashlib
from datetime import date, timedelta

from sqlalchemy.orm import Session

from admin_app.config import Settings, get_settings
from admin_app.errors import DomainError
from admin_app.models import AppSetting, Member, RelayAccount, RelayLedger
from admin_app.timeutil import utcnow

# 余额流水里的备注前缀。对账时靠它把本地幂等键和中转站流水对上。
NOTE_PREFIX = "evc-admin:"
_NO_KEY = "当前是演示环境，不能同步余额"


def apply_credentials(member: Member, credentials: dict) -> None:
    """把登录/建号时拿到的一次性密钥和令牌写到成员上。接口永远不回传这些字段。"""
    api_key = str(credentials.get("api_key") or "").strip()
    access = str(credentials.get("access_token") or "").strip()
    refresh = str(credentials.get("refresh_token") or "").strip()
    if api_key:
        member.relay_api_key = api_key
    if access:
        member.relay_access_token = access
    if refresh:
        member.relay_refresh_token = refresh
    if access:
        expires_in = credentials.get("expires_in")
        try:
            seconds = int(expires_in) if expires_in is not None else 3600
        except (TypeError, ValueError):
            seconds = 3600
        member.relay_token_expires_at = utcnow() + timedelta(seconds=max(60, seconds))


def _cents(value: object) -> int:
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError) as exc:
        raise DomainError(502, "服务返回的金额格式不正确") from exc


class LocalRelay:
    mode = "local"

    def __init__(self, db: Session, group_name: str) -> None:
        self.db = db
        self.group_name = group_name

    def create_user(self, email: str, password: str) -> tuple[str, dict]:
        del password
        existing = self.db.query(RelayAccount).filter(RelayAccount.email == email).one_or_none()
        if existing:
            return existing.relay_user_id, {}
        number = self.db.query(RelayAccount).count() + 2001
        relay_user_id = f"user_{number}"
        self.db.add(
            RelayAccount(
                relay_user_id=relay_user_id,
                email=email,
                status="active",
                groups=[self.group_name] if self.group_name else [],
            )
        )
        self.db.flush()
        return relay_user_id, {}

    def find_user_by_email(self, email: str) -> dict | None:
        """按邮箱查已有账号。找不到返回 None，用于把中转站里已注册的人绑进公司。"""
        row = self.db.query(RelayAccount).filter(RelayAccount.email == email).one_or_none()
        if row is None:
            return None
        return {"id": row.relay_user_id, "email": row.email, "status": row.status}

    def _apply(self, relay_user_id: str, cents: int, key: str, kind: str) -> None:
        if self.was_applied(key, relay_user_id):
            return
        account = self.db.get(RelayAccount, relay_user_id) if relay_user_id else None
        if account is None or account.status != "active":
            raise DomainError(400, "账号不存在或已停用")
        self.db.add(
            RelayLedger(
                idempotency_key=key,
                relay_user_id=relay_user_id,
                amount_cents=cents if kind == "add" else -cents,
                kind=kind,
            )
        )
        self.db.flush()

    def add_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        self._apply(relay_user_id, cents, key, "add")

    def subtract_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        self._apply(relay_user_id, cents, key, "subtract")

    def was_applied(self, key: str, relay_user_id: str = "") -> bool:
        del relay_user_id
        return self.db.query(RelayLedger).filter(RelayLedger.idempotency_key == key).first() is not None

    def set_status(self, relay_user_id: str, status: str) -> None:
        account = self.db.get(RelayAccount, relay_user_id)
        if account is None:
            raise DomainError(400, "账号不存在")
        account.status = status
        self.db.flush()

    def user_snapshot(self, relay_user_id: str) -> dict | None:
        del relay_user_id
        raise DomainError(400, _NO_KEY)

    def usage_summary(self, relay_user_id: str, start: date, end: date) -> dict:
        del relay_user_id, start, end
        raise DomainError(400, _NO_KEY)

    def balance_history(self, relay_user_id: str, page_size: int = 100) -> list[dict]:
        del relay_user_id, page_size
        raise DomainError(400, _NO_KEY)


class HttpRelay:
    mode = "remote"

    def __init__(self, settings: Settings, group_id: str, base_url: str = "") -> None:
        import httpx

        self.settings = settings
        self.group_id = group_id
        self.base_url = (base_url or settings.relay_base_url).rstrip("/")
        self.client = httpx.Client(
            timeout=20.0,
            headers={
                "x-api-key": settings.relay_admin_key,
                "User-Agent": "evc-admin/0.1.0",
            },
        )

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _raise(self, response, action: str) -> None:
        if response.status_code in (401, 403):
            raise DomainError(502, f"{action}失败，服务配置不完整，请联系平台管理员")
        if response.status_code >= 400:
            raise DomainError(502, f"{action}失败，请稍后再试")

    def _payload(self, response, action: str) -> object:
        self._raise(response, action)
        try:
            body = response.json()
        except ValueError as exc:
            raise DomainError(502, f"{action}失败，服务返回的数据格式不对") from exc
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def _get(self, path: str, params: dict | None = None) -> object | None:
        import httpx

        try:
            response = self.client.get(self._url(path), params=params)
        except httpx.HTTPError as exc:
            raise DomainError(502, "连不上服务，读取数据失败") from exc
        if response.status_code == 404:
            return None
        return self._payload(response, "读取")

    def create_user(self, email: str, password: str) -> tuple[str, dict]:
        group = self._configured_group_id()
        response = self.client.post(
            self._url("/admin/users"),
            headers={
                "Idempotency-Key": hashlib.sha256(f"evc-admin-user:{email}".encode()).hexdigest()[:32],
            },
            # token4u 受限密钥的 users.create 明确拒绝附带余额，包括 balance: 0。
            json={
                "email": email,
                "password": password,
                "allowed_groups": [group],
                "restrict_public_groups": True,
            },
        )
        data = self._payload(response, "建号")
        user_id = data.get("id") if isinstance(data, dict) else None
        if not user_id:
            raise DomainError(502, "开通账号失败，服务没有返回用户编号")
        credentials = self._create_default_key(email, password)
        return str(user_id), credentials

    def find_user_by_email(self, email: str) -> dict | None:
        """按邮箱查中转站已有账号。列表接口支持 search，再按邮箱做精确匹配，避免搜到前缀相同的其他账号。"""
        wanted = email.strip().lower()
        data = self._get("/admin/users", params={"search": wanted, "page": 1, "page_size": 50})
        rows = data if isinstance(data, list) else []
        if isinstance(data, dict):
            rows = data.get("items") or data.get("list") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("email") or "").strip().lower() != wanted:
                continue
            user_id = row.get("id") or row.get("user_id")
            if not user_id:
                continue
            return {"id": str(user_id), "email": wanted, "status": str(row.get("status") or "active")}
        return None

    def _create_default_key(self, email: str, password: str) -> dict:
        import httpx

        with httpx.Client(timeout=20.0) as user_client:
            response = user_client.post(
                self._url("/auth/login"),
                json={"email": email, "password": password},
            )
            self._raise(response, "账号登录验证")
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            data = data if isinstance(data, dict) else payload
            access_token = str(data.get("access_token") or "").strip()
            if not access_token:
                raise DomainError(502, "开通账号失败，服务没有返回登录凭证")
            group_id = self._configured_group_id()
            response = user_client.post(
                self._url("/keys"),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Idempotency-Key": hashlib.sha256(f"evc-admin-key:{email}:{group_id}".encode()).hexdigest()[:32],
                },
                json={"name": "电商视频合规", "group_id": group_id},
            )
        self._raise(response, "创建审核密钥")
        created = self._payload(response, "创建审核密钥")
        key = ""
        if isinstance(created, dict):
            key = str(created.get("key") or created.get("api_key") or created.get("secret") or "").strip()
        return {
            "api_key": key,
            "access_token": access_token,
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "expires_in": data.get("expires_in"),
        }

    def _configured_group_id(self) -> int:
        try:
            group_id = int(self.group_id)
        except (TypeError, ValueError) as exc:
            raise DomainError(400, "服务配置不完整，请联系平台管理员") from exc
        if group_id <= 0:
            raise DomainError(400, "服务配置不完整，请联系平台管理员")
        return group_id

    def add_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        response = self.client.post(
            self._url(f"/admin/users/{relay_user_id}/balance"),
            headers={"Idempotency-Key": key},
            json={"balance": round(cents / 100, 2), "operation": "add", "notes": NOTE_PREFIX + key},
        )
        self._raise(response, "加款")

    def subtract_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        response = self.client.post(
            self._url(f"/admin/users/{relay_user_id}/balance"),
            headers={"Idempotency-Key": key},
            json={"balance": round(cents / 100, 2), "operation": "subtract", "notes": NOTE_PREFIX + key},
        )
        self._raise(response, "扣款")

    def was_applied(self, key: str, relay_user_id: str = "") -> bool:
        if not relay_user_id:
            return False
        try:
            entries = self.balance_history(relay_user_id, page_size=100)
        except DomainError:
            return False
        needle = NOTE_PREFIX + key
        return any(str(item.get("notes") or "") == needle for item in entries if isinstance(item, dict))

    def set_status(self, relay_user_id: str, status: str) -> None:
        response = self.client.put(
            self._url(f"/admin/users/{relay_user_id}"),
            json={"status": status},
        )
        self._raise(response, "更新账号状态")

    def user_snapshot(self, relay_user_id: str) -> dict | None:
        data = self._get(f"/admin/users/{relay_user_id}")
        if data is None:
            return None
        if not isinstance(data, dict) or "balance" not in data:
            raise DomainError(502, "服务配置不完整，请联系平台管理员")
        return {
            "id": str(data.get("id") or relay_user_id),
            "email": str(data.get("email") or ""),
            "status": str(data.get("status") or ""),
            "balance_cents": _cents(data.get("balance")),
            "total_recharged_cents": _cents(data.get("total_recharged")),
        }

    def usage_summary(self, relay_user_id: str, start: date, end: date) -> dict:
        data = self._get(
            "/admin/usage/stats",
            params={
                "user_id": relay_user_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "timezone": self.settings.timezone,
            },
        )
        if not isinstance(data, dict):
            raise DomainError(502, "服务没有返回用量统计")
        cost = data.get("total_actual_cost")
        if cost is None:
            cost = data.get("total_cost")
        return {
            "requests": int(data.get("total_requests") or 0),
            "input_tokens": int(data.get("total_input_tokens") or 0),
            "output_tokens": int(data.get("total_output_tokens") or 0),
            "cost_cents": _cents(cost),
        }

    def balance_history(self, relay_user_id: str, page_size: int = 100) -> list[dict]:
        data = self._get(
            f"/admin/users/{relay_user_id}/balance-history",
            params={"page": 1, "page_size": page_size},
        )
        items = data.get("items") if isinstance(data, dict) else data
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _setting(db: Session, field: str) -> str:
    row = db.get(AppSetting, 1)
    value = getattr(row, field, "") if row else ""
    return str(value or "").strip()


def group_name_of(db: Session) -> str:
    return _setting(db, "group_name") or get_settings().relay_group_name


def group_id_of(db: Session) -> str:
    return _setting(db, "group_id") or get_settings().relay_group_id


def base_url_of(db: Session) -> str:
    return _setting(db, "relay_base_url") or get_settings().relay_base_url


def _relay_payload(response, action: str) -> object:
    if response.status_code in (401, 403):
        raise DomainError(401, f"{action}失败，登录已失效")
    if response.status_code >= 400:
        raise DomainError(502, f"{action}失败，请稍后再试")
    try:
        body = response.json()
    except ValueError as exc:
        raise DomainError(502, f"{action}失败，服务返回的数据格式不对") from exc
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _create_or_find_key(base_url: str, access_token: str, group_id: str, email: str) -> str:
    import httpx

    try:
        numeric = int(group_id)
    except (TypeError, ValueError) as exc:
        raise DomainError(400, "服务配置不完整，请联系平台管理员") from exc
    if numeric <= 0:
        raise DomainError(400, "服务配置不完整，请联系平台管理员")
    base = base_url.rstrip("/")
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            f"{base}/keys",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Idempotency-Key": hashlib.sha256(f"evc-admin-key:{email}:{numeric}".encode()).hexdigest()[:32],
                "User-Agent": "evc-admin/0.1.0",
            },
            json={"name": "电商视频合规", "group_id": numeric},
        )
        if response.status_code == 401:
            raise DomainError(401, "登录已失效，请重新登录后再试")
        if response.status_code < 400:
            data = _relay_payload(response, "创建识别密钥")
        else:
            data = None
        if isinstance(data, dict):
            for name in ("key", "api_key", "secret"):
                value = str(data.get(name) or "").strip()
                if value:
                    return value
        # 幂等键已经建过 Key，创建接口可能不再回原文；再列一次当前账号的 Key。
        listing = client.get(
            f"{base}/keys",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": "evc-admin/0.1.0"},
            params={"page": 1, "page_size": 100},
        )
        if listing.status_code == 401:
            raise DomainError(401, "登录已失效，请重新登录后再试")
        if listing.status_code < 400:
            body = _relay_payload(listing, "读取识别密钥")
            items = body.get("items") if isinstance(body, dict) else body
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    if int(item.get("group_id") or 0) != numeric:
                        continue
                except (TypeError, ValueError):
                    continue
                for name in ("key", "api_key", "secret"):
                    value = str(item.get(name) or "").strip()
                    if value:
                        return value
    raise DomainError(502, "没有拿到识别密钥，请让这个成员重新登录一次后台")


def _refresh_member_token(db: Session, member: Member, base_url: str) -> str:
    import httpx

    refresh = str(member.relay_refresh_token or "").strip()
    if not refresh:
        return ""
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/auth/refresh",
                json={"refresh_token": refresh},
                headers={"User-Agent": "evc-admin/0.1.0"},
            )
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    data = _relay_payload(response, "刷新登录")
    if not isinstance(data, dict):
        return ""
    access = str(data.get("access_token") or "").strip()
    if not access:
        return ""
    apply_credentials(
        member,
        {
            "access_token": access,
            "refresh_token": data.get("refresh_token") or refresh,
            "expires_in": data.get("expires_in"),
        },
    )
    db.flush()
    return access


def member_credentials(db: Session, member: Member) -> tuple[str, str]:
    """返回（画面模型 Key，口播用中转站令牌）。没有 Key 时明确报错，不回退到别人的额度。"""
    base = base_url_of(db)
    token = str(member.relay_access_token or "").strip()
    expires_at = member.relay_token_expires_at
    if token and expires_at is not None and expires_at <= utcnow() + timedelta(minutes=5):
        token = _refresh_member_token(db, member, base) or token
    if not str(member.relay_api_key or "").strip() and token:
        key = _create_or_find_key(base, token, group_id_of(db), member.email)
        member.relay_api_key = key
        db.flush()
    key = str(member.relay_api_key or "").strip()
    if not key:
        raise DomainError(400, "这个成员还没有识别密钥。请让该成员先登录一次后台，或由管理员重新开通账号。")
    return key, token


def relay_mode() -> str:
    """只看环境变量，不构造 HTTP 客户端。页面刷新对账结果时用。"""
    return "remote" if get_settings().relay_admin_key else "local"


def get_relay(db: Session) -> LocalRelay | HttpRelay:
    settings = get_settings()
    if settings.relay_admin_key:
        return HttpRelay(settings, group_id_of(db), base_url_of(db))
    return LocalRelay(db, group_name_of(db))