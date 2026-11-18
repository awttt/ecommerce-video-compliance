"""中转站管理接口。没有管理密钥时走本地账本，不访问网络。"""

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
                "Authorization": f"Bearer {settings.relay_admin_key}",
                "User-Agent": "evc-admin/0.1.0",
            },
        )

    def _url(self, path: str) -> str:
        return self.settings.relay_base_url.rstrip("/") + path

    def _raise(self, response, action: str) -> None:
        if response.status_code >= 400:
            raise DomainError(502, f"中转站{action}失败（HTTP {response.status_code}）")

    def create_user(self, email: str, password: str) -> str:
        if not self.group_id:
            raise DomainError(400, "请先在接入设置填写专属分组 ID")
        group: object = int(self.group_id) if self.group_id.isdigit() else self.group_id
        response = self.client.post(
            self._url("/admin/users"),
            json={"email": email, "password": password, "allowed_groups": [group], "balance": 0},
        )
        self._raise(response, "建号")
        data = response.json()
        user_id = data.get("id") or (data.get("data") or {}).get("id")
        if not user_id:
            raise DomainError(502, "中转站建号没有返回用户编号")
        return str(user_id)

    def add_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        response = self.client.post(
            self._url(f"/admin/users/{relay_user_id}/balance"),
            json={"amount": round(cents / 100, 2), "operation": "add", "idempotency_key": key},
        )
        self._raise(response, "加款")

    def subtract_balance(self, relay_user_id: str, cents: int, key: str) -> None:
        response = self.client.post(
            self._url(f"/admin/users/{relay_user_id}/balance"),
            json={"amount": round(cents / 100, 2), "operation": "subtract", "idempotency_key": key},
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
