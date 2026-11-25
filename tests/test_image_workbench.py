"""生图工作台。不访问真实上游，契约用测试注入。"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.image_contract import (
    IMAGE_KEY_NAME,
    ImageContract,
    apply_preview_login,
    gateway_base,
    install_contract,
    install_transport,
    prepare_upstream,
    preview_png,
    reset_image_contract,
    resolve_image_group,
)
from app.core.image_contract import LocalRequest
from app.core.relay import KEY_NAME, RelayClient, RelayError
from app.factory import create_app
from app.services.manager import get_manager

DETECTION_KEY = "sk-detection-key-4242"
IMAGE_KEY = "sk-image-user-key-7777"
TINY_PNG = preview_png("auto", 0)


def sign_in(email: str = "user@example.com") -> None:
    manager = get_manager()
    manager._settings["relay_email"] = email
    manager._settings["relay_access_token"] = "access-token-for-tests"
    manager._settings["relay_login_at"] = __import__("time").time()


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def raw_pixels(data: bytes) -> bytes:
    position = 8
    payload = b""
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        tag = data[position + 4 : position + 8]
        chunk = data[position + 8 : position + 8 + length]
        if tag == b"IDAT":
            payload += chunk
        position += 12 + length
    return zlib.decompress(payload)


class GroupRelay:
    def __init__(self, groups: list[dict], created_group: int | None = None):
        self.groups = groups
        self.created: list[tuple] = []
        self.updates: list[tuple] = []
        self.created_group = created_group

    async def list_groups(self, token: str):
        return self.groups

    async def list_keys(self, token: str):
        return [
            {
                "id": 3,
                "name": KEY_NAME,
                "status": "active",
                "group_id": 4,
                "key": DETECTION_KEY,
            }
        ]

    async def create_key(self, token: str, group_id: int):
        raise AssertionError("image flow must not create the detection key")

    async def create_key_named(self, token: str, name: str, group_id: int):
        self.created.append((name, group_id, token))
        return {
            "id": 11,
            "name": name,
            "status": "active",
            "group_id": self.created_group if self.created_group is not None else group_id,
            "key": IMAGE_KEY,
        }

    async def update_key_group(self, token: str, key_id: int, group_id: int):
        self.updates.append((key_id, group_id))
        return {"id": key_id, "group_id": group_id}


def test_preview_png_is_labeled_and_keeps_aspect():
    square = preview_png("1024x1024", 0)
    landscape = preview_png("1536x1024", 1)
    portrait = preview_png("1024x1536", 2)
    assert png_size(square) == (480, 480)
    assert png_size(landscape) == (576, 384)
    assert png_size(portrait) == (384, 576)
    assert b"\x1f\x3b\x73" in raw_pixels(square)


def test_gateway_rejects_direct_and_arbitrary_hosts():
    blocked = [
        "https://chuanapi.com/v1",
        "https://api.chuanapi.com/v1",
        "https://token4u.cc.evil.com/v1",
        "http://token4u.cc/v1",
        "https://user:secret@token4u.cc/v1",
        "https://token4u.cc/v1/images",
        "https://127.0.0.1/v1",
    ]
    from app.core import image_contract

    for raw in blocked:
        image_contract.os.environ["EVC_IMAGE_GATEWAY_URL"] = raw
        with pytest.raises(image_contract.ImageError):
            gateway_base()
    image_contract.os.environ["EVC_IMAGE_GATEWAY_URL"] = "https://token4u.cc/v1"
    assert gateway_base() == "https://token4u.cc/v1"


def test_prepare_upstream_sends_only_whitelisted_fields():
    local = LocalRequest(prompt="一只杯子", model="contract-model", size="1536x1024", count=3, quality="auto")
    refused = ImageContract(confirmed=False, fields=frozenset({"model", "prompt"}), models=("contract-model",), parser=lambda raw: [])
    with pytest.raises(Exception, match="契约尚未确认"):
        prepare_upstream(local, IMAGE_KEY, refused)
    extra = ImageContract(confirmed=True, fields=frozenset({"model", "prompt", "size"}), models=("contract-model",), parser=lambda raw: [b"x"])
    with pytest.raises(Exception, match="尚未确认的参数"):
        prepare_upstream(local, IMAGE_KEY, extra)
    accepted = ImageContract(
        confirmed=True,
        fields=frozenset({"model", "prompt"}),
        models=("contract-model",),
        parser=lambda raw: [TINY_PNG],
    )
    prepared = prepare_upstream(local, IMAGE_KEY, accepted)
    assert prepared.url == "https://token4u.cc/v1/images/generations"
    assert prepared.body == {"model": "contract-model", "prompt": "一只杯子"}
    assert prepared.api_key == IMAGE_KEY
    with pytest.raises(Exception):
        prepare_upstream(local, "preview-local-session", accepted)


def test_image_group_never_falls_back_to_detection():
    detection = [{"id": 4, "name": "电商审核专属-bot创建", "status": "active"}]
    image = detection + [{"id": 7, "name": "生图专属-bot创建", "status": "active"}]
    with pytest.raises(Exception, match="尚未配置"):
        resolve_image_group(image)
    from app.core import image_contract

    image_contract.os.environ["EVC_IMAGE_RELAY_GROUP_NAME"] = "生图专属-bot创建"
    assert resolve_image_group(image) == 7
    with pytest.raises(Exception, match="不会改用审核分组"):
        resolve_image_group(detection)
    image_contract.os.environ["EVC_IMAGE_RELAY_GROUP_NAME"] = "电商审核专属-bot创建"
    with pytest.raises(Exception, match="不能与审核分组同名"):
        resolve_image_group(image)
    image_contract.os.environ["EVC_IMAGE_RELAY_GROUP_NAME"] = ""
    image_contract.os.environ["EVC_IMAGE_RELAY_GROUP_ID"] = "4"
    with pytest.raises(Exception, match="审核分组"):
        resolve_image_group(detection)
    image_contract.os.environ["EVC_IMAGE_RELAY_GROUP_ID"] = "7"
    assert resolve_image_group([{"id": 7, "name": "其他", "status": "active"}]) == 7


def test_preview_login_is_mock_only(monkeypatch):
    assert apply_preview_login({}) is False
    monkeypatch.setenv("EVC_IMAGE_PREVIEW_LOGIN", "1")
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "token4u")
    assert apply_preview_login({}) is False
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "mock")
    settings: dict = {}
    assert apply_preview_login(settings) is True
    assert settings["relay_access_token"] == "preview-local-session"
    assert apply_preview_login({"relay_access_token": "already-logged-in"}) is False


def test_named_key_entry_cannot_create_the_detection_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not reach the network")

    async def run():
        client = RelayClient(transport=httpx.MockTransport(handler))
        with pytest.raises(RelayError):
            await client.create_key_named("access-token-for-tests", KEY_NAME, 4)

    asyncio.run(run())


def test_unconfigured_generation_is_refused_and_makes_no_upstream_call():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("upstream was called")

    install_transport(httpx.MockTransport(handler))
    with TestClient(create_app()) as client:
        sign_in()
        status = client.get("/api/images/status")
        assert status.status_code == 200
        body = status.json()
        assert body["enabled"] is False
        assert body["mode"] == "unconfigured"
        assert body["preview"] is False
        assert body["models"] == []
        assert "不会改用审核分组" in body["reason"]
        denied = client.post("/api/images/generations", json={"prompt": "湖面", "size": "1024x1024", "count": 1})
        assert denied.status_code == 409
        assert "禁用" in denied.json()["detail"]
        stolen = client.post(
            "/api/images/generations",
            json={"prompt": "湖面", "group_id": 7, "base_url": "https://chuanapi.com/v1", "api_key": IMAGE_KEY},
        )
        assert stolen.status_code == 400
        assert IMAGE_KEY not in stolen.text
    assert calls == []


def test_mock_generation_history_download_and_owner_isolation(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "mock")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("mock must not call upstream")

    install_transport(httpx.MockTransport(handler))
    with TestClient(create_app()) as client:
        assert client.get("/api/images/status").status_code == 401
        sign_in("a@example.com")
        get_manager()._settings["base_url"] = "https://chuanapi.com/v1"
        get_manager()._settings["relay_api_key"] = DETECTION_KEY
        status = client.get("/api/images/status").json()
        assert status["preview"] is True
        assert status["enabled"] is True
        assert status["group_label"] == "预览不绑定分组"
        assert status["models"] == [{"id": "preview-mock", "label": "预览模拟（不是上游模型）"}]
        created = client.post(
            "/api/images/generations",
            json={"prompt": "日出湖面", "model": "preview-mock", "size": "1536x1024", "count": 2},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["preview"] is True
        assert body["upstream_sent"] is False
        assert body["status"] == "done"
        assert "不是真实上游生图" in body["message"]
        assert len(body["files"]) == 2
        assert "image-workbench" not in created.text
        assert DETECTION_KEY not in created.text
        downloaded = client.get(body["files"][0]["url"])
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("image/png")
        assert "no-store" in downloaded.headers["cache-control"]
        assert png_size(downloaded.content) == (576, 384)
        listed = client.get("/api/images/batches", params={"orientation": "landscape", "q": "湖面"})
        assert [item["id"] for item in listed.json()] == [body["id"]]
        assert client.get("/api/images/batches", params={"q": "%"}).json() == []
        sign_in("b@example.com")
        hidden = client.get(body["files"][0]["url"])
        assert hidden.status_code == 404
        assert client.get(f"/api/images/batches/{body['id']}").status_code == 404
        sign_in("a@example.com")
        assert client.get("/api/images/batches/../secrets").status_code == 404
        assert client.get(f"/api/images/batches/{body['id']}/files/9").status_code == 404
        assert get_manager()._settings["relay_api_key"] == DETECTION_KEY
        removed = client.delete(f"/api/images/batches/{body['id']}")
        assert removed.status_code == 200
        assert client.get(f"/api/images/batches/{body['id']}").status_code == 404


def test_mock_reference_stays_local(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "mock")
    with TestClient(create_app()) as client:
        sign_in()
        response = client.post(
            "/api/images/generations",
            data={"prompt": "带参考图", "size": "1024x1024", "count": "1"},
            files={"references": ("note.png", TINY_PNG, "image/png")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["upstream_sent"] is False
        assert len(body["references"]) == 1
        reference = client.get(body["references"][0]["url"])
        assert reference.status_code == 200
        assert reference.content == TINY_PNG
        gif = client.post(
            "/api/images/generations",
            data={"prompt": "动图", "count": "1"},
            files={"references": ("a.gif", b"GIF89a" + b"\x00" * 16, "image/gif")},
        )
        assert gif.status_code == 415
        huge = b"\xff\xd8\xff" + b"\x00" * (4 * 1024 * 1024)
        oversized = client.post(
            "/api/images/generations",
            data={"prompt": "太大", "count": "1"},
            files={"references": ("big.jpg", huge, "image/jpeg")},
        )
        assert oversized.status_code == 413


def test_token4u_without_contract_does_not_call_or_reuse_detection_key(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "token4u")
    monkeypatch.setenv("EVC_IMAGE_RELAY_GROUP_NAME", "生图专属-bot创建")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("upstream was called")

    install_transport(httpx.MockTransport(handler))
    relay = GroupRelay([
        {"id": 4, "name": "电商审核专属-bot创建", "status": "active"},
        {"id": 7, "name": "生图专属-bot创建", "status": "active"},
    ])
    with TestClient(create_app()) as client:
        sign_in()
        manager = get_manager()
        manager.relay = relay
        manager._settings["relay_api_key"] = DETECTION_KEY
        manager._settings["base_url"] = "https://chuanapi.com/v1"
        status = client.get("/api/images/status")
        assert status.status_code == 200
        assert status.json()["enabled"] is False
        assert "契约尚未确认" in status.json()["reason"]
        assert status.json()["group_label"] == "生图专属-bot创建"
        denied = client.post("/api/images/generations", json={"prompt": "杯子", "model": "contract-model"})
        assert denied.status_code == 409
        assert "契约尚未确认" in denied.json()["detail"]
        assert manager._settings["relay_api_key"] == DETECTION_KEY
        assert "image_relay_api_key" not in manager._settings
    assert relay.created == []
    assert calls == []


def test_confirmed_contract_posts_user_key_to_token4u_only(monkeypatch, caplog):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "token4u")
    monkeypatch.setenv("EVC_IMAGE_RELAY_GROUP_NAME", "生图专属-bot创建")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"ignored-body")

    install_transport(httpx.MockTransport(handler))
    install_contract(
        ImageContract(
            confirmed=True,
            fields=frozenset({"model", "prompt"}),
            models=("contract-model",),
            parser=lambda raw: [preview_png("1024x1024", 0)],
        )
    )
    relay = GroupRelay(
        [
            {"id": 4, "name": "电商审核专属-bot创建", "status": "active"},
            {"id": 7, "name": "生图专属-bot创建", "status": "active"},
        ]
    )
    with TestClient(create_app()) as client:
        sign_in()
        manager = get_manager()
        manager.relay = relay
        manager._settings["relay_api_key"] = DETECTION_KEY
        manager._settings["relay_api_key_id"] = 3
        manager._settings["base_url"] = "https://chuanapi.com/v1"
        response = client.post(
            "/api/images/generations",
            json={
                "prompt": "一只杯子",
                "model": "contract-model",
                "size": "1024x1536",
                "count": 4,
                "quality": "auto",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["upstream_sent"] is True
        assert body["preview"] is False
        assert body["size"] == "1024x1536"
        assert DETECTION_KEY not in response.text
        assert IMAGE_KEY not in response.text
        assert manager._settings["relay_api_key"] == DETECTION_KEY
        assert manager._settings["relay_api_key_id"] == 3
        assert manager._settings["image_relay_api_key"] == IMAGE_KEY
        assert manager._settings["image_relay_group_id"] == 7
    assert relay.created == [(IMAGE_KEY_NAME, 7, "access-token-for-tests")]
    assert relay.updates == []
    assert len(calls) == 1
    sent = calls[0]
    assert str(sent.url) == "https://token4u.cc/v1/images/generations"
    assert "chuanapi" not in str(sent.url)
    assert sent.headers["authorization"] == f"Bearer {IMAGE_KEY}"
    assert json.loads(sent.content) == {"model": "contract-model", "prompt": "一只杯子"}
    assert "size" not in json.loads(sent.content)
    assert IMAGE_KEY not in caplog.text
    assert DETECTION_KEY not in caplog.text


def test_wrong_created_group_is_rebound_without_touching_detection_key(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "token4u")
    monkeypatch.setenv("EVC_IMAGE_RELAY_GROUP_NAME", "生图专属-bot创建")
    install_transport(httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")))
    install_contract(
        ImageContract(
            confirmed=True,
            fields=frozenset({"prompt"}),
            models=("contract-model",),
            parser=lambda raw: [preview_png("auto", 0)],
        )
    )
    relay = GroupRelay(
        [
            {"id": 4, "name": "电商审核专属-bot创建", "status": "active"},
            {"id": 7, "name": "生图专属-bot创建", "status": "active"},
        ],
        created_group=4,
    )
    with TestClient(create_app()) as client:
        sign_in()
        manager = get_manager()
        manager.relay = relay
        manager._settings["relay_api_key"] = DETECTION_KEY
        response = client.post("/api/images/generations", json={"prompt": "杯子", "model": "contract-model"})
        assert response.status_code == 200, response.text
        assert manager._settings["relay_api_key"] == DETECTION_KEY
    assert relay.updates == [(11, 7)]


def test_upstream_error_does_not_leak_and_does_not_follow_redirect(monkeypatch, caplog):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "token4u")
    monkeypatch.setenv("EVC_IMAGE_RELAY_GROUP_NAME", "生图专属-bot创建")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://chuanapi.com/secret"}, text=f"leak {IMAGE_KEY}")

    install_transport(httpx.MockTransport(handler))
    install_contract(
        ImageContract(
            confirmed=True,
            fields=frozenset({"model", "prompt"}),
            models=("contract-model",),
            parser=lambda raw: [preview_png("auto", 0)],
        )
    )
    relay = GroupRelay([
        {"id": 4, "name": "电商审核专属-bot创建", "status": "active"},
        {"id": 7, "name": "生图专属-bot创建", "status": "active"},
    ])
    with TestClient(create_app()) as client:
        sign_in()
        manager = get_manager()
        manager.relay = relay
        response = client.post("/api/images/generations", json={"prompt": "杯子", "model": "contract-model"})
        assert response.status_code == 502
        assert IMAGE_KEY not in response.text
        assert "chuanapi" not in response.text
        assert "image_relay_api_key" not in client.get("/api/settings").text
    assert len(calls) == 1
    assert IMAGE_KEY not in caplog.text


def test_direct_gateway_env_is_rejected_before_key_creation(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "token4u")
    monkeypatch.setenv("EVC_IMAGE_RELAY_GROUP_NAME", "生图专属-bot创建")
    monkeypatch.setenv("EVC_IMAGE_GATEWAY_URL", "https://chuanapi.com/v1")
    install_contract(
        ImageContract(
            confirmed=True,
            fields=frozenset({"model", "prompt"}),
            models=("contract-model",),
            parser=lambda raw: [preview_png("auto", 0)],
        )
    )
    relay = GroupRelay([{"id": 7, "name": "生图专属-bot创建", "status": "active"}])
    with TestClient(create_app()) as client:
        sign_in()
        get_manager().relay = relay
        response = client.post("/api/images/generations", json={"prompt": "杯子", "model": "contract-model"})
        assert response.status_code == 409
        assert "直连" in response.json()["detail"]
    assert relay.created == []


def test_logout_and_account_switch_drop_image_key_only():
    with TestClient(create_app()) as client:
        sign_in("first@example.com")
        manager = get_manager()
        manager._settings["image_relay_api_key"] = IMAGE_KEY
        manager._settings["relay_api_key"] = DETECTION_KEY
        logged_out = client.post("/api/account/logout")
        assert logged_out.status_code == 200
        assert IMAGE_KEY not in logged_out.text
        assert "image_relay_api_key" not in manager._settings

        class LoginRelay:
            def __init__(self):
                self.created: list[str] = []

            async def login(self, email, password):
                return {
                    "access_token": "access-token-from-login-0001",
                    "refresh_token": "refresh-token-from-login-0001",
                    "expires_in": 3600,
                    "user": {"email": email, "balance": 1},
                }

            async def list_groups(self, token):
                return [{"id": 4, "name": "电商审核专属-bot创建", "status": "active"}]

            async def list_keys(self, token):
                return []

            async def create_key(self, token, group_id):
                self.created.append(KEY_NAME)
                return {"id": 9, "name": KEY_NAME, "status": "active", "group_id": group_id, "key": DETECTION_KEY}

            async def usage(self, token):
                return {}

            async def create_key_named(self, token, name, group_id):
                raise AssertionError("switching accounts must not create an image key")

        relay = LoginRelay()
        manager.relay = relay
        manager._settings["relay_email"] = "first@example.com"
        manager._settings["relay_access_token"] = "access-token-for-tests"
        manager._settings["image_relay_api_key"] = IMAGE_KEY
        switched = client.post("/api/account/login", json={"email": "second@example.com", "password": "secret"})
        assert switched.status_code == 200, switched.text
        assert IMAGE_KEY not in switched.text
        assert "image_relay_api_key" not in manager._settings
        assert manager._settings["relay_api_key"] == DETECTION_KEY
        assert relay.created == [KEY_NAME]


def test_history_keeps_only_the_newest_batches(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "mock")
    monkeypatch.setattr("app.services.image_jobs.MAX_BATCHES", 2)
    with TestClient(create_app()) as client:
        sign_in()
        ids = []
        for index in range(3):
            response = client.post("/api/images/generations", json={"prompt": f"第{index}张", "count": 1})
            assert response.status_code == 200, response.text
            ids.append(response.json()["id"])
        listed = client.get("/api/images/batches").json()
        assert [item["id"] for item in listed] == [ids[2], ids[1]]
        assert client.get(f"/api/images/batches/{ids[0]}").status_code == 404


def test_second_submit_is_rejected_while_one_is_running(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "mock")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(owner, local, references):
        started.set()
        await release.wait()
        return {"id": "held", "status": "running", "preview": True, "files": [], "references": []}

    monkeypatch.setattr("app.services.image_jobs._run_mock", slow)

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            manager = get_manager()
            manager._settings["relay_email"] = "user@example.com"
            manager._settings["relay_access_token"] = "access-token-for-tests"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = asyncio.create_task(
                    client.post("/api/images/generations", json={"prompt": "第一张"})
                )
                await asyncio.wait_for(started.wait(), timeout=2)
                second = await client.post("/api/images/generations", json={"prompt": "第二张"})
                assert second.status_code == 409
                assert "进行中" in second.text
                release.set()
                done = await first
                assert done.status_code == 200

    asyncio.run(scenario())


def test_provider_named_chuanapi_stays_disabled(monkeypatch):
    monkeypatch.setenv("EVC_IMAGE_PROVIDER", "chuanapi")
    with TestClient(create_app()) as client:
        sign_in()
        body = client.get("/api/images/status").json()
        assert body["mode"] == "unconfigured"
        assert body["enabled"] is False


def teardown_module():
    reset_image_contract()
