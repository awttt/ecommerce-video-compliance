"""中转站管理接口。没有管理密钥时走本地账本，不访问网络。"""

import hashlib

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.errors import DomainError
from app.models import RelayAccount, RelayLedger


class LocalRelay:
    def __init__(self, db: Session, group_name: str) -> None:
        self.db = db
        self.group_name = group_name

    def create_user(self, email: str, password: str) -> str:
        del password
        existing = self.db.query(RelayAccount).filter(RelayAccount.email == email).one_or_none()
        if existing:
            return existing.relay_user_id
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
        return relay_user_id

    def _apply(self, relay_user_id: str, cents: int, key: str, kind: str) -> None:
        if self.was_applied(key, relay_user_id):
            return
        account = self.db.get(RelayAccount, relay_user_id) if relay_user_id else None
        if account is None or account.status != "active":
            raise DomainError(400, "中转站账号不存在或已停用")
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
            raise DomainError(400, "中转站账号不存在")
        account.status = status
        self.db.flush()


class HttpRelay:
    def __init__(self, settings: Settings, group_id: str) -> None:
        import httpx

        self.settings = settings
        self.group_id = group_id
        self.client = httpx.Client(
            timeout=20.0,
            headers={
                "x-api-key": settings.relay_admin_key,
                "User-Agent": "evc-admin/0.1.0",
            },
        )

    def _url(self, path: str) -> str:
        return self.settings.relay_base_url.rstrip("/") + path

    def _raise(self, response, action: str) -> None:
        if response.status_code >= 400:
            raise DomainError(502, f"中转站{action}失败（HTTP {response.status_code}）")

    def create_user(self, email: str, password: str) -> str:
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
        self._raise(response, "建号")
        data = response.json()
        user_id = data.get("id") or (data.get("data") or {}).get("id")
        if not user_id:
            raise DomainError(502, "中转站建号没有返回用户编号")
        self._create_default_key(email, password)
        return str(user_id)

    def _create_default_key(self, email: str, password: str) -> None:
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
                raise DomainError(502, "中转站没有返回新账号登录凭证")
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

    def _configured_group_id(self) -> int:
        try:
            group_id = int(self.group_id)
        except (TypeError, ValueError) as exc:
            raise DomainError(400, "请先在接入设置填写专属分组数字 ID") from exc
        if group_id <= 0:
            raise DomainError(400, "专属分组 ID 需要是正整数")
        return group_id

    def add_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        response = self.client.post(
            self._url(f"/admin/users/{relay_user_id}/balance"),
            headers={"Idempotency-Key": key},
            json={"balance": round(cents / 100, 2), "operation": "add"},
        )
        self._raise(response, "加款")

    def subtract_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        response = self.client.post(
            self._url(f"/admin/users/{relay_user_id}/balance"),
            headers={"Idempotency-Key": key},
            json={"balance": round(cents / 100, 2), "operation": "subtract"},
        )
        self._raise(response, "扣款")

    def was_applied(self, key: str, relay_user_id: str = "") -> bool:
        if not relay_user_id:
            return False
        response = self.client.get(self._url(f"/admin/users/{relay_user_id}/balance-history"))
        if response.status_code >= 400:
            return False
        return key in response.text

    def set_status(self, relay_user_id: str, status: str) -> None:
        response = self.client.put(
            self._url(f"/admin/users/{relay_user_id}"),
            json={"status": status},
        )
        self._raise(response, "更新账号状态")


def group_name_of(db: Session) -> str:
    from app.models import AppSetting

    row = db.get(AppSetting, 1)
    if row and row.group_name:
        return row.group_name
    return get_settings().relay_group_name


def group_id_of(db: Session) -> str:
    from app.models import AppSetting

    row = db.get(AppSetting, 1)
    if row and row.group_id:
        return row.group_id
    return get_settings().relay_group_id


def get_relay(db: Session) -> LocalRelay | HttpRelay:
    settings = get_settings()
    if settings.relay_admin_key:
        return HttpRelay(settings, group_id_of(db))
    return LocalRelay(db, group_name_of(db))
