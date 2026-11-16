import json

import httpx
from fastapi.testclient import TestClient

from app.core.deepseek import chat_url
from app.core.relay import (
    CHAT_BASE,
    DEFAULT_RELAY_GROUP_NAME,
    DEEPSEEK_FLASH_MODEL,
    RelayClient,
    RelayError,
    explain_error,
    group_unavailable_message,
    resolve_relay_group_id,
    usable_api_key,
)
from app.factory import create_app
from app.logging_setup import redact
from app.services.manager import JobState, get_manager

EMAIL = "user@example.com"
PASSWORD = "local-pass-1"
ACCESS = "access-token-value-not-a-jwt"
REFRESH = "refresh-token-value-hidden"
TEMP = "temp-2fa-session-secret"
RELAY_KEY = "sk-relay-account-key-4242"
SAVED_KEY = "sk-saved-4321"
OLD_KEY = "sk-old-group-key-1414"
TARGET_GROUP_ID = 21
OLD_GROUP_ID = 14


def envelope(data: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"code": 0 if status < 400 else status, "message": "success", "data": data})


def failure(status: int, reason: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"code": status, "message": message, "reason": reason})


class Upstream:
    def __init__(self) -> None:
        self.creates = 0
        self.create_body: dict | None = None
        self.create_key_header = ""
        self.group_updates: list[dict] = []
        self.updated_key_ids: list[str] = []
        self.verbs: list[str] = []
        self.deletes = 0
        self.list_mode = "empty"
        self.groups_status = 200
        self.created_group_override: int | None = None
        self.created_items: list[dict] = []
        self.create_failure: tuple[int, str, str] | None = None
        self.groups = [
            {"id": OLD_GROUP_ID, "name": "国产模型-bot创建", "status": "active", "is_exclusive": False},
            {
                "id": TARGET_GROUP_ID,
                "name": DEFAULT_RELAY_GROUP_NAME,
                "status": "active",
                "is_exclusive": True,
                "subscription_type": "standard",
                "rate_multiplier": 1.8,
            },
        ]
        self.login_mode = "ok"
        self.profile_unauthorized = False
        self.forwarded_temp = False
        self.calls: list[tuple[str, dict]] = []
        self.payment_disabled = False
        self.created_orders: list[dict] = []
        self.order_status: dict[str, str] = {}
        self.list_query: dict[str, str] = {}
        self.public = {
            "registration_enabled": True,
            "email_verify_enabled": False,
            "turnstile_enabled": False,
            "tencent_captcha_enabled": False,
            "aliyun_captcha_enabled": False,
            "backend_mode_enabled": False,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict = {}
        if request.content:
            body = json.loads(request.content.decode())
        self.calls.append((path, body))
        self.verbs.append(request.method)
        if request.method == "DELETE":
            self.deletes += 1
            return failure(405, "NOT_ALLOWED", "delete is not used")
        if path.endswith("/settings/public"):
            return envelope(self.public)
        if path.endswith("/auth/send-verify-code"):
            return envelope({"message": "sent", "countdown": 60})
        if path.endswith("/auth/register"):
            return envelope(self._session())
        if path.endswith("/auth/login"):
            if self.login_mode == "bad":
                return failure(401, "INVALID_CREDENTIALS", "invalid email or password")
            if self.login_mode == "twofa":
                return envelope({"requires_2fa": True, "temp_token": TEMP, "user_email_masked": "u***@example.com"})
            return envelope(self._session())
        if path.endswith("/auth/login/2fa"):
            self.forwarded_temp = body.get("temp_token") == TEMP and body.get("totp_code") == "123456"
            return envelope(self._session())
        if path.endswith("/auth/refresh"):
            return envelope({"access_token": "rotated-access-token", "refresh_token": "rotated-refresh-token", "expires_in": 3600})
        if path.endswith("/auth/logout"):
            return envelope({"message": "ok"})
        if path.endswith("/user/profile"):
            if self.profile_unauthorized:
                self.profile_unauthorized = False
                return failure(401, "ACCESS_TOKEN_EXPIRED", "access token has expired")
            return envelope({"email": EMAIL, "balance": 8.5, "frozen_balance": 0.1})
        if path.endswith("/usage/dashboard/stats"):
            return envelope({"today_actual_cost": 0.125, "total_actual_cost": 3.5})
        if path.endswith("/groups/available") and request.method == "GET":
            if self.groups_status >= 400:
                return failure(self.groups_status, "UPSTREAM", "groups unavailable")
            return envelope(self.groups)
        if path.endswith("/keys") and request.method == "GET":
            return envelope({"items": self._keys(), "total": 1, "page": 1, "page_size": 100, "pages": 1})
        if path.endswith("/keys") and request.method == "POST":
            self.creates += 1
            self.create_body = body
            self.create_key_header = request.headers.get("idempotency-key", "")
            if self.create_failure is not None:
                status, reason, message = self.create_failure
                return failure(status, reason, message)
            group_id = self.created_group_override if self.created_group_override is not None else body.get("group_id")
            created = {
                "id": 9,
                "name": "电商视频合规",
                "key": RELAY_KEY,
                "status": "active",
                "ip_whitelist": [],
                "group_id": group_id,
            }
            self.created_items.append(created)
            return envelope(created)
        if request.method == "PUT" and "/keys/" in path:
            self.group_updates.append(body)
            self.updated_key_ids.append(path.rstrip("/").split("/")[-1])
            return envelope({"id": 9, "name": "电商视频合规", "status": "active", "group_id": body.get("group_id")})
        if path.endswith("/payment/checkout-info"):
            if self.payment_disabled:
                return failure(403, "PAYMENT_DISABLED", "payment system is disabled")
            return envelope(self._checkout())
        if path.endswith("/payment/orders/my") and request.method == "GET":
            self.list_query = dict(request.url.params)
            return envelope(
                {
                    "items": [
                        {
                            "id": 42,
                            "user_id": 7,
                            "amount": 20,
                            "pay_amount": 20.12,
                            "fee_rate": 0.6,
                            "bonus_amount": 2,
                            "currency": "CNY",
                            "payment_type": "alipay",
                            "out_trade_no": "trade-secret",
                            "provider_instance_id": "instance-secret",
                            "status": "COMPLETED",
                            "order_type": "balance",
                            "created_at": "2026-11-13T11:00:00Z",
                            "expires_at": "2026-11-13T12:00:00Z",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 10,
                    "pages": 1,
                }
            )
        if request.method == "POST" and path.endswith("/cancel") and "/payment/orders/" in path:
            order_id = path.rstrip("/").split("/")[-2]
            self.order_status[order_id] = "CANCELLED"
            return envelope({"message": "cancelled"})
        if request.method == "GET" and "/payment/orders/" in path:
            order_id = path.rstrip("/").split("/")[-1]
            if not order_id.isdigit():
                return failure(404, "NOT_FOUND", "missing")
            return envelope(self._order(int(order_id), self.order_status.get(order_id, "PENDING")))
        if path.endswith("/payment/orders") and request.method == "POST":
            self.created_orders.append(body)
            if body.get("payment_type") == "wxpay":
                return envelope(
                    {
                        "order_id": 43,
                        "amount": body.get("amount"),
                        "pay_amount": body.get("amount"),
                        "status": "PENDING",
                        "result_type": "oauth_required",
                        "payment_type": "wxpay",
                        "client_secret": "cs_secret_value",
                        "resume_token": "resume-secret",
                        "oauth": {"authorize_url": "https://token4u.cc/oauth?token=secret-oauth"},
                        "jsapi": {"paySign": "sign-secret"},
                        "expires_at": "2026-11-13T12:00:00Z",
                    }
                )
            return envelope(
                {
                    "order_id": 42,
                    "amount": 20,
                    "pay_amount": 20.12,
                    "fee_rate": 0.6,
                    "bonus_amount": 2,
                    "status": "PENDING",
                    "result_type": "order_created",
                    "payment_type": "alipay",
                    "pay_url": "https://pay.example.com/checkout",
                    "qr_code": "https://qr.alipay.com/fake",
                    "client_secret": "cs_secret_value",
                    "resume_token": "resume-secret",
                    "out_trade_no": "trade-secret",
                    "expires_at": "2026-11-13T12:00:00Z",
                }
            )
        return failure(404, "NOT_FOUND", "missing")

    def _checkout(self) -> dict:
        return {
            "methods": {
                "alipay": {
                    "payment_type": "alipay",
                    "display_name": "支付宝",
                    "currency": "CNY",
                    "fee_rate": 0.6,
                    "daily_limit": 5000,
                    "single_min": 1,
                    "single_max": 2000,
                },
                "wxpay": {"payment_type": "wxpay", "currency": "CNY", "fee_rate": 0, "single_min": 0, "single_max": 0},
            },
            "global_min": 0,
            "global_max": 0,
            "plans": [{"id": 1, "name": "hidden plan"}],
            "balance_disabled": False,
            "balance_recharge_multiplier": 1,
            "recharge_fee_rate": 0,
            "recharge_bonus_tiers": [{"min_amount": 50, "bonus_percent": 5}, {"min_amount": 100, "bonus_percent": 10}],
            "recharge_bonus_mode": "bonus",
            "recharge_bonus_notice": "满额赠送",
            "help_text": "支付后余额自动到账",
            "help_image_url": "https://token4u.cc/secret.png",
            "stripe_publishable_key": "pk_live_should_not_leak",
        }

    def _order(self, order_id: int, status: str) -> dict:
        return {
            "id": order_id,
            "user_id": 7,
            "amount": 20,
            "pay_amount": 20.12,
            "fee_rate": 0.6,
            "bonus_amount": 2,
            "currency": "CNY",
            "payment_type": "alipay",
            "out_trade_no": "trade-secret",
            "provider_instance_id": "instance-secret",
            "status": status,
            "order_type": "balance",
            "created_at": "2026-11-13T11:00:00Z",
            "expires_at": "2026-11-13T12:00:00Z",
        }

    def _session(self) -> dict:
        return {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "expires_in": 3600,
            "token_type": "Bearer",
            "user": {"email": EMAIL, "balance": 12.5, "frozen_balance": 0.25},
        }

    def _keys(self) -> list[dict]:
        named = {"name": "电商视频合规", "status": "active", "ip_whitelist": []}
        if self.list_mode == "full":
            base = [{**named, "id": 9, "key": RELAY_KEY, "group_id": TARGET_GROUP_ID}]
        elif self.list_mode == "masked":
            base = [{**named, "id": 9, "key": "****4242", "group_id": TARGET_GROUP_ID}]
        elif self.list_mode == "whitelist":
            base = [
                {
                    **named,
                    "id": 3,
                    "key": RELAY_KEY,
                    "group_id": TARGET_GROUP_ID,
                    "ip_whitelist": ["203.0.113.8"],
                }
            ]
        elif self.list_mode == "legacy":
            base = [{**named, "id": 3, "key": OLD_KEY, "group_id": OLD_GROUP_ID}]
        elif self.list_mode == "both":
            base = [
                {**named, "id": 3, "key": OLD_KEY, "group_id": OLD_GROUP_ID},
                {**named, "id": 8, "key": RELAY_KEY, "group_id": TARGET_GROUP_ID},
            ]
        else:
            base = []
        seen = {item.get("id") for item in base}
        return base + [item for item in self.created_items if item.get("id") not in seen]


def attach(upstream: Upstream) -> None:
    get_manager().relay = RelayClient(transport=httpx.MockTransport(upstream))


def login(client: TestClient) -> dict:
    response = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def queued_state() -> JobState:
    return JobState(
        id="a" * 32,
        filename="素材.mp4",
        source_path="素材.mp4",
        status="queued",
        stage="queued",
        message="",
        progress=0,
        fps=1,
        concurrency=1,
        duration_sec=1,
        frames_estimated=0,
        frames_extracted=0,
        frames_recognized=0,
        failed_frames=0,
        hit_count=0,
        error=None,
        speech_stage="",
        speech_message="",
        asr_error=None,
        options={"base_url": "https://api.deepseek.com", "model": "deepseek-v4.1-flash"},
        output_path=None,
        created_at="",
        updated_at="",
        finished_at=None,
    )


def test_frozen_app_points_tls_at_the_bundled_certificate(monkeypatch, tmp_path):
    import os
    import sys

    from app.core.relay import configure_tls

    cafile = tmp_path / "certifi" / "cacert.pem"
    cafile.parent.mkdir()
    cafile.write_text("bundle", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    configure_tls()
    assert os.environ["SSL_CERT_FILE"] == str(cafile)


def test_explain_error_uses_chinese_and_hides_secrets():
    error = explain_error(401, {"reason": "INVALID_CREDENTIALS", "message": "invalid email or password"})
    assert str(error) == "邮箱或密码不正确"
    assert error.status == 400
    leaked = explain_error(400, {"message": "密钥 sk-super-secret-value 无效"})
    assert "sk-super-secret-value" not in str(leaked)
    assert usable_api_key("****4242") is False
    assert usable_api_key(RELAY_KEY) is True
    denied = explain_error(403, {"reason": "GROUP_NOT_ALLOWED", "message": "user is not allowed to bind this group"})
    assert denied.status == 403
    assert str(denied) == "当前账号还不能识别画面，请联系管理员开通。"
    assert "sk-" not in str(denied)
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturevalue"
    assert token not in redact(f"header {token}")


def test_login_pins_picture_calls_to_the_account_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    upstream = Upstream()
    with TestClient(create_app()) as client:
        attach(upstream)
        saved = client.put(
            "/api/settings",
            json={
                "api_key": SAVED_KEY,
                "model": "deepseek-v4.1-flash",
                "base_url": "https://example.invalid",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["model"] == DEEPSEEK_FLASH_MODEL
        assert saved.json()["base_url"] == "https://api.deepseek.com"
        body = login(client)
        settings = body["settings"]
        assert settings["account_logged_in"] is True
        assert settings["account_email"] == EMAIL
        assert settings["account_balance"] == 12.5
        assert settings["account_frozen_balance"] == 0.25
        assert settings["account_today_cost"] == 0.125
        assert settings["account_total_cost"] == 3.5
        assert settings["api_key_source"] == "account"
        assert settings["api_key_masked"].endswith("4242")
        assert settings["base_url"] == "https://api.deepseek.com"
        assert settings["model"] == DEEPSEEK_FLASH_MODEL
        assert settings["has_api_key"] is True
        text = json.dumps(body, ensure_ascii=False)
        for secret in (PASSWORD, ACCESS, REFRESH, RELAY_KEY, SAVED_KEY):
            assert secret not in text
        assert upstream.creates == 1
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert upstream.group_updates == []
        assert upstream.deletes == 0
        assert any(path.endswith("/groups/available") for path, _ in upstream.calls)
        assert upstream.create_key_header
        config = get_manager()._config_for(queued_state())
        assert config.api_key == RELAY_KEY
        assert config.base_url == CHAT_BASE
        assert chat_url(config.base_url) == "https://token4u.cc/v1/chat/completions"
        assert config.model == DEEPSEEK_FLASH_MODEL
        assert settings["model"] == DEEPSEEK_FLASH_MODEL
        kept = client.put("/api/settings", json={"fps": 1.5})
        assert kept.json()["account_logged_in"] is True
        assert kept.json()["api_key_source"] == "account"
        assert kept.json()["fps"] == 1.5
        assert ACCESS not in kept.text
        cleared = client.put("/api/settings", json={"clear_api_key": True})
        assert cleared.json()["api_key_source"] == "account"
        upstream.list_mode = "masked"
        again = login(client)
        assert again["settings"]["api_key_source"] == "account"
        assert upstream.creates == 1
        assert get_manager()._settings["relay_api_key"] == RELAY_KEY
        logged_out = client.post("/api/account/logout")
        assert logged_out.status_code == 200
        logged_out_settings = logged_out.json()["settings"]
        assert logged_out_settings["account_logged_in"] is False
        assert logged_out_settings["api_key_source"] == "none"
        assert logged_out_settings["model"] == DEEPSEEK_FLASH_MODEL
        stored = json.dumps(get_manager()._settings, ensure_ascii=False)
        assert ACCESS not in stored
        assert RELAY_KEY not in stored
        assert PASSWORD not in stored
        fallback = get_manager()._config_for(queued_state())
        assert fallback.api_key == ""
        assert fallback.base_url == "https://api.deepseek.com"
        assert fallback.model == DEEPSEEK_FLASH_MODEL


def test_existing_named_key_is_reused_without_a_whitelist():
    upstream = Upstream()
    upstream.list_mode = "full"
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["api_key_source"] == "account"
        assert upstream.creates == 0
        assert upstream.group_updates == []
        assert upstream.deletes == 0
        upstream.list_mode = "whitelist"
        client.post("/api/account/logout")
        created = login(client)
        assert created["settings"]["api_key_masked"].endswith("4242")
        assert upstream.creates == 1
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert "ip_whitelist" not in upstream.create_body
        assert upstream.deletes == 0


def test_resolve_relay_group_id_prefers_the_exclusive_name():
    groups = [
        {"id": OLD_GROUP_ID, "name": "国产模型-bot创建", "status": "active"},
        {"id": str(TARGET_GROUP_ID), "name": f"  {DEFAULT_RELAY_GROUP_NAME}  ", "status": "active"},
        {"id": 30, "name": DEFAULT_RELAY_GROUP_NAME, "status": "inactive"},
    ]
    assert resolve_relay_group_id(groups, name=DEFAULT_RELAY_GROUP_NAME, fallback_id=OLD_GROUP_ID) == TARGET_GROUP_ID
    assert resolve_relay_group_id(groups, name="国产模型-bot创建", fallback_id=None) == OLD_GROUP_ID
    try:
        resolve_relay_group_id(
            [{"id": 30, "name": DEFAULT_RELAY_GROUP_NAME, "status": "inactive"}],
            name=DEFAULT_RELAY_GROUP_NAME,
            fallback_id=99,
        )
    except RelayError as exc:
        assert exc.status == 403
        assert str(exc) == "当前账号还不能识别画面，请联系管理员开通。"
        detail = group_unavailable_message(DEFAULT_RELAY_GROUP_NAME, 99)
        assert "不会改用其他分组" in detail
        assert "99" in detail
    else:
        raise AssertionError("missing group should fail")
    try:
        resolve_relay_group_id(
            [
                {"id": 7, "name": DEFAULT_RELAY_GROUP_NAME, "status": "active"},
                {"id": 8, "name": DEFAULT_RELAY_GROUP_NAME, "status": "active"},
            ],
            name=DEFAULT_RELAY_GROUP_NAME,
            fallback_id=None,
        )
    except RelayError as exc:
        assert "多个" in str(exc)
    else:
        raise AssertionError("duplicate names should fail")


def test_existing_key_in_another_group_is_left_alone():
    upstream = Upstream()
    upstream.list_mode = "legacy"
    with TestClient(create_app()) as client:
        attach(upstream)
        manager = get_manager()
        manager._settings["relay_email"] = EMAIL
        manager._settings["relay_api_key"] = OLD_KEY
        manager._settings["relay_api_key_id"] = 3
        body = login(client)
        assert body["settings"]["api_key_source"] == "account"
        assert body["settings"]["api_key_masked"].endswith("4242")
        assert upstream.creates == 1
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert upstream.group_updates == []
        assert upstream.deletes == 0
        assert "DELETE" not in upstream.verbs
        assert manager._settings["relay_api_key"] == RELAY_KEY
        assert manager._settings["relay_api_key_id"] == 9
        again = login(client)
        assert again["settings"]["api_key_source"] == "account"
        assert upstream.creates == 1
        assert upstream.deletes == 0


def test_key_already_in_the_target_group_is_reused():
    upstream = Upstream()
    upstream.list_mode = "both"
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["api_key_masked"].endswith("4242")
        assert upstream.creates == 0
        assert upstream.group_updates == []
        assert upstream.updated_key_ids == []
        assert upstream.deletes == 0
        assert get_manager()._settings["relay_api_key"] == RELAY_KEY
        assert get_manager()._settings["relay_api_key_id"] == 8


def test_new_key_group_is_corrected_without_touching_other_keys():
    upstream = Upstream()
    upstream.list_mode = "legacy"
    upstream.created_group_override = OLD_GROUP_ID
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["api_key_source"] == "account"
        assert upstream.creates == 1
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert upstream.group_updates == [{"group_id": TARGET_GROUP_ID}]
        assert upstream.updated_key_ids == ["9"]
        assert upstream.deletes == 0


def test_group_not_allowed_on_create_keeps_existing_keys(monkeypatch):
    monkeypatch.delenv("EVC_RELAY_GROUP_ID", raising=False)
    upstream = Upstream()
    upstream.create_failure = (403, "GROUP_NOT_ALLOWED", "user is not allowed to bind this group")
    with TestClient(create_app()) as client:
        attach(upstream)
        manager = get_manager()
        manager._settings["relay_email"] = EMAIL
        manager._settings["relay_api_key"] = OLD_KEY
        manager._settings["relay_api_key_id"] = 3
        body = login(client)
        assert body["settings"]["account_logged_in"] is True
        assert body["settings"]["has_api_key"] is False
        assert body["message"] == "已登录。当前账号还不能识别画面，请联系管理员开通。"
        assert "不会改用其他分组" not in body["message"]
        assert upstream.creates == 1
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert upstream.group_updates == []
        assert upstream.deletes == 0
        assert manager._settings.get("relay_api_key") in (None, "")
        assert OLD_KEY not in body["message"]


def test_missing_exclusive_group_is_a_clear_error_and_creates_nothing(monkeypatch):
    monkeypatch.delenv("EVC_RELAY_GROUP_ID", raising=False)
    upstream = Upstream()
    upstream.groups = [{"id": OLD_GROUP_ID, "name": "国产模型-bot创建", "status": "active", "is_exclusive": False}]
    with TestClient(create_app()) as client:
        attach(upstream)
        manager = get_manager()
        manager._settings["relay_email"] = EMAIL
        manager._settings["relay_api_key"] = OLD_KEY
        manager._settings["relay_api_key_id"] = 3
        body = login(client)
        assert body["settings"]["account_logged_in"] is True
        assert body["settings"]["has_api_key"] is False
        assert body["settings"]["api_key_source"] == "none"
        assert body["message"] == "已登录。当前账号还不能识别画面，请联系管理员开通。"
        assert DEFAULT_RELAY_GROUP_NAME not in body["message"]
        assert "不会改用其他分组" not in body["message"]
        assert upstream.creates == 0
        assert upstream.group_updates == []
        assert upstream.deletes == 0
        assert "DELETE" not in upstream.verbs
        assert manager._settings.get("relay_api_key") in (None, "")
        assert OLD_KEY not in json.dumps(body, ensure_ascii=False)


def test_configured_group_id_does_not_override_a_resolved_name(monkeypatch):
    monkeypatch.setenv("EVC_RELAY_GROUP_ID", str(OLD_GROUP_ID))
    upstream = Upstream()
    with TestClient(create_app()) as client:
        attach(upstream)
        login(client)
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert upstream.deletes == 0


def test_configured_group_id_is_used_when_the_name_is_not_listed(monkeypatch):
    monkeypatch.setenv("EVC_RELAY_GROUP_NAME", "不存在的分组")
    monkeypatch.setenv("EVC_RELAY_GROUP_ID", str(OLD_GROUP_ID))
    upstream = Upstream()
    upstream.groups = [{"id": OLD_GROUP_ID, "name": "国产模型-bot创建", "status": "active"}]
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["api_key_source"] == "account"
        assert upstream.create_body == {"name": "电商视频合规", "group_id": OLD_GROUP_ID}
        assert upstream.deletes == 0


def test_configured_group_id_that_is_not_available_does_not_create_a_key(monkeypatch):
    monkeypatch.setenv("EVC_RELAY_GROUP_ID", "99")
    upstream = Upstream()
    upstream.groups = [{"id": OLD_GROUP_ID, "name": "国产模型-bot创建", "status": "active"}]
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["has_api_key"] is False
        assert body["message"] == "已登录。当前账号还不能识别画面，请联系管理员开通。"
        assert "99" not in body["message"]
        assert upstream.creates == 0
        assert upstream.deletes == 0


def test_group_lookup_failure_can_use_a_numeric_fallback(monkeypatch):
    monkeypatch.setenv("EVC_RELAY_GROUP_ID", str(TARGET_GROUP_ID))
    upstream = Upstream()
    upstream.groups_status = 500
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["api_key_source"] == "account"
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}


def test_group_lookup_failure_without_a_fallback_does_not_create_a_key():
    upstream = Upstream()
    upstream.groups_status = 500
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert body["settings"]["has_api_key"] is False
        assert body["message"] == "已登录。暂时无法确认账号权限，请稍后再试。"
        assert DEFAULT_RELAY_GROUP_NAME not in body["message"]
        assert upstream.creates == 0
        assert upstream.deletes == 0


def test_invalid_group_id_is_reported(monkeypatch):
    monkeypatch.setenv("EVC_RELAY_GROUP_ID", "abc")
    upstream = Upstream()
    with TestClient(create_app()) as client:
        attach(upstream)
        body = login(client)
        assert "正整数" in body["message"]
        assert upstream.creates == 0
        assert upstream.deletes == 0


def test_refresh_creates_a_target_group_key_when_the_saved_one_is_missing():
    upstream = Upstream()
    upstream.list_mode = "full"
    with TestClient(create_app()) as client:
        attach(upstream)
        login(client)
        assert upstream.creates == 0
        get_manager()._settings.pop("relay_api_key", None)
        get_manager()._settings.pop("relay_api_key_id", None)
        upstream.list_mode = "legacy"
        refreshed = client.post("/api/account/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["settings"]["api_key_source"] == "account"
        assert upstream.creates == 1
        assert upstream.create_body == {"name": "电商视频合规", "group_id": TARGET_GROUP_ID}
        assert upstream.group_updates == []
        assert upstream.deletes == 0
        assert OLD_KEY not in refreshed.text


def test_bad_login_and_two_factor_do_not_store_a_session():
    upstream = Upstream()
    upstream.login_mode = "bad"
    with TestClient(create_app()) as client:
        attach(upstream)
        rejected = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "邮箱或密码不正确"
        assert client.get("/api/settings").json()["account_logged_in"] is False
        early = client.post("/api/account/login/2fa", json={"totp_code": "123456"})
        assert early.status_code == 400
        assert early.json()["detail"] == "请先登录，再填写验证码"
        upstream.login_mode = "twofa"
        challenge = client.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        assert challenge.status_code == 200, challenge.text
        assert challenge.json()["requires_2fa"] is True
        assert challenge.json()["settings"]["account_logged_in"] is False
        assert TEMP not in challenge.text
        assert get_manager()._settings.get("relay_access_token") in (None, "")
        done = client.post("/api/account/login/2fa", json={"totp_code": "123456"})
        assert done.status_code == 200, done.text
        assert done.json()["settings"]["account_logged_in"] is True
        assert upstream.forwarded_temp is True
        assert TEMP not in done.text
        assert ACCESS not in done.text


def test_register_send_code_and_public_options():
    upstream = Upstream()
    upstream.public = {
        "registration_enabled": False,
        "email_verify_enabled": True,
        "turnstile_enabled": True,
        "backend_mode_enabled": True,
    }
    with TestClient(create_app()) as client:
        attach(upstream)
        options = client.get("/api/account/options")
        assert options.status_code == 200
        body = options.json()
        assert body["reachable"] is True
        assert body["registration_enabled"] is False
        assert body["email_verify_enabled"] is True
        assert body["captcha"] == "turnstile"
        assert "人机验证" in body["message"]
        assert "后台模式" in body["message"]
        sent = client.post("/api/account/send-code", json={"email": EMAIL})
        assert sent.status_code == 200, sent.text
        assert "60" in sent.json()["message"]
        registered = client.post(
            "/api/account/register",
            json={"email": EMAIL, "password": PASSWORD, "verify_code": "889900"},
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["settings"]["account_logged_in"] is True
        register_body = next(item for path, item in upstream.calls if path.endswith("/auth/register"))
        assert register_body["verify_code"] == "889900"
        short = client.post("/api/account/register", json={"email": EMAIL, "password": "12345"})
        assert short.status_code == 400
        assert short.json()["detail"] == "密码至少 6 位"

        def offline(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        get_manager().relay = RelayClient(transport=httpx.MockTransport(offline))
        failed = client.get("/api/account/options")
        assert failed.status_code == 200
        assert failed.json()["reachable"] is False
        assert "token4u.cc" in failed.json()["message"]


def test_refresh_rotates_an_expired_session_and_updates_balance():
    upstream = Upstream()
    upstream.list_mode = "full"
    with TestClient(create_app()) as client:
        attach(upstream)
        login(client)
        upstream.profile_unauthorized = True
        refreshed = client.post("/api/account/refresh")
        assert refreshed.status_code == 200, refreshed.text
        settings = refreshed.json()["settings"]
        assert settings["account_balance"] == 8.5
        assert settings["account_frozen_balance"] == 0.1
        assert "rotated-access-token" not in refreshed.text
        assert get_manager()._settings["relay_access_token"] == "rotated-access-token"
        assert get_manager()._settings["relay_refresh_token"] == "rotated-refresh-token"


def test_recharge_uses_the_relay_balance_checkout_and_hides_payment_secrets():
    upstream = Upstream()
    with TestClient(create_app()) as client:
        attach(upstream)
        blocked = client.get("/api/account/recharge")
        assert blocked.status_code == 401
        assert blocked.json()["detail"] == "请先登录"
        login(client)

        upstream.payment_disabled = True
        denied = client.get("/api/account/recharge")
        assert denied.status_code == 403
        assert denied.json()["detail"] == "站点暂时关闭了充值"
        upstream.payment_disabled = False

        info = client.get("/api/account/recharge")
        assert info.status_code == 200, info.text
        body = info.json()
        assert body["balance_disabled"] is False
        assert body["bonus_mode"] == "bonus"
        assert body["bonus_tiers"] == [{"min_amount": 50.0, "bonus_percent": 5.0}, {"min_amount": 100.0, "bonus_percent": 10.0}]
        assert {item["payment_type"] for item in body["methods"]} == {"alipay", "wxpay"}
        wxpay = next(item for item in body["methods"] if item["payment_type"] == "wxpay")
        assert wxpay["display_name"] == "微信支付"
        assert "pk_live_should_not_leak" not in info.text
        assert "hidden plan" not in info.text
        assert "secret.png" not in info.text

        rejected = client.post("/api/account/recharge/orders", json={"amount": 20, "payment_type": "card"})
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "请选择页面上的一种支付方式"
        low = client.post("/api/account/recharge/orders", json={"amount": 0.5, "payment_type": "alipay"})
        assert low.status_code == 400
        assert low.json()["detail"] == "单笔最少 1 元"

        created = client.post("/api/account/recharge/orders", json={"amount": 20, "payment_type": "alipay"})
        assert created.status_code == 200, created.text
        paid = created.json()
        assert paid["order_id"] == 42
        assert paid["pay_url"] == "https://pay.example.com/checkout"
        assert paid["qr_code"] == "https://qr.alipay.com/fake"
        assert paid["settings"] is None
        assert "cs_secret_value" not in created.text
        assert "resume-secret" not in created.text
        assert "trade-secret" not in created.text
        assert upstream.created_orders[-1] == {
            "amount": 20.0,
            "payment_type": "alipay",
            "order_type": "balance",
            "is_mobile": False,
        }

        wechat = client.post("/api/account/recharge/orders", json={"amount": 20, "payment_type": "wxpay"})
        assert wechat.status_code == 200, wechat.text
        assert "扫码" in wechat.json()["message"]
        assert "secret-oauth" not in wechat.text
        assert "cs_secret_value" not in wechat.text
        assert "sign-secret" not in wechat.text

        upstream.order_status["42"] = "COMPLETED"
        fetched = client.get("/api/account/recharge/orders/42")
        assert fetched.status_code == 200, fetched.text
        done = fetched.json()
        assert done["status"] == "COMPLETED"
        assert done["settings"]["account_balance"] == 8.5
        assert "trade-secret" not in fetched.text
        assert "instance-secret" not in fetched.text
        assert "user_id" not in fetched.text

        listed = client.get("/api/account/recharge/orders")
        assert listed.status_code == 200, listed.text
        assert listed.json()["orders"][0]["order_id"] == 42
        assert listed.json()["orders"][0]["status"] == "COMPLETED"
        assert "trade-secret" not in listed.text
        assert "user_id" not in listed.text
        assert upstream.list_query["order_type"] == "balance"
        assert upstream.list_query["page_size"] == "10"

        cancelled = client.post("/api/account/recharge/orders/7/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"
        assert cancelled.json()["message"] == ""
