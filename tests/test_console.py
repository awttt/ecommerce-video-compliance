"""控制台壳：静态页面、转发规则和入口开关。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.console import (
    DEFAULT_CLOUD_URL,
    DEFAULT_PORT,
    adopt_tools_session,
    cloud_base_url,
    console_port,
    create_console_app,
)
from app.factory import create_app


@pytest.fixture
def dist(tmp_path):
    folder = tmp_path / "dist"
    folder.mkdir()
    (folder / "index.html").write_text("<html><body>console-spa</body></html>", encoding="utf-8")
    (folder / "logo.png").write_bytes(b"png")
    (folder / "assets").mkdir()
    (folder / "assets" / "app.js").write_text("console-js", encoding="utf-8")
    return folder


def _client(dist, handler, cloud="https://admin.example.test"):
    app = create_console_app(cloud=cloud, dist=dist, transport=httpx.MockTransport(handler))
    return TestClient(app)


def test_proxy_keeps_method_query_body_and_auth(dist):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["authorization"] = request.headers.get("authorization")
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(
            201,
            json={"ok": True},
            headers={"set-cookie": "evc_admin_access=abc; Path=/", "x-upstream": "1"},
        )

    with _client(dist, handler) as client:
        response = client.post(
            "/v1/members/invite?dry=1",
            json={"email": "a@b.c"},
            headers={"Authorization": "Bearer token-1", "Cookie": "evc_admin_access=abc"},
        )

    assert seen["method"] == "POST"
    assert seen["url"] == "https://admin.example.test/v1/members/invite?dry=1"
    assert seen["body"] == '{"email":"a@b.c"}'
    assert seen["authorization"] == "Bearer token-1"
    assert seen["cookie"] == "evc_admin_access=abc"

    assert response.status_code == 201
    assert response.json() == {"ok": True}
    assert response.headers["x-upstream"] == "1"
    assert "evc_admin_access=abc" in response.headers["set-cookie"]


def test_proxy_passes_upstream_errors_through(dist):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "请先登录"})

    with _client(dist, handler) as client:
        response = client.get("/v1/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "请先登录"}


def test_proxy_does_not_forward_client_encoding(dist):
    """浏览器的 accept-encoding 可能要求 br/zstd，本机解不开，必须让 httpx 自己声明。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["encoding"] = request.headers.get("accept-encoding", "")
        return httpx.Response(200, json={"ok": True})

    with _client(dist, handler) as client:
        response = client.get("/v1/auth/me", headers={"Accept-Encoding": "x-client-only"})

    assert response.status_code == 200
    assert "x-client-only" not in str(seen["encoding"])


def test_proxy_reports_network_failure_as_gateway_error(dist):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with _client(dist, handler) as client:
        response = client.get("/v1/health")

    assert response.status_code == 502
    assert "连不上管理服务器" in response.json()["detail"]


def test_unknown_api_path_is_not_swallowed_by_spa(dist):
    with _client(dist, lambda request: httpx.Response(404)) as client:
        assert client.get("/v1/does-not-exist").status_code == 404
        assert client.get("/v1").status_code == 404


def test_spa_serves_assets_and_falls_back_to_index(dist):
    with _client(dist, lambda request: httpx.Response(500)) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "console-spa" in root.text
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console-js"
        deep = client.get("/members")
        assert deep.status_code == 200
        assert "console-spa" in deep.text


def test_spa_does_not_escape_dist(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console-spa</html>", encoding="utf-8")
    with _client(dist, lambda request: httpx.Response(500)) as client:
        assert "secret" not in client.get("/../secret.txt").text


def test_spa_shows_build_hint_when_dist_missing(tmp_path):
    app = create_console_app(
        cloud="https://admin.example.test",
        dist=tmp_path / "missing",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 503
    assert "admin/web" in response.text


def test_index_injects_tools_url_when_launched_from_desktop(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body>console-spa</body></html>", encoding="utf-8")
    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        tools_url="http://127.0.0.1:8765/",
    )
    with TestClient(app) as client:
        html = client.get("/").text
    assert 'window.__EVC_TOOLS_URL__="http://127.0.0.1:8765/"' in html
    assert html.index("__EVC_TOOLS_URL__") < html.index("</head>")


def test_index_has_no_tools_url_when_served_standalone(dist):
    with _client(dist, lambda request: httpx.Response(500)) as client:
        assert "__EVC_TOOLS_URL__" not in client.get("/").text


def test_healthz_reports_cloud(dist):
    with _client(dist, lambda request: httpx.Response(500)) as client:
        body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["cloud"] == "https://admin.example.test"


def test_console_endpoint_exposes_local_url():
    with TestClient(create_app("http://127.0.0.1:8780/")) as client:
        body = client.get("/api/console").json()
    assert body == {"enabled": True, "url": "http://127.0.0.1:8780/"}


def test_console_endpoint_is_disabled_without_url():
    with TestClient(create_app()) as client:
        body = client.get("/api/console").json()
    assert body == {"enabled": False, "url": ""}


def test_server_url_and_port_follow_environment(monkeypatch):
    monkeypatch.delenv("EVC_ADMIN_SERVER_URL", raising=False)
    monkeypatch.delenv("EVC_CONSOLE_PORT", raising=False)
    assert cloud_base_url() == DEFAULT_CLOUD_URL
    assert console_port() == DEFAULT_PORT

    monkeypatch.setenv("EVC_ADMIN_SERVER_URL", "https://admin.example.test/")
    monkeypatch.setenv("EVC_CONSOLE_PORT", "8790")
    assert cloud_base_url() == "https://admin.example.test"
    assert console_port() == 8790

    monkeypatch.setenv("EVC_CONSOLE_PORT", "not-a-port")
    assert console_port() == DEFAULT_PORT


def test_sso_exchanges_tools_session(dist, monkeypatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": "admin-access", "refresh_token": "admin-refresh"},
            headers={"set-cookie": "evc_admin_access=admin-access; Path=/"},
        )

    monkeypatch.setattr("app.console._tools_credentials", lambda: ("relay-token", "relay-refresh"))
    with _client(dist, handler) as client:
        response = client.post("/sso")

    assert seen["url"] == "https://admin.example.test/v1/auth/sso"
    assert seen["body"] == {"access_token": "relay-token", "refresh_token": "relay-refresh"}
    assert response.status_code == 200
    assert response.json()["access_token"] == "admin-access"
    assert "evc_admin_access=admin-access" in response.headers["set-cookie"]


def test_sso_requires_tools_login(dist, monkeypatch):
    monkeypatch.setattr("app.console._tools_credentials", lambda: ("", ""))
    with _client(dist, lambda request: httpx.Response(500)) as client:
        response = client.post("/sso")
    assert response.status_code == 401
    assert "工具端还没有登录" in response.json()["detail"]


def test_sso_reports_network_failure(dist, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("app.console._tools_credentials", lambda: ("relay-token", ""))
    with _client(dist, handler) as client:
        response = client.post("/sso")
    assert response.status_code == 502


def test_adopt_tools_session_logs_tools_in(monkeypatch):
    import asyncio

    import app.services.manager as manager_module

    calls: dict[str, object] = {}

    class FakeManager:
        async def account_login(self, email: str, password: str) -> None:
            calls["login"] = (email, password)

        async def account_login_2fa(self, code: str) -> None:
            calls["2fa"] = code

    monkeypatch.setattr(manager_module, "get_manager", lambda: FakeManager())
    asyncio.run(adopt_tools_session("login", {"email": "a@b.c", "password": "pw"}))
    asyncio.run(adopt_tools_session("2fa", {"totp_code": "123456"}))
    assert calls == {"login": ("a@b.c", "pw"), "2fa": "123456"}


def test_recharge_proxy_forwards_requests(dist):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"order_id": 123, "status": "PENDING"})

    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(handler),
        tools_url="http://tools.test/",
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/account/recharge/orders",
            json={"amount": 50, "payment_type": "wechat"},
        )

    assert response.status_code == 200
    assert seen["method"] == "POST"
    assert "http://tools.test/api/account/recharge/orders" in str(seen["url"])
    assert response.json()["order_id"] == 123


def test_recharge_proxy_validates_path(dist):
    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        tools_url="http://tools.test/",
    )
    with TestClient(app) as client:
        assert client.get("/api/account/recharge/orders").status_code == 200
        assert client.get("/api/account/recharge/orders/123").status_code == 200
        assert client.post("/api/account/recharge/orders/123/cancel").status_code == 200
        assert client.get("/api/account/recharge/invalid").status_code == 404
        assert client.get("/api/account/recharge/orders/123/invalid").status_code == 404


def test_recharge_returns_404_without_tools_url(dist):
    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        tools_url="",
    )
    with TestClient(app) as client:
        response = client.get("/api/account/recharge/orders")
        assert response.status_code == 404
        assert "充值只能在桌面端使用" in response.json()["detail"]


def test_recharge_reports_tools_error(dist):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(handler),
        tools_url="http://tools.test/",
    )
    with TestClient(app) as client:
        response = client.get("/api/account/recharge/orders")
        assert response.status_code == 502
        assert "连不上本机服务" in response.json()["detail"]


def test_balance_endpoint_returns_three_fields(dist):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "account_logged_in": True,
                "account_email": "user@example.com",
                "account_balance": 123.45,
                "account_frozen_balance": 10.0,
                "account_today_cost": 5.0,
            },
        )

    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(handler),
        tools_url="http://tools.test/",
    )
    with TestClient(app) as client:
        response = client.get("/api/account/balance")
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"logged_in", "email", "balance"}
        assert data["logged_in"] is True
        assert data["email"] == "user@example.com"
        assert data["balance"] == 123.45


def test_balance_endpoint_without_tools_url(dist):
    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        tools_url="",
    )
    with TestClient(app) as client:
        response = client.get("/api/account/balance")
        assert response.status_code == 404
        assert "充值只能在桌面端使用" in response.json()["detail"]


def test_recharge_info_and_order_paths_reach_tools_without_admin_token(dist):
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    app = create_console_app(
        cloud="https://admin.example.test",
        dist=dist,
        transport=httpx.MockTransport(handler),
        tools_url="http://tools.test/",
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer admin-token"}
        assert client.get("/api/account/recharge", headers=headers).status_code == 200
        assert client.get("/api/account/recharge/orders/7", headers=headers).status_code == 200
        assert client.post("/api/account/recharge/orders/7/cancel", headers=headers).status_code == 200
        assert client.get("/api/settings").status_code == 404

    assert seen == [
        ("GET", "http://tools.test/api/account/recharge", None),
        ("GET", "http://tools.test/api/account/recharge/orders/7", None),
        ("POST", "http://tools.test/api/account/recharge/orders/7/cancel", None),
    ]
