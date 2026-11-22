"""新增用户自动识别中转站账号。全部用假的中转站客户端，不发真实请求。"""

import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from admin_app.errors import DomainError
from admin_app.relay import LOOKUP_FAILED
from admin_app.service import SAVE_FAILED

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admin import _client, _login  # noqa: E402


_SENTINELS = ("sentinel-api-key", "sentinel-access", "sentinel-refresh")
_PASSWORD = "Newpass123"


class MemoryRelay:
    """只在内存里模仿中转站。密码不保存，避免测试失败时把口令打出来。"""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.creates: list[str] = []
        self.lookups = 0
        self.fail_lookup = False
        self.fail_after_create = False

    def find_user_by_email(self, email: str) -> dict | None:
        self.lookups += 1
        if self.fail_lookup:
            raise RuntimeError("relay lookup down")
        return self.users.get(email.strip().lower())

    def create_user(self, email: str, password: str) -> tuple[str, dict]:
        key = email.strip().lower()
        self.creates.append(key)
        if len(password or "") < 8:
            raise DomainError(400, "密码需要在 8 到 72 位之间")
        user_id = f"user_{7000 + len(self.users)}"
        self.users[key] = {"id": user_id, "email": key, "status": "active"}
        if self.fail_after_create:
            raise DomainError(502, "建号失败，请稍后再试")
        return user_id, {
            "api_key": _SENTINELS[0],
            "access_token": _SENTINELS[1],
            "refresh_token": _SENTINELS[2],
        }

    def user_snapshot(self, relay_user_id: str) -> None:
        del relay_user_id
        return None


def _install(monkeypatch) -> MemoryRelay:
    relay = MemoryRelay()
    monkeypatch.setattr("admin_app.service.get_relay", lambda db: relay)
    return relay


def _no_secrets(text: str) -> None:
    leaked = [item for item in (*_SENTINELS, _PASSWORD) if item in text]
    assert leaked == []


def _jiecheng(client, headers) -> int:
    rows = client.get("/v1/tenants", headers=headers).json()
    return next(item["id"] for item in rows if item["name"] == "锦程商贸")


def test_existing_relay_user_is_bound_without_create(tmp_path, monkeypatch):
    relay = _install(monkeypatch)
    relay.users["veteran@example.com"] = {"id": "user_42", "email": "veteran@example.com", "status": "active"}
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant_id = _jiecheng(client, platform)
        created = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "Veteran@example.com",
                "name": "老用户",
                "role": "member",
                "tenant_id": tenant_id,
            },
        )
        assert created.status_code == 200, created.text
        _no_secrets(created.text)
        body = created.json()
        assert body["linked_existing"] is True
        assert body["relay_user_id"] == "user_42"
        assert relay.creates == []

        relay.users["boss-old@example.com"] = {
            "id": "user_43",
            "email": "boss-old@example.com",
            "status": "active",
        }
        company = client.post(
            "/v1/tenants",
            headers=platform,
            json={
                "name": "老公司",
                "admin_email": "boss-old@example.com",
                "admin_name": "老管理员",
                "seats": 3,
            },
        )
        assert company.status_code == 200, company.text
        _no_secrets(company.text)
        assert company.json()["linked_existing"] is True
        assert company.json()["admin"]["relay_user_id"] == "user_43"
        assert relay.creates == []

        invited = client.post(
            "/v1/members/invite",
            headers=platform,
            json={"email": "invite-old@example.com", "name": "受邀", "role": "member", "tenant_id": tenant_id},
        )
        assert invited.status_code == 200, invited.text
        relay.users["invite-old@example.com"] = {
            "id": "user_44",
            "email": "invite-old@example.com",
            "status": "active",
        }
        accepted = client.post(
            f"/v1/invites/{invited.json()['invite_token']}/accept",
            json={"password": _PASSWORD, "name": "受邀"},
        )
        assert accepted.status_code == 200, accepted.text
        _no_secrets(accepted.text)
        assert accepted.json()["linked_existing"] is True
        assert accepted.json()["member"]["relay_user_id"] == "user_44"
        assert relay.creates == []


def test_missing_relay_user_is_created_once_then_bound(tmp_path, monkeypatch):
    relay = _install(monkeypatch)
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant_id = _jiecheng(client, platform)
        created = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "fresh@example.com",
                "name": "新人",
                "password": _PASSWORD,
                "role": "member",
                "tenant_id": tenant_id,
                "link_existing": True,
            },
        )
        assert created.status_code == 200, created.text
        _no_secrets(created.text)
        assert created.json()["linked_existing"] is False
        assert created.json()["relay_user_id"]
        assert relay.creates == ["fresh@example.com"]

        company = client.post(
            "/v1/tenants",
            headers=platform,
            json={
                "name": "新公司",
                "admin_email": "boss-new@example.com",
                "admin_name": "新管理员",
                "admin_password": _PASSWORD,
                "seats": 2,
            },
        )
        assert company.status_code == 200, company.text
        _no_secrets(company.text)
        assert company.json()["linked_existing"] is False
        assert relay.creates == ["fresh@example.com", "boss-new@example.com"]

        invited = client.post(
            "/v1/members/invite",
            headers=platform,
            json={"email": "invite-new@example.com", "name": "新邀请", "role": "member", "tenant_id": tenant_id},
        )
        assert invited.status_code == 200, invited.text
        accepted = client.post(
            f"/v1/invites/{invited.json()['invite_token']}/accept",
            json={"password": _PASSWORD, "name": "新邀请"},
        )
        assert accepted.status_code == 200, accepted.text
        _no_secrets(accepted.text)
        assert accepted.json()["linked_existing"] is False
        assert relay.creates == ["fresh@example.com", "boss-new@example.com", "invite-new@example.com"]


def test_lookup_failure_does_not_create_user(tmp_path, monkeypatch):
    relay = _install(monkeypatch)
    relay.fail_lookup = True
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant_id = _jiecheng(client, platform)
        before = client.get("/v1/tenants", headers=platform).json()
        member = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "unknown@example.com",
                "name": "未知",
                "password": _PASSWORD,
                "role": "member",
                "tenant_id": tenant_id,
            },
        )
        assert member.status_code == 502
        assert member.json()["detail"] == LOOKUP_FAILED
        _no_secrets(member.text)
        company = client.post(
            "/v1/tenants",
            headers=platform,
            json={
                "name": "查失败的公司",
                "admin_email": "boss-fail@example.com",
                "admin_name": "失败",
                "admin_password": _PASSWORD,
                "seats": 2,
            },
        )
        assert company.status_code == 502
        assert company.json()["detail"] == LOOKUP_FAILED
        assert client.get("/v1/tenants", headers=platform).json() == before
        invited = client.post(
            "/v1/members/invite",
            headers=platform,
            json={"email": "invite-fail@example.com", "name": "失败邀请", "role": "member", "tenant_id": tenant_id},
        )
        assert invited.status_code == 200, invited.text
        accepted = client.post(
            f"/v1/invites/{invited.json()['invite_token']}/accept",
            json={"password": _PASSWORD, "name": "失败邀请"},
        )
        assert accepted.status_code == 502
        assert accepted.json()["detail"] == LOOKUP_FAILED
        assert relay.creates == []
        people = client.get("/v1/members", headers=platform, params={"tenant_id": tenant_id}).json()
        assert all(item["email"] not in {"unknown@example.com", "invite-fail@example.com"} for item in people)


def test_create_success_but_db_write_failure_retries_without_duplicate(tmp_path, monkeypatch):
    relay = _install(monkeypatch)
    real_commit = Session.commit
    armed = {"on": False}

    def commit(self, *args, **kwargs):
        if armed["on"]:
            armed["on"] = False
            raise RuntimeError("simulated write failure")
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", commit)
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant_id = _jiecheng(client, platform)
        payload = {
            "email": "retry@example.com",
            "name": "重试",
            "password": _PASSWORD,
            "role": "member",
            "tenant_id": tenant_id,
        }
        armed["on"] = True
        failed = client.post("/v1/members", headers=platform, json=payload)
        assert failed.status_code == 500
        assert failed.json()["detail"] == SAVE_FAILED
        _no_secrets(failed.text)
        assert relay.creates == ["retry@example.com"]
        people = client.get("/v1/members", headers=platform, params={"tenant_id": tenant_id}).json()
        assert all(item["email"] != "retry@example.com" for item in people)

        again = client.post("/v1/members", headers=platform, json=payload)
        assert again.status_code == 200, again.text
        _no_secrets(again.text)
        assert again.json()["linked_existing"] is True
        assert again.json()["relay_user_id"] == relay.users["retry@example.com"]["id"]
        assert relay.creates == ["retry@example.com"]

        company_body = {
            "name": "写库失败公司",
            "admin_email": "boss-retry@example.com",
            "admin_name": "重试管理员",
            "admin_password": _PASSWORD,
            "seats": 2,
        }
        armed["on"] = True
        failed_company = client.post("/v1/tenants", headers=platform, json=company_body)
        assert failed_company.status_code == 500
        assert failed_company.json()["detail"] == SAVE_FAILED
        names = {item["name"] for item in client.get("/v1/tenants", headers=platform).json()}
        assert "写库失败公司" not in names
        recovered = client.post("/v1/tenants", headers=platform, json=company_body)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["linked_existing"] is True
        assert relay.creates == ["retry@example.com", "boss-retry@example.com"]

        invited = client.post(
            "/v1/members/invite",
            headers=platform,
            json={"email": "invite-retry@example.com", "name": "重试邀请", "role": "member", "tenant_id": tenant_id},
        )
        assert invited.status_code == 200, invited.text
        token = invited.json()["invite_token"]
        armed["on"] = True
        failed_accept = client.post(f"/v1/invites/{token}/accept", json={"password": _PASSWORD, "name": "重试邀请"})
        assert failed_accept.status_code == 500
        assert failed_accept.json()["detail"] == SAVE_FAILED
        retried = client.post(f"/v1/invites/{token}/accept", json={"password": _PASSWORD, "name": "重试邀请"})
        assert retried.status_code == 200, retried.text
        assert retried.json()["linked_existing"] is True
        assert relay.creates == ["retry@example.com", "boss-retry@example.com", "invite-retry@example.com"]


def test_create_error_after_user_exists_binds_instead_of_duplicating(tmp_path, monkeypatch):
    relay = _install(monkeypatch)
    relay.fail_after_create = True
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant_id = _jiecheng(client, platform)
        created = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "half@example.com",
                "name": "半成品",
                "password": _PASSWORD,
                "role": "member",
                "tenant_id": tenant_id,
            },
        )
        assert created.status_code == 200, created.text
        _no_secrets(created.text)
        assert created.json()["linked_existing"] is True
        assert relay.creates == ["half@example.com"]
        assert created.json()["relay_user_id"] == relay.users["half@example.com"]["id"]


def test_http_relay_lookup_fails_closed_and_hides_password(monkeypatch):
    import admin_app.relay as relay
    from admin_app.config import Settings

    calls: list[tuple[str, str, object]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.mode = "ok"

        def post(self, url, headers=None, json=None, **kwargs):
            del kwargs
            if self.mode == "down":
                raise httpx.ConnectError("down")
            body = dict(json or {})
            body.pop("password", None)
            calls.append(("POST", url, body))
            return httpx.Response(200, json={"code": 0, "data": {"id": 9}})

        def get(self, url, params=None, **kwargs):
            del kwargs
            calls.append(("GET", url, params))
            if self.mode == "down":
                raise httpx.ConnectError("down")
            if self.mode == "status":
                return httpx.Response(503, json={"message": "nope"})
            if self.mode == "missing-route":
                return httpx.Response(404, json={"message": "missing"})
            if self.mode == "bad-json":
                return httpx.Response(200, content=b"not-json")
            if self.mode == "bad-shape":
                return httpx.Response(200, json={"code": 0, "data": {"unexpected": True}})
            if self.mode == "empty":
                return httpx.Response(200, json={"code": 0, "data": {"items": []}})
            if self.mode == "prefix":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": [{"id": 2, "email": "ann@example.com", "status": "active"}]}},
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 2, "email": "ann@example.com", "status": "active"},
                            {"user_id": 8, "email": "Ann.Other@example.com", "status": "disabled"},
                        ]
                    },
                },
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    settings = Settings(
        database_url="sqlite://",
        jwt_secret="test-secret",
        relay_base_url="https://relay.example/api/v1",
        relay_gateway_url="https://relay.example/v1",
        relay_admin_key="restricted-key",
        relay_group_id="9",
        relay_group_name="电商审核专属-bot创建",
        timezone="Asia/Shanghai",
    )
    sender = relay.HttpRelay.__new__(relay.HttpRelay)
    sender.settings = settings
    sender.group_id = "9"
    sender.base_url = settings.relay_base_url.rstrip("/")
    sender.client = FakeClient()

    found = sender.find_user_by_email("ANN.OTHER@example.com")
    assert found == {"id": "8", "email": "ann.other@example.com", "status": "disabled"}
    assert calls[-1][0] == "GET"
    assert calls[-1][2]["search"] == "ann.other@example.com"

    sender.client.mode = "empty"
    assert sender.find_user_by_email("nobody@example.com") is None
    sender.client.mode = "prefix"
    assert sender.find_user_by_email("an@example.com") is None

    for mode in ("status", "missing-route", "bad-json", "bad-shape", "down"):
        sender.client.mode = mode
        with pytest.raises(DomainError) as caught:
            sender.find_user_by_email("nobody@example.com")
        assert caught.value.detail == LOOKUP_FAILED
        assert caught.value.status == 502

    sender.client.mode = "down"
    with pytest.raises(DomainError) as created:
        sender.create_user("staff@example.com", _PASSWORD)
    assert created.value.detail == "建号失败，请稍后再试"
    assert created.value.__cause__ is None
    assert _PASSWORD not in str(created.value)
    posted = [body for method, url, body in calls if method == "POST" and str(url).endswith("/admin/users")]
    assert posted == []
