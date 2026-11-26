"""管理端生图工作台。不访问真实网关，不使用管理密钥，也不改审核钥。"""

import base64
import hashlib
import json
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admin import _fake_login, _login  # noqa: E402

from admin_app.errors import DomainError
from admin_app.image_relay import (
    IMAGE_GENERATIONS_URL,
    build_generation_payload,
    image_group_name,
    resolve_image_group,
    set_transport,
)
from admin_app.seed import DEMO_PASSWORD

FLARE = "gpt-image-2.5-flare"
GROUP_NAME = "生图专用-bot创建"
_CREATED_IMAGE_KEYS: list[dict] = []
IMAGE_KEY = "sk-image-user-only"
REVIEW_KEY = "review-key-do-not-touch"
ADMIN_KEY = "sk-admin-do-not-use"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _client(tmp_path, monkeypatch, *, admin_key: str = ""):
    monkeypatch.setenv("EVC_ADMIN_DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    monkeypatch.setenv("EVC_ADMIN_JWT_SECRET", "test-secret-test-secret")
    monkeypatch.setenv("EVC_RELAY_BASE_URL", "https://token4u.cc/api/v1")
    monkeypatch.setenv("EVC_RELAY_GATEWAY_URL", "https://token4u.cc/v1")
    for name in (
        "EVC_ADMIN_IMAGE_LIVE",
        "EVC_ADMIN_IMAGE_CONTRACT_OK",
        "EVC_ADMIN_IMAGE_ALLOW_UNVERIFIED_SIZES",
        "EVC_ADMIN_IMAGE_ALLOW_UNVERIFIED_QUALITY",
        "EVC_ADMIN_IMAGE_GROUP_NAME",
        "EVC_ADMIN_IMAGE_GROUP_ID",
        "EVC_ADMIN_IMAGE_MODELS",
    ):
        monkeypatch.delenv(name, raising=False)
    if admin_key:
        monkeypatch.setenv("EVC_RELAY_ADMIN_KEY", admin_key)
    else:
        monkeypatch.delenv("EVC_RELAY_ADMIN_KEY", raising=False)
    from admin_app.config import get_settings
    from admin_app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    _CREATED_IMAGE_KEYS.clear()
    import admin_app.auth_relay as auth_relay

    monkeypatch.setattr(auth_relay, "login_password", _fake_login)
    from admin_app.main import create_app

    set_transport(None)
    auth_relay._transport = None
    return TestClient(create_app())


def _groups(monkeypatch, rows: list[dict] | None = None, *, token: str = "relay-test-access"):
    import admin_app.auth_relay as auth_relay

    payload = rows if rows is not None else [{"id": 42, "name": GROUP_NAME, "status": "active"}]

    def available_groups(access_token: str) -> list[dict]:
        assert access_token == token
        assert access_token != ADMIN_KEY
        return payload

    monkeypatch.setattr(auth_relay, "available_groups", available_groups)


def _install_transport(handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        joined = " ".join(
            [
                request.method,
                str(request.url),
                request.headers.get("authorization", ""),
                request.headers.get("x-api-key", ""),
            ]
        )
        assert ADMIN_KEY not in joined
        assert "chuanapi" not in str(request.url)
        return handler(request)

    set_transport(httpx.MockTransport(wrapped))
    return seen


def _key_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("authorization") == "Bearer relay-test-access"
    assert request.url.path == "/api/v1/keys"
    if request.method == "GET":
        if not _CREATED_IMAGE_KEYS:
            return httpx.Response(404, json={"code": 404})
        return httpx.Response(200, json={"code": 0, "data": {"items": list(_CREATED_IMAGE_KEYS)}})
    body = json.loads(request.content.decode())
    assert body == {"name": "生图专用", "group_id": 16}
    expected = hashlib.sha256(b"evc-admin-image-key:zhang@jiecheng.com:16").hexdigest()[:32]
    assert request.headers.get("idempotency-key") == expected
    _CREATED_IMAGE_KEYS.append({"id": 1, "name": "生图专用", "group_id": 16, "key": IMAGE_KEY})
    return httpx.Response(200, json={"code": 0, "data": {"key": IMAGE_KEY}})


def _body(**overrides) -> dict:
    payload = {
        "prompt": "红色礼盒 白底",
        "model": FLARE,
        "n": 1,
        "size": "1024x1024",
        "quality": "auto",
        "confirm": True,
    }
    payload.update(overrides)
    return payload


def _member(email: str):
    from admin_app.db import session
    from admin_app.models import Member

    db = session()
    try:
        return db.query(Member).filter(Member.email == email).one(), db
    except Exception:
        db.close()
        raise


def test_group_name_match_never_uses_a_fixed_id():
    with __import__("pytest").raises(DomainError) as missing:
        resolve_image_group(
            [
                {"id": 16, "name": "普通GPT", "status": "active"},
                {"id": 15, "name": "电商审核专属-bot创建", "status": "active"},
            ],
            name=GROUP_NAME,
        )
    assert missing.value.status == 403
    assert "不会改用审核分组" in missing.value.detail

    disabled = [{"id": 16, "name": GROUP_NAME, "status": "disabled"}]
    with __import__("pytest").raises(DomainError) as inactive:
        resolve_image_group(disabled, name=GROUP_NAME)
    assert inactive.value.status == 403

    group = resolve_image_group([{"id": 77, "name": GROUP_NAME, "status": "active"}], name=GROUP_NAME)
    assert group["id"] == 77

    with __import__("pytest").raises(DomainError) as many:
        resolve_image_group(
            [
                {"id": 3, "name": GROUP_NAME, "status": "active"},
                {"id": 9, "name": GROUP_NAME, "status": "active"},
            ],
            name=GROUP_NAME,
        )
    assert many.value.status == 502


def test_available_groups_preserves_list_payload(tmp_path, monkeypatch):
    import admin_app.auth_relay as auth_relay
    from admin_app.config import get_settings

    monkeypatch.setenv("EVC_RELAY_BASE_URL", "https://token4u.cc/api/v1")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/groups/available"
        assert request.headers.get("authorization") == "Bearer user-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {"id": 15, "name": "电商审核专属-bot创建", "status": "active"},
                    {"id": 77, "name": GROUP_NAME, "status": "active"},
                ],
            },
        )

    auth_relay._transport = httpx.MockTransport(handler)
    try:
        rows = auth_relay.available_groups("user-token")
    finally:
        auth_relay._transport = None
    assert [row["name"] for row in rows] == ["电商审核专属-bot创建", GROUP_NAME]

    def wrapped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"groups": [{"id": 8, "name": GROUP_NAME}]}})

    auth_relay._transport = httpx.MockTransport(wrapped)
    try:
        packed = auth_relay.available_groups("user-token")
    finally:
        auth_relay._transport = None
    assert packed == [{"id": 8, "name": GROUP_NAME}]


def test_image_group_name_ignores_review_group_setting(monkeypatch):
    monkeypatch.setenv("EVC_RELAY_GROUP_NAME", "电商审核专属-bot创建")
    monkeypatch.delenv("EVC_ADMIN_IMAGE_GROUP_NAME", raising=False)
    assert image_group_name() == GROUP_NAME
    monkeypatch.setenv("EVC_ADMIN_IMAGE_GROUP_NAME", "生图专用-bot创建")
    assert image_group_name() == GROUP_NAME


def test_payload_omits_auto_size_and_quality():
    payload = build_generation_payload(model=FLARE, prompt="红色礼盒", n=1, size="2048x1152", quality="auto")
    assert payload == {"model": FLARE, "prompt": "红色礼盒", "n": 1, "size": "2048x1152"}
    assert "quality" not in payload
    assert payload["size"] != "2048x2048"


def test_status_requires_login_and_hides_secrets(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/v1/image-workbench/status").status_code == 401
        seen = _install_transport(_key_handler)
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            status = client.get("/v1/image-workbench/status", headers=headers)
            assert status.status_code == 200, status.text
            body = status.json()
            assert body["group_ready"] is True
            assert body["key_ready"] is True
            assert body["group_name"] == GROUP_NAME
            assert "group_id" not in body
            assert body["mock"] is True
            assert body["live_enabled"] is False
            assert body["endpoint"] == {
                "method": "POST",
                "url": IMAGE_GENERATIONS_URL,
                "editable": False,
            }
            assert body["references"]["enabled"] is False
            assert body["async_generation"]["enabled"] is False
            assert body["pricing"]["price_per_image"] == 0.1
            assert "0.04" not in json.dumps(body["pricing"])
            sizes = {item["value"]: item for item in body["sizes"]}
            assert sizes["2048x1152"]["verified"] is True
            assert sizes["2048x1152"]["verified_for"] == [FLARE]
            assert "上游已验证" in sizes["2048x1152"]["note"]
            assert "本站网关待验" in sizes["2048x1152"]["note"]
            assert sizes["3840x2160"]["verified_for"] == [FLARE]
            assert sizes["1024x1024"]["verified"] is False
            assert sizes["1024x1024"]["gateway_pending"] is True
            assert sizes["1024x1024"]["enabled"] is True
            for blocked in ("2560x1440", "2048x2048", "2160x3840"):
                assert sizes[blocked]["verified"] is False
                assert sizes[blocked]["enabled"] is False
            assert "已支持" not in json.dumps(body["sizes"])
            models = {item["id"]: item for item in body["models"]}
            assert models[FLARE]["selectable"] is True
            assert models[FLARE]["ready"] is False
            assert models[FLARE]["upstream_verified"] is True
            assert models["gpt-image-2.5-sunburst"]["selectable"] is False
            assert models["gpt-image-2.5-sunburst"]["ready"] is False
            me = client.get("/v1/auth/me", headers=headers).json()
            assert "image.workbench" in me["permissions"]
            assert IMAGE_KEY not in status.text
            assert REVIEW_KEY not in status.text
            assert any(request.method == "POST" for request in seen)
        finally:
            set_transport(None)


def test_missing_group_name_still_creates_the_configured_key(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        seen = _install_transport(_key_handler)
        try:
            _groups(
                monkeypatch,
                [
                    {"id": 15, "name": "电商审核专属-bot创建", "status": "active"},
                    {"id": 9, "name": "普通GPT", "status": "active"},
                ],
            )
            headers = _login(client, "zhang@jiecheng.com")
            status = client.get("/v1/image-workbench/status", headers=headers)
            assert status.status_code == 200, status.text
            assert status.json()["group_ready"] is True
            created = [request for request in seen if request.method == "POST"]
            assert len(created) == 1
            assert json.loads(created[0].content.decode()) == {"name": "生图专用", "group_id": 16}
            assert "电商视频合规" not in created[0].content.decode()
            again = client.get("/v1/image-workbench/status", headers=headers)
            assert again.status_code == 200
            assert len([request for request in seen if request.method == "POST"]) == 1
        finally:
            set_transport(None)


def test_mock_generation_keeps_review_key_and_isolation(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        seen = _install_transport(_key_handler)
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            member, db = _member("zhang@jiecheng.com")
            before = member.balance_cents
            member.relay_api_key = REVIEW_KEY
            db.commit()
            db.close()

            created = client.post(
                "/v1/image-workbench/generate",
                headers=headers,
                json=_body(prompt="红色礼盒 <script>", size="2048x1152", n=1),
            )
            assert created.status_code == 200, created.text
            job = created.json()
            assert job["mock"] is True
            assert job["est_cost_cents"] == 10
            assert job["message"].startswith("预览占位图")
            assert "MOCK" in job["message"] or "预览占位图" in job["message"]
            assert IMAGE_KEY not in created.text
            assert REVIEW_KEY not in created.text
            file_url = job["images"][0]["url"]
            downloaded = client.get(file_url, headers=headers)
            assert downloaded.status_code == 200
            assert downloaded.headers["content-type"].startswith("image/svg+xml")
            assert "private" in downloaded.headers["cache-control"]
            assert "inline" in downloaded.headers["content-disposition"]
            saved = client.get(f"{file_url}?download=1", headers=headers)
            assert saved.status_code == 200
            assert saved.content == downloaded.content
            disposition = saved.headers["content-disposition"]
            assert "attachment" in disposition
            assert "mock-1.svg" in disposition
            assert b"MOCK" in downloaded.content
            assert b'width="2048"' in downloaded.content
            assert b'height="1152"' in downloaded.content
            assert b"<script>" not in downloaded.content
            assert b"&lt;script&gt;" in downloaded.content

            again = client.post("/v1/image-workbench/generate", headers=headers, json=_body(size="1024x1024", n=4))
            assert again.status_code == 200, again.text
            assert again.json()["est_cost_cents"] == 40
            assert len(again.json()["images"]) == 4
            key_calls = [request.method for request in seen if request.url.path.endswith("/keys")]
            assert key_calls == ["GET", "POST", "GET"]
            assert key_calls.count("POST") == 1

            member, db = _member("zhang@jiecheng.com")
            assert member.relay_api_key == REVIEW_KEY
            assert member.relay_image_api_key == IMAGE_KEY
            assert member.balance_cents == before
            db.close()

            from admin_app.db import session
            from admin_app.models import AuditLog, RelayLedger

            db = session()
            audits = db.query(AuditLog).filter(AuditLog.action == "image.generate_preview").all()
            assert len(audits) == 2
            assert all(IMAGE_KEY not in item.summary and REVIEW_KEY not in item.summary for item in audits)
            assert db.query(RelayLedger).count() == 0
            db.close()

            own = client.get("/v1/image-workbench/jobs", headers=headers)
            assert own.status_code == 200
            assert {item["id"] for item in own.json()} == {job["id"], again.json()["id"]}
            landscape = client.get("/v1/image-workbench/jobs?orientation=landscape", headers=headers).json()
            assert [item["id"] for item in landscape] == [job["id"]]
            found = client.get("/v1/image-workbench/jobs?query=礼盒", headers=headers).json()
            assert job["id"] in {item["id"] for item in found}

            for email in ("chen@youpin.com", "li@jiecheng.com", "admin@yujing.local"):
                other = _login(client, email)
                assert client.get("/v1/image-workbench/jobs", headers=other).json() == []
                assert client.get(file_url, headers=other).status_code == 404
                assert client.delete(f"/v1/image-workbench/jobs/{job['id']}", headers=other).status_code == 404

            removed = client.delete(f"/v1/image-workbench/jobs/{job['id']}", headers=headers)
            assert removed.status_code == 200
            assert client.get(file_url, headers=headers).status_code == 404
        finally:
            set_transport(None)


def test_rejected_combinations_do_not_call_upstream(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        seen = _install_transport(_key_handler)
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            cases = [
                _body(size="2560x1440"),
                _body(size="2048x2048"),
                _body(size="2160x3840"),
                _body(model="gpt-image-2.5-sunburst", size="2048x1152"),
                _body(size="2048x1152", n=2),
                _body(size="1024x1024", quality="high"),
                _body(model="not-a-model"),
            ]
            for payload in cases:
                response = client.post("/v1/image-workbench/generate", headers=headers, json=payload)
                assert response.status_code == 400, response.text
            extra = client.post(
                "/v1/image-workbench/generate",
                headers=headers,
                json=_body(group=16, baseURL="https://chuanapi.com", key="sk-bypass"),
            )
            assert extra.status_code == 422
            assert client.post("/v1/image-workbench/generate", headers=headers, json=_body(confirm=False)).status_code == 422
            assert client.post("/v1/image-workbench/generate", headers=headers, json=_body(async_mode=True)).status_code == 422
            assert client.post("/v1/image-workbench/references", headers=headers).status_code == 403
            assert seen == []
        finally:
            set_transport(None)


def test_listed_key_ignores_review_key(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("已有生图密钥时不应再创建")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {"name": "电商视频合规", "group_id": 42, "key": REVIEW_KEY},
                        {"name": "生图专用", "group_id": 15, "key": "wrong-group"},
                        {"name": "生图专用", "group_id": 42, "key": "sk-image-listed"},
                    ]
                },
            },
        )

    with _client(tmp_path, monkeypatch) as client:
        _install_transport(handler)
        try:
            monkeypatch.setenv("EVC_ADMIN_IMAGE_GROUP_ID", "42")
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            created = client.post("/v1/image-workbench/generate", headers=headers, json=_body())
            assert created.status_code == 200, created.text
            member, db = _member("zhang@jiecheng.com")
            assert member.relay_image_api_key == "sk-image-listed"
            assert member.relay_api_key == ""
            db.close()
            assert REVIEW_KEY not in created.text
        finally:
            set_transport(None)


def test_group_model_list_controls_selection(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _install_transport(_key_handler)
        try:
            _groups(
                monkeypatch,
                [{"id": 42, "name": GROUP_NAME, "status": "active", "models": ["gpt-image-2.5-sunburst"]}],
            )
            headers = _login(client, "zhang@jiecheng.com")
            blocked = client.post(
                "/v1/image-workbench/generate",
                headers=headers,
                json=_body(size="2048x1152"),
            )
            assert blocked.status_code == 400, blocked.text
            allowed = client.post(
                "/v1/image-workbench/generate",
                headers=headers,
                json=_body(model="gpt-image-2.5-sunburst", size="1024x1024"),
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["mock"] is True
        finally:
            set_transport(None)


def test_live_path_stays_off_until_both_switches_and_verified_combo(tmp_path, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.url.path.endswith("/images/generations"):
            body = json.loads(request.content.decode())
            assert body == {"model": FLARE, "prompt": "红色礼盒 白底", "n": 1, "size": "3840x2160"}
            assert request.headers.get("authorization") == f"Bearer {IMAGE_KEY}"
            assert ADMIN_KEY not in request.headers.get("authorization", "")
            encoded = base64.b64encode(PNG).decode()
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        return _key_handler(request)

    with _client(tmp_path, monkeypatch, admin_key=ADMIN_KEY) as client:
        _install_transport(handler)
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            monkeypatch.setenv("EVC_ADMIN_IMAGE_LIVE", "1")
            one_switch = client.post("/v1/image-workbench/generate", headers=headers, json=_body(size="3840x2160"))
            assert one_switch.status_code == 200, one_switch.text
            assert one_switch.json()["mock"] is True
            assert not any("/images/generations" in item for item in calls)

            monkeypatch.setenv("EVC_ADMIN_IMAGE_CONTRACT_OK", "1")
            pending = client.post("/v1/image-workbench/generate", headers=headers, json=_body(size="1024x1024"))
            assert pending.status_code == 400, pending.text
            assert not any("/images/generations" in item for item in calls)

            live = client.post("/v1/image-workbench/generate", headers=headers, json=_body(size="3840x2160"))
            assert live.status_code == 200, live.text
            assert live.json()["mock"] is False
            assert any(item.endswith("/images/generations") for item in calls)
            downloaded = client.get(live.json()["images"][0]["url"], headers=headers)
            assert downloaded.content == PNG
            assert b"MOCK" not in downloaded.content

            from admin_app.config import get_settings

            monkeypatch.setenv("EVC_RELAY_GATEWAY_URL", "https://chuanapi.com/v1")
            get_settings.cache_clear()
            before = len(calls)
            refused = client.post("/v1/image-workbench/generate", headers=headers, json=_body(size="2048x1152"))
            assert refused.status_code == 400, refused.text
            assert "token4u.cc/v1/images/generations" in refused.json()["detail"]
            assert not any("/images/generations" in item for item in calls[before:])
        finally:
            set_transport(None)
            from admin_app.config import get_settings

            get_settings.cache_clear()


def test_private_result_url_is_rejected():
    from admin_app.image_relay import _assert_public_image_url

    for url in ("http://127.0.0.1/a.png", "https://127.0.0.1/a.png", "https://chuanapi.com/a.png"):
        try:
            _assert_public_image_url(url)
        except DomainError as exc:
            assert exc.status == 502
        else:
            raise AssertionError(url)


def test_existing_member_column_upgrade_keeps_review_key(tmp_path, monkeypatch):
    dbfile = tmp_path / "old.db"
    monkeypatch.setenv("EVC_ADMIN_DATABASE_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("EVC_ADMIN_JWT_SECRET", "test-secret-test-secret")
    from admin_app.config import get_settings
    from admin_app.db import init_db, reset_engine

    get_settings.cache_clear()
    reset_engine()
    engine = create_engine(f"sqlite:///{dbfile}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE tenant_members (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(120),
                    relay_api_key TEXT NOT NULL DEFAULT '',
                    month_cost_cents INTEGER NOT NULL DEFAULT 0,
                    previous_month_cost_cents INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        connection.execute(text("INSERT INTO tenant_members (id, email, relay_api_key) VALUES (1, 'a@b.c', 'review-key')"))
    engine.dispose()
    init_db()
    engine = create_engine(f"sqlite:///{dbfile}")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT relay_api_key, relay_image_api_key FROM tenant_members WHERE id = 1")).one()
        tables = {item[0] for item in connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
    assert row == ("review-key", "")
    assert "image_jobs" in tables


def test_image_modules_do_not_hardcode_group_number():
    root = Path(__file__).resolve().parents[1] / "admin_app"
    for name in ("image_relay.py", "image_routes.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "group_id = 16" not in source
        assert "group_id=16" not in source
        assert "== 16" not in source
        assert 'os.environ.get("EVC_RELAY_GROUP_NAME"' not in source
        assert "relay_group_name" not in source


def test_image_key_failure_stays_out_of_logs_and_review_key(tmp_path, monkeypatch, caplog):
    import logging

    secret = "sk-image-failure-must-stay-out"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={"message": secret, "key": secret})
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    with _client(tmp_path, monkeypatch) as client:
        _install_transport(handler)
        try:
            _groups(monkeypatch)
            headers = _login(client, "zhang@jiecheng.com")
            with caplog.at_level(logging.DEBUG):
                status = client.get("/v1/image-workbench/status", headers=headers)
            assert status.status_code == 200, status.text
            assert "生图密钥没有创建成功" in status.json()["group_reason"]
            assert "审核功能不受影响" in status.json()["group_reason"]
            assert secret not in status.text
            assert secret not in caplog.text
            member, db = _member("zhang@jiecheng.com")
            assert member.relay_api_key == ""
            assert member.relay_image_api_key == ""
            db.close()
            with caplog.at_level(logging.DEBUG):
                denied = client.post("/v1/image-workbench/generate", headers=headers, json=_body())
            assert denied.status_code == 502, denied.text
            assert secret not in denied.text
            assert secret not in caplog.text
        finally:
            set_transport(None)


def test_concurrent_image_key_create_posts_once():
    import threading

    posts = {"n": 0}
    items: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"items": list(items)}})
        posts["n"] += 1
        items.append({"id": 1, "name": "生图专用", "group_id": 16, "key": IMAGE_KEY})
        return httpx.Response(200, json={"code": 0, "data": {"key": IMAGE_KEY}})

    set_transport(httpx.MockTransport(handler))
    try:
        from admin_app.image_relay import create_or_find_image_key

        errors: list[BaseException] = []

        def work() -> None:
            try:
                found = create_or_find_image_key(
                    "https://token4u.cc/api/v1",
                    "relay-test-access",
                    16,
                    "zhang@jiecheng.com",
                )
                assert found == IMAGE_KEY
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert posts["n"] == 1
    finally:
        set_transport(None)
