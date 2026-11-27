"""新建账号同时授予审核组和生图专组；老账号缺分组时只追加，不改其他字段。"""

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_image_workbench import _client, _groups, _install_transport, _login, _member  # noqa: E402

from admin_app.errors import DomainError
from admin_app.image_relay import image_group_forbidden, set_transport
from admin_app.relay import (
    HttpRelay,
    read_allowed_groups,
    safe_relay_user_id,
    sorted_unique_group_ids,
)

REVIEW_MARKER = "review-marker"
IMAGE_MARKER = "image-marker"
LEAK_MARKER = "leak-marker"
TOKEN_MARKER = "relay-access-marker"


def test_group_lists_are_stable_and_reject_partial_reads():
    assert sorted_unique_group_ids([16, 15, 15]) == [15, 16]
    assert read_allowed_groups([15, "9", {"id": 15}]) == [9, 15]
    assert read_allowed_groups([]) == []
    assert read_allowed_groups([15, {"name": "审核组"}]) is None
    assert read_allowed_groups(None) is None
    assert read_allowed_groups([True, 15]) is None
    assert safe_relay_user_id("user_1042") == "user_1042"
    assert safe_relay_user_id("1/replace-group") == ""
    assert safe_relay_user_id("../replace-group") == ""


def test_image_group_forbidden_is_only_permission_failure():
    assert image_group_forbidden(httpx.Response(400, json={"reason": "GROUP_NOT_ALLOWED"}))
    assert image_group_forbidden(httpx.Response(403, json={"message": "permission"}))
    assert image_group_forbidden(httpx.Response(403, json={"data": {"reason": "GROUP_NOT_ALLOWED"}}))
    assert not image_group_forbidden(httpx.Response(401, json={"reason": "TOKEN_EXPIRED"}))
    assert not image_group_forbidden(httpx.Response(500, json={"message": LEAK_MARKER}))


class _Recorded:
    sink: list | None = None

    def __init__(self, user: dict | None = None, *args, put_status: int = 200, get_status: int = 200, **kwargs) -> None:
        del args, kwargs
        self.user = user or {}
        self.put_status = put_status
        self.get_status = get_status
        self.calls = _Recorded.sink if _Recorded.sink is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, **kwargs):
        del kwargs
        self.calls.append(("GET", url, params, None))
        if self.get_status >= 400:
            return httpx.Response(self.get_status, json={"message": LEAK_MARKER})
        if params and "search" in (params or {}):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{"id": "user_1042", "email": params["search"], "status": "active"}]}},
            )
        return httpx.Response(200, json={"code": 0, "data": self.user})

    def put(self, url, json=None, headers=None, **kwargs):
        del headers, kwargs
        self.calls.append(("PUT", url, None, json))
        return httpx.Response(self.put_status, json={"message": LEAK_MARKER, "code": 0, "data": {}})

    def post(self, url, headers=None, json=None, **kwargs):
        del headers, kwargs
        self.calls.append(("POST", url, None, json))
        if str(url).endswith("/admin/users"):
            return httpx.Response(200, json={"code": 0, "data": {"id": 77}})
        if str(url).endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "user-token"}})
        if str(url).endswith("/keys"):
            return httpx.Response(200, json={"code": 0, "data": {"key": REVIEW_MARKER}})
        raise AssertionError(url)


def _sender(client: _Recorded, group_id: str = "15") -> HttpRelay:
    sender = HttpRelay.__new__(HttpRelay)
    sender.client = client
    sender.base_url = "https://token4u.cc/api/v1"
    sender.group_id = group_id
    sender.settings = None
    return sender


def _user_posts(calls: list[tuple]) -> list[dict]:
    return [body for method, url, _, body in calls if method == "POST" and str(url).endswith("/admin/users")]


def test_new_user_gets_both_groups_and_keeps_the_review_key(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _Recorded)
    monkeypatch.delenv("EVC_ADMIN_IMAGE_GROUP_ID", raising=False)
    _Recorded.sink = []
    try:
        _exercise_new_user(monkeypatch)
    finally:
        _Recorded.sink = None


def _exercise_new_user(monkeypatch) -> None:
    from admin_app.config import Settings

    recorded = _Recorded()
    sender = _sender(recorded, "15")
    sender.settings = Settings(
        database_url="sqlite://",
        jwt_secret="test-secret",
        relay_base_url="https://token4u.cc/api/v1",
        relay_gateway_url="https://token4u.cc/v1",
        relay_admin_key="admin-marker",
        relay_group_id="15",
        relay_group_name="审核专属",
        timezone="Asia/Shanghai",
    )
    user_id, credentials = sender.create_user("staff@example.com", "password123")
    assert user_id == "77"
    assert credentials["api_key"] == REVIEW_MARKER
    body = _user_posts(recorded.calls)[0]
    assert body["allowed_groups"] == [15, 16]
    assert body["restrict_public_groups"] is True
    assert "balance" not in body
    assert "role" not in body
    assert "rate_multiplier" not in body
    key_body = next(item[3] for item in recorded.calls if item[0] == "POST" and str(item[1]).endswith("/keys"))
    assert key_body == {"name": "电商视频合规", "group_id": 15}
    assert all("replace-group" not in str(item[1]) for item in recorded.calls)

    monkeypatch.setenv("EVC_ADMIN_IMAGE_GROUP_ID", "9")
    recorded.calls.clear()
    sender.group_id = "15"
    sender.create_user("other@example.com", "password123")
    assert _user_posts(recorded.calls)[0]["allowed_groups"] == [9, 15]

    monkeypatch.setenv("EVC_ADMIN_IMAGE_GROUP_ID", "15")
    recorded.calls.clear()
    sender.create_user("same@example.com", "password123")
    assert _user_posts(recorded.calls)[0]["allowed_groups"] == [15]
    assert _user_posts(recorded.calls)[0]["restrict_public_groups"] is True

    monkeypatch.setenv("EVC_ADMIN_IMAGE_GROUP_ID", "abc")
    recorded.calls.clear()
    with pytest.raises(DomainError) as caught:
        sender.create_user("bad@example.com", "password123")
    assert caught.value.detail == "服务配置不完整，请联系平台管理员"
    assert recorded.calls == []
    assert "password123" not in str(caught.value)


def test_append_preserves_original_groups_and_skips_known_ones():
    user = {
        "id": "user_1042",
        "allowed_groups": [15, {"id": 9}, 15],
        "restrict_public_groups": False,
        "balance": 12.5,
        "role": "user",
        "rate_multiplier": 1.8,
        "status": "active",
    }
    recorded = _Recorded(user)
    sender = _sender(recorded)
    assert sender.append_image_group("user_1042", 16) is True
    puts = [item for item in recorded.calls if item[0] == "PUT"]
    assert len(puts) == 1
    method, url, _, body = puts[0]
    assert method == "PUT"
    assert url == "https://token4u.cc/api/v1/admin/users/user_1042"
    assert "replace-group" not in url
    assert set(body) == {"allowed_groups", "restrict_public_groups"}
    assert body["allowed_groups"] == [9, 15, 16]
    assert body["restrict_public_groups"] is False
    assert 15 in body["allowed_groups"] and 9 in body["allowed_groups"]

    recorded.calls.clear()
    recorded.user = {**user, "allowed_groups": [16, 15]}
    assert sender.append_image_group("user_1042", 16) is True
    assert [item for item in recorded.calls if item[0] == "PUT"] == []

    recorded.user = {**user, "allowed_groups": [15, "nope"]}
    assert sender.append_image_group("user_1042", 16) is False
    recorded.user = {"allowed_groups": [15]}
    assert sender.append_image_group("user_1042", 16) is False
    assert sender.append_image_group("1/replace-group", 16) is False
    assert [item for item in recorded.calls if item[0] == "PUT"] == []
    assert all("replace-group" not in str(item[1]) for item in recorded.calls)


def test_open_paths_share_create_user():
    source = (Path(__file__).resolve().parents[1] / "admin_app" / "service.py").read_text(encoding="utf-8")
    assert source.count("relay.create_user(") == 1
    for name in ("create_tenant", "create_member", "accept_invite"):
        chunk = source.split(f"def {name}", 1)[1].split("\ndef ", 1)[0]
        assert "_relay_account(" in chunk


def _bind_admin(monkeypatch, user: dict, *, put_status: int = 200) -> _Recorded:
    recorded = _Recorded(user, put_status=put_status)
    import admin_app.relay as relay

    def get_relay(db):
        del db
        return _sender(recorded)

    monkeypatch.setattr(relay, "get_relay", get_relay)
    return recorded


def _key_transport(posts: dict, items: list[dict], first_status: int = 403, reason: str = "GROUP_NOT_ALLOWED"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") in (None, "")
        assert "admin-marker" not in request.headers.get("authorization", "")
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"items": list(items)}})
        posts["n"] += 1
        body = json.loads(request.content.decode())
        assert body["name"] == "生图专用"
        assert body["group_id"] == 16
        posts["idempotency"].append(request.headers.get("idempotency-key"))
        if posts["n"] == 1 and first_status >= 400:
            return httpx.Response(first_status, json={"reason": reason, "message": LEAK_MARKER, "key": LEAK_MARKER})
        items.append({"id": 1, "name": "生图专用", "group_id": 16, "key": IMAGE_MARKER})
        return httpx.Response(200, json={"code": 0, "data": {"key": IMAGE_MARKER}})

    return handler


def _prepare_review_key() -> None:
    member, db = _member("zhang@jiecheng.com")
    member.relay_api_key = REVIEW_MARKER
    db.commit()
    db.close()


def _stored_keys() -> tuple[str, str]:
    member, db = _member("zhang@jiecheng.com")
    pair = (member.relay_api_key, member.relay_image_api_key)
    db.close()
    return pair


def test_old_account_group_denied_appends_then_reuses_image_key(tmp_path, monkeypatch, caplog):
    user = {
        "id": "user_1042",
        "allowed_groups": [15, 9, 15],
        "restrict_public_groups": False,
        "balance": 12.5,
        "role": "user",
        "rate_multiplier": 1.8,
    }
    with _client(tmp_path, monkeypatch, admin_key="admin-marker") as client:
        recorded = _bind_admin(monkeypatch, user)
        posts = {"n": 0, "idempotency": []}
        items: list[dict] = []
        seen = _install_transport(_key_transport(posts, items))
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            _prepare_review_key()
            with caplog.at_level(logging.DEBUG):
                status = client.get("/v1/image-workbench/status", headers=headers)
            assert status.status_code == 200, status.text
            body = status.json()
            assert body["key_ready"] is True
            assert body["group_ready"] is True
            assert body["live_enabled"] is False
            assert LEAK_MARKER not in status.text
            assert IMAGE_MARKER not in status.text
            assert REVIEW_MARKER not in status.text
            assert TOKEN_MARKER not in status.text
            assert LEAK_MARKER not in caplog.text
            assert IMAGE_MARKER not in caplog.text
            puts = [item for item in recorded.calls if item[0] == "PUT"]
            assert len(puts) == 1
            assert puts[0][1] == "https://token4u.cc/api/v1/admin/users/user_1042"
            assert "replace-group" not in puts[0][1]
            assert set(puts[0][3]) == {"allowed_groups", "restrict_public_groups"}
            assert puts[0][3]["allowed_groups"] == [9, 15, 16]
            assert puts[0][3]["restrict_public_groups"] is False
            assert posts["n"] == 2
            assert posts["idempotency"][0] == posts["idempotency"][1]
            assert _stored_keys() == (REVIEW_MARKER, IMAGE_MARKER)
            again = client.get("/v1/image-workbench/status", headers=headers)
            assert again.status_code == 200
            assert posts["n"] == 2
            assert len([item for item in recorded.calls if item[0] == "PUT"]) == 1
            assert all("replace-group" not in str(item[1]) for item in recorded.calls)
            assert all(item[0] != "POST" or not str(item[1]).endswith("/keys") for item in recorded.calls)
            del seen
        finally:
            set_transport(None)


def test_existing_image_group_does_not_put_and_still_retries_the_key(tmp_path, monkeypatch):
    user = {"allowed_groups": [15, 16], "restrict_public_groups": True, "balance": 3, "role": "user"}
    with _client(tmp_path, monkeypatch, admin_key="admin-marker") as client:
        recorded = _bind_admin(monkeypatch, user)
        posts = {"n": 0, "idempotency": []}
        items: list[dict] = []
        _install_transport(_key_transport(posts, items))
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            _prepare_review_key()
            status = client.get("/v1/image-workbench/status", headers=headers)
            assert status.status_code == 200, status.text
            assert status.json()["key_ready"] is True
            assert [item for item in recorded.calls if item[0] == "PUT"] == []
            assert any(item[0] == "GET" and str(item[1]).endswith("/admin/users/user_1042") for item in recorded.calls)
            assert posts["n"] == 2
            assert _stored_keys() == (REVIEW_MARKER, IMAGE_MARKER)
        finally:
            set_transport(None)


@pytest.mark.parametrize("status,reason", [(500, "UPSTREAM"), (401, "TOKEN_EXPIRED")])
def test_non_permission_key_errors_do_not_touch_groups(tmp_path, monkeypatch, caplog, status, reason):
    with _client(tmp_path, monkeypatch, admin_key="admin-marker") as client:
        def explode(db):
            del db
            raise AssertionError("non-permission errors must not read or update groups")

        monkeypatch.setattr("admin_app.relay.get_relay", explode)
        posts = {"n": 0, "idempotency": []}
        _install_transport(_key_transport(posts, [], first_status=status, reason=reason))
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            _prepare_review_key()
            with caplog.at_level(logging.DEBUG):
                result = client.get("/v1/image-workbench/status", headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["key_ready"] is False
            assert LEAK_MARKER not in result.text
            assert LEAK_MARKER not in caplog.text
            assert posts["n"] == 1
            assert _stored_keys() == (REVIEW_MARKER, "")
        finally:
            set_transport(None)


def test_missing_admin_key_does_not_append_groups(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        def explode(db):
            del db
            raise AssertionError("missing admin key must not call the relay admin api")

        monkeypatch.setattr("admin_app.relay.get_relay", explode)
        posts = {"n": 0, "idempotency": []}
        _install_transport(_key_transport(posts, []))
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            _prepare_review_key()
            result = client.get("/v1/image-workbench/status", headers=headers)
            assert result.status_code == 200, result.text
            assert "还不能绑定生图分组" in result.json()["group_reason"]
            assert "审核功能不受影响" in result.json()["group_reason"]
            assert posts["n"] == 1
            assert _stored_keys() == (REVIEW_MARKER, "")
            assert LEAK_MARKER not in result.text
        finally:
            set_transport(None)


def test_failed_group_update_does_not_retry_or_leak(tmp_path, monkeypatch, caplog):
    user = {"allowed_groups": [15], "restrict_public_groups": True, "balance": 1, "role": "admin"}
    with _client(tmp_path, monkeypatch, admin_key="admin-marker") as client:
        recorded = _bind_admin(monkeypatch, user, put_status=403)
        posts = {"n": 0, "idempotency": []}
        _install_transport(_key_transport(posts, []))
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            _prepare_review_key()
            with caplog.at_level(logging.DEBUG):
                result = client.get("/v1/image-workbench/status", headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["key_ready"] is False
            assert posts["n"] == 1
            assert len([item for item in recorded.calls if item[0] == "PUT"]) == 1
            assert LEAK_MARKER not in result.text
            assert LEAK_MARKER not in caplog.text
            assert _stored_keys()[0] == REVIEW_MARKER
        finally:
            set_transport(None)


def test_image_group_endpoint_appends_for_members_only(tmp_path, monkeypatch, caplog):
    user = {
        "allowed_groups": [15, 3],
        "restrict_public_groups": True,
        "balance": 8,
        "role": "user",
        "rate_multiplier": 1.8,
    }
    with _client(tmp_path, monkeypatch, admin_key="admin-marker") as client:
        recorded = _bind_admin(monkeypatch, user)
        import admin_app.auth_relay as auth_relay

        def current_account(token: str) -> dict:
            assert token == TOKEN_MARKER
            return {"email": "zhang@jiecheng.com", "id": "user_9999"}

        monkeypatch.setattr(auth_relay, "current_account", current_account)
        _login(client, "zhang@jiecheng.com")
        with caplog.at_level(logging.DEBUG):
            ok = client.post("/v1/account/image-group", json={"access_token": TOKEN_MARKER})
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"ok": True}
        assert TOKEN_MARKER not in ok.text
        assert LEAK_MARKER not in ok.text
        assert TOKEN_MARKER not in caplog.text
        puts = [item for item in recorded.calls if item[0] == "PUT"]
        assert len(puts) == 1
        assert puts[0][1].endswith("/admin/users/user_1042")
        assert puts[0][3]["allowed_groups"] == [3, 15, 16]
        assert puts[0][3]["restrict_public_groups"] is True
        assert "balance" not in puts[0][3]
        assert "role" not in puts[0][3]
        assert "rate_multiplier" not in puts[0][3]
        assert all("replace-group" not in str(item[1]) for item in recorded.calls)

        def stranger(token: str) -> dict:
            del token
            return {"email": "nobody@example.com", "id": "user_1"}

        monkeypatch.setattr(auth_relay, "current_account", stranger)
        before = len(recorded.calls)
        denied = client.post("/v1/account/image-group", json={"access_token": TOKEN_MARKER})
        assert denied.status_code == 403
        assert len(recorded.calls) == before

        def disabled(token: str) -> dict:
            del token
            return {"email": "zhao@jiecheng.com"}

        monkeypatch.setattr(auth_relay, "current_account", disabled)
        blocked = client.post("/v1/account/image-group", json={"access_token": TOKEN_MARKER})
        assert blocked.status_code == 403
        assert len(recorded.calls) == before
        assert TOKEN_MARKER not in blocked.text


def test_image_group_endpoint_degrades_without_admin_key_and_stores_looked_up_id(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        def explode(db):
            del db
            raise AssertionError("missing admin key must not call the relay admin api")

        monkeypatch.setattr("admin_app.relay.get_relay", explode)
        import admin_app.auth_relay as auth_relay

        monkeypatch.setattr(auth_relay, "current_account", lambda token: {"email": "zhang@jiecheng.com", "id": "user_1042"})
        _login(client, "zhang@jiecheng.com")
        missed = client.post("/v1/account/image-group", json={"access_token": TOKEN_MARKER})
        assert missed.status_code == 200, missed.text
        assert missed.json() == {"ok": False}
        assert TOKEN_MARKER not in missed.text
        assert client.post("/v1/account/image-group", json={"access_token": "  "}).status_code == 401

    with _client(tmp_path, monkeypatch, admin_key="admin-marker") as client:
        user = {"allowed_groups": [15], "restrict_public_groups": False, "balance": 1, "role": "user"}
        recorded = _bind_admin(monkeypatch, user)
        import admin_app.auth_relay as auth_relay

        monkeypatch.setattr(auth_relay, "current_account", lambda token: {"email": "zhang@jiecheng.com"})
        _login(client, "zhang@jiecheng.com")
        member, db = _member("zhang@jiecheng.com")
        member.relay_user_id = ""
        db.commit()
        db.close()
        found = client.post("/v1/account/image-group", json={"access_token": TOKEN_MARKER})
        assert found.status_code == 200, found.text
        assert found.json() == {"ok": True}
        assert any(item[0] == "GET" and item[2] and item[2].get("search") == "zhang@jiecheng.com" for item in recorded.calls)
        puts = [item for item in recorded.calls if item[0] == "PUT"]
        assert puts[0][1].endswith("/admin/users/user_1042")
        assert puts[0][3]["restrict_public_groups"] is False
        member, db = _member("zhang@jiecheng.com")
        assert member.relay_user_id == "user_1042"
        assert member.relay_api_key == ""
        db.close()
