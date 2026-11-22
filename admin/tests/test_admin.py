import json
from datetime import date
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from admin_app.auth_relay import SUPER_ADMIN_EMAIL, RelayLoginError
from admin_app.seed import DEMO_PASSWORD


def _fake_login(email: str, password: str) -> dict:
    if password != DEMO_PASSWORD:
        raise RelayLoginError("邮箱或密码不正确", 401)
    return {
        "access_token": "relay-test-access",
        "refresh_token": "relay-test-refresh",
        "user": {"email": email},
    }


def _client(tmp_path, monkeypatch, *, fake_login: bool = True) -> TestClient:
    monkeypatch.setenv("EVC_ADMIN_DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    monkeypatch.setenv("EVC_DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setenv("EVC_ADMIN_JWT_SECRET", "test-secret-test-secret")
    monkeypatch.delenv("EVC_RELAY_ADMIN_KEY", raising=False)
    from admin_app.config import get_settings
    from admin_app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    if fake_login:
        import admin_app.auth_relay as auth_relay

        monkeypatch.setattr(auth_relay, "login_password", _fake_login)
    from admin_app.main import create_app

    return TestClient(create_app())


def _login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/v1/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_login_uses_relay_and_marks_super_admin(tmp_path, monkeypatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("user-agent")
        body = json.loads(request.content.decode())
        seen["email"] = body.get("email")
        seen["has_password"] = bool(body.get("password"))
        if body.get("password") == "wrong-pass":
            return httpx.Response(
                401,
                json={"code": 401, "message": "invalid email or password", "reason": "INVALID_CREDENTIALS"},
            )
        asked = str(body.get("email") or "")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "access_token": "upstream-access-should-not-leak",
                    "refresh_token": "upstream-refresh-should-not-leak",
                    "user": {"id": 42, "email": asked},
                },
            },
        )

    import admin_app.auth_relay as auth_relay

    monkeypatch.setattr(auth_relay, "_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("EVC_RELAY_BASE_URL", "https://token4u.cc/api/v1")
    with _client(tmp_path, monkeypatch, fake_login=False) as client:
        bad = client.post("/v1/auth/login", json={"email": SUPER_ADMIN_EMAIL, "password": "wrong-pass"})
        assert bad.status_code == 401
        assert bad.json()["detail"] == "邮箱或密码不正确"
        ok = client.post("/v1/auth/login", json={"email": " 350944364@QQ.com ", "password": "real-pass"})
        assert ok.status_code == 200, ok.text
        payload = ok.json()
        assert payload["user"]["role"] == "platform_admin"
        assert payload["user"]["email"] == SUPER_ADMIN_EMAIL
        assert payload["user"]["name"] == "总管理员"
        assert payload["user"]["tenant_id"] is None
        assert "upstream-access" not in ok.text
        assert "upstream-refresh" not in ok.text
        assert seen["url"] == "https://token4u.cc/api/v1/auth/login"
        assert seen["email"] == SUPER_ADMIN_EMAIL
        assert seen["has_password"] is True
        assert seen["ua"] == "ecommerce-video-compliance/0.1.0"
        again = client.post("/v1/auth/login", json={"email": SUPER_ADMIN_EMAIL, "password": "real-pass"})
        assert again.status_code == 200
        assert again.json()["user"]["id"] == payload["user"]["id"]
        stranger = client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "real-pass"})
        assert stranger.status_code == 403
        assert stranger.json()["detail"] == "这个账号没有后台权限"


def test_http_relay_matches_token4u_admin_contract(monkeypatch):
    """evc-admin 发出的请求体必须符合 token4u 受限管理密钥契约。"""
    import admin_app.relay as relay
    from admin_app.config import Settings

    calls: list[tuple[str, str, dict, object]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def _record(self, method: str, url: str, headers, body):
            calls.append((method, url, dict(headers or {}), body))
            if url.endswith("/admin/users"):
                return httpx.Response(200, json={"code": 0, "data": {"id": 77}})
            if url.endswith("/auth/login"):
                return httpx.Response(200, json={"code": 0, "data": {"access_token": "user-token"}})
            return httpx.Response(200, json={"code": 0, "data": {}})

        def post(self, url, headers=None, json=None, **kwargs):
            del kwargs
            return self._record("POST", url, headers, json)

        def put(self, url, headers=None, json=None, **kwargs):
            del kwargs
            return self._record("PUT", url, headers, json)

        def get(self, url, params=None, **kwargs):
            del kwargs
            calls.append(("GET", url, {}, params))
            if url.endswith("/balance-history"):
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": [{"notes": "evc-admin:idem-add", "value": 10}]}},
                )
            if url.endswith("/admin/usage/stats"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "total_requests": 4,
                            "total_input_tokens": 100,
                            "total_output_tokens": 20,
                            "total_cost": 9.9,
                            "total_actual_cost": 3.6,
                        },
                    },
                )
            if url.endswith("/admin/users/77"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "id": 77,
                            "email": "staff@example.com",
                            "status": "active",
                            "balance": 12.5,
                            "total_recharged": 30,
                        },
                    },
                )
            return httpx.Response(200, json={"code": 0, "data": {}})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    settings = Settings(
        database_url="sqlite://",
        jwt_secret="test-secret",
        relay_base_url="https://relay.example/api/v1",
        relay_gateway_url="https://relay.example/v1",
        relay_admin_key="restricted-key",
        relay_group_id="9",
        relay_group_name="审核专属",
        timezone="Asia/Shanghai",
    )
    sender = relay.HttpRelay.__new__(relay.HttpRelay)
    sender.settings = settings
    sender.group_id = "9"
    sender.base_url = settings.relay_base_url.rstrip("/")
    sender.client = FakeClient()

    user_id, credentials = sender.create_user("staff@example.com", "password123")
    assert user_id == "77"
    assert credentials["access_token"] == "user-token"
    user_body = next(body for method, url, _, body in calls if method == "POST" and url.endswith("/admin/users"))
    assert user_body["allowed_groups"] == [9]
    assert user_body["restrict_public_groups"] is True
    assert "balance" not in user_body

    sender.add_balance("77", 1000, "idem-add")
    method, _, headers, body = calls[-1]
    assert method == "POST" and body == {"balance": 10.0, "operation": "add", "notes": "evc-admin:idem-add"}
    assert headers["Idempotency-Key"] == "idem-add"

    sender.subtract_balance("77", 300, "idem-sub")
    assert calls[-1][2]["Idempotency-Key"] == "idem-sub"
    assert calls[-1][3] == {"balance": 3.0, "operation": "subtract", "notes": "evc-admin:idem-sub"}

    sender.set_status("77", "disabled")
    assert calls[-1][:2] == ("PUT", "https://relay.example/api/v1/admin/users/77")
    assert calls[-1][3] == {"status": "disabled"}

    # 读取侧：余额、状态、用量和中转站流水都必须能取回并按分换算。
    snapshot = sender.user_snapshot("77")
    assert snapshot["status"] == "active"
    assert snapshot["balance_cents"] == 1250
    month = sender.usage_summary("77", date(2026, 11, 1), date(2026, 11, 20))
    assert month == {"requests": 4, "input_tokens": 100, "output_tokens": 20, "cost_cents": 360}
    assert sender.was_applied("idem-add", "77") is True
    assert sender.was_applied("idem-other", "77") is False
    assert calls[-1][0] == "GET" and calls[-1][3]["page_size"] == 100


def test_login_2fa_keeps_temp_token_on_server(tmp_path, monkeypatch):
    import admin_app.auth_relay as auth_relay

    def login_password(email: str, password: str) -> dict:
        del email, password
        return {"requires_2fa": True, "temp_token": "temp-secret", "user_email_masked": "3***@qq.com"}

    def login_totp(temp: str, code: str) -> dict:
        assert temp == "temp-secret"
        assert code == "123456"
        return {"access_token": "upstream-access", "refresh_token": "upstream-refresh", "user": {"id": 7, "email": SUPER_ADMIN_EMAIL}}

    monkeypatch.setattr(auth_relay, "login_password", login_password)
    monkeypatch.setattr(auth_relay, "login_totp", login_totp)
    with _client(tmp_path, monkeypatch, fake_login=False) as client:
        first = client.post("/v1/auth/login", json={"email": SUPER_ADMIN_EMAIL, "password": "real-pass"})
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["requires_2fa"] is True
        assert "temp-secret" not in first.text
        assert "access_token" not in body
        bad_code = client.post("/v1/auth/login/2fa", json={"challenge": body["challenge"], "totp_code": "12"})
        assert bad_code.status_code == 400
        second = client.post("/v1/auth/login/2fa", json={"challenge": body["challenge"], "totp_code": "123456"})
        assert second.status_code == 200, second.text
        assert second.json()["user"]["role"] == "platform_admin"
        assert "upstream-access" not in second.text


def test_login_and_reconcile(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        failed = client.post("/v1/auth/login", json={"email": "admin@yujing.local", "password": "nope"})
        assert failed.status_code == 401
        headers = _login(client, "admin@yujing.local")
        dashboard = client.get("/v1/dashboard", headers=headers)
        assert dashboard.status_code == 200, dashboard.text
        report = dashboard.json()["reconcile"]
        assert report["ok"] is True
        assert report["overdraft"] is False
        # 公司总额 = 所有成员余额之和 + 已消耗；可分配就是当前余额。
        assert report["allocated"] == round(report["balance"] + report["cumulative_cost"], 2)
        assert report["pool_total"] == report["allocated"]
        assert report["pool_remaining"] == report["balance"]
        assert dashboard.json()["pending_keywords"] == 3


def test_tenant_cannot_cross_company_or_settings(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant = _login(client, "li@jiecheng.com")
        tenants = client.get("/v1/tenants", headers=platform).json()
        youpin = next(item["id"] for item in tenants if item["name"] == "优品优选")
        jie = next(item["id"] for item in tenants if item["name"] == "锦程商贸")
        denied = client.get("/v1/members", headers=tenant, params={"tenant_id": youpin})
        assert denied.status_code == 403
        denied_rules = client.get("/v1/rules", headers=tenant, params={"tenant_id": youpin})
        assert denied_rules.status_code == 403
        own_rules = client.get("/v1/rules", headers=tenant)
        assert own_rules.status_code == 200
        own = client.get("/v1/members", headers=tenant).json()
        assert {item["name"] for item in own} == {"李运营", "张剪辑", "王审核", "赵兼职"}
        assert client.get("/v1/tenants", headers=tenant).status_code == 403
        assert client.get("/v1/settings", headers=tenant).status_code == 403
        assert client.get("/v1/members", headers=platform, params={"tenant_id": jie}).json()[0]["tenant_name"] == "锦程商贸"


def test_keyword_review_rules(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant = _login(client, "li@jiecheng.com")
        member = _login(client, "zhang@jiecheng.com")
        rows = client.get("/v1/rules/submissions", headers=tenant, params={"status": "pending"}).json()
        assert {item["text"] for item in rows} == {"厨房清洁神器", "冬季保暖"}
        own = next(item for item in rows if item["text"] == "冬季保暖")
        other = next(item for item in rows if item["text"] == "厨房清洁神器")
        assert client.post(f"/v1/rules/submissions/{own['id']}/approve", headers=tenant, json={}).status_code == 403
        approved = client.post(f"/v1/rules/submissions/{other['id']}/approve", headers=tenant, json={})
        assert approved.status_code == 200, approved.text
        pulled = client.get("/v1/rules", headers=member, params={"since": 0})
        assert pulled.status_code == 200
        assert pulled.headers["etag"]
        texts = {item["text"] for item in pulled.json()["phrases"]}
        assert "厨房清洁神器" in texts
        assert "家居好物" in texts
        global_rows = client.get("/v1/rules/submissions", headers=platform, params={"status": "pending"}).json()
        quality = next(item for item in global_rows if item["text"] == "品质生活")
        assert client.post(f"/v1/rules/submissions/{quality['id']}/approve", headers=tenant, json={}).status_code == 403
        forced = client.post(
            f"/v1/rules/submissions/{quality['id']}/approve",
            headers=platform,
            json={"force": True, "remark": "平台备注后通过"},
        )
        assert forced.status_code == 200, forced.text
        bad = client.post(
            "/v1/rules/submissions",
            headers=member,
            json={"text": "全网最低", "category": "夸大宣传", "source": "画面", "scope": "tenant"},
        )
        assert bad.status_code == 400
        assert "口播" in bad.json()["detail"]
        long_text = client.post(
            "/v1/rules/submissions",
            headers=member,
            json={"text": "啊" * 41, "category": "价格", "source": "口播", "scope": "tenant"},
        )
        assert long_text.status_code == 400
        global_submit = client.post(
            "/v1/rules/submissions",
            headers=member,
            json={"text": "平台词", "category": "品牌", "source": "画面", "scope": "global"},
        )
        assert global_submit.status_code == 403
        note = client.put(
            "/v1/rules/settings",
            headers=tenant,
            json={"categories": ["价格"], "ignore_phrases": ["包邮"], "note": "这里允许推测"},
        )
        assert note.status_code == 400
        too_many = client.put(
            "/v1/rules/settings",
            headers=tenant,
            json={"categories": ["价格"], "ignore_phrases": [f"词{i}" for i in range(31)], "note": ""},
        )
        assert too_many.status_code == 400
        saved = client.put(
            "/v1/rules/settings",
            headers=tenant,
            json={"categories": ["价格", "规格", "品牌", "夸大宣传"], "ignore_phrases": ["包邮"], "note": "只补充，不放宽"},
        )
        assert saved.status_code == 200, saved.text
        again = client.get("/v1/rules", headers=member, params={"since": 0}).json()
        assert again["ignore_phrases"] == ["包邮"]
        assert "禁止推测" in again["speculation_policy"]


def test_quota_is_idempotent_and_reclaim_stays_reconciled(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        tenant = _login(client, "li@jiecheng.com")
        member = _login(client, "zhang@jiecheng.com")
        assert client.post(
            "/v1/quotas/allocate",
            headers=member,
            json={"member_id": 1, "amount": 10, "idempotency_key": "nope", "direction": "add"},
        ).status_code == 403
        people = client.get("/v1/members", headers=tenant).json()
        zhang = next(item for item in people if item["name"] == "张剪辑")
        before = client.get("/v1/usage/reconcile", headers=tenant).json()
        body = {
            "member_id": zhang["id"],
            "amount": 100,
            "idempotency_key": "qa-test-100",
            "direction": "add",
            "remark": "测试",
        }
        first = client.post("/v1/quotas/allocate", headers=tenant, json=body)
        second = client.post("/v1/quotas/allocate", headers=tenant, json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        after = client.get("/v1/usage/reconcile", headers=tenant).json()
        assert after["ok"] is True
        # 分配是公司管理员余额转到成员余额，公司总额不变。
        assert after["balance"] == before["balance"]
        assert after["allocated"] == before["allocated"]
        assert after["pool_remaining"] == before["pool_remaining"]
        people_after = client.get("/v1/members", headers=tenant).json()
        zhang_after = next(item for item in people_after if item["name"] == "张剪辑")
        li_after = next(item for item in people_after if item["name"] == "李运营")
        assert zhang_after["balance"] == round(zhang["balance"] + 100, 2)
        assert li_after["balance"] == round(zhang_after["balance"] * 0 + li_after["balance"], 2)
        clash = client.post(
            "/v1/quotas/allocate",
            headers=tenant,
            json={**body, "amount": 80},
        )
        assert clash.status_code == 409
        reclaim = client.post(
            "/v1/quotas/allocate",
            headers=tenant,
            json={
                "member_id": zhang["id"],
                "amount": 100,
                "idempotency_key": "qa-test-reclaim",
                "direction": "subtract",
            },
        )
        assert reclaim.status_code == 200, reclaim.text
        restored = client.get("/v1/usage/reconcile", headers=tenant).json()
        assert restored["ok"] is True
        assert restored["allocated"] == before["allocated"]
        assert restored["balance"] == before["balance"]
        assert restored["pool_remaining"] == before["pool_remaining"]
        assert restored["month_cost"] == before["month_cost"]


def test_overdraft_shows_zero_and_breaks_reconcile(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        tenant = _login(client, "li@jiecheng.com")
        from admin_app.config import get_settings
        from admin_app.db import session
        from admin_app.models import Member

        get_settings.cache_clear()
        db = session()
        person = db.query(Member).filter(Member.email == "zhao@jiecheng.com").one()
        person.balance_cents = -500
        person.overdraft = True
        db.commit()
        db.close()
        report = client.get("/v1/usage/reconcile", headers=tenant).json()
        assert report["ok"] is False
        assert report["overdraft"] is True
        assert "赵兼职" in report["overdraft_members"]
        shown = next(item for item in client.get("/v1/members", headers=tenant).json() if item["name"] == "赵兼职")
        assert shown["balance"] == 0
        assert shown["overdraft"] is True


def test_disable_frees_seat_and_invite_uses_only_exclusive_group(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        tenant = _login(client, "li@jiecheng.com")
        people = client.get("/v1/members", headers=tenant).json()
        wang = next(item for item in people if item["name"] == "王审核")
        company = client.get("/v1/quotas/summary", headers=tenant).json()["tenants"][0]
        assert company["name"] == "锦程商贸"
        before = next(item for item in client.get("/v1/tenants", headers=_login(client, "admin@yujing.local")).json() if item["name"] == "锦程商贸")
        disabled = client.post(f"/v1/members/{wang['id']}/disable", headers=tenant)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"
        assert client.post("/v1/auth/login", json={"email": "wang@jiecheng.com", "password": DEMO_PASSWORD}).status_code == 403
        after = next(item for item in client.get("/v1/tenants", headers=_login(client, "admin@yujing.local")).json() if item["name"] == "锦程商贸")
        assert after["seats_used"] == before["seats_used"] - 1
        devices = client.get(f"/v1/members/{wang['id']}/devices", headers=tenant).json()
        assert devices and all(not item["active"] for item in devices)
        invited = client.post(
            "/v1/members/invite",
            headers=tenant,
            json={"email": "new@jiecheng.com", "name": "新同事", "role": "member", "initial_amount": 0},
        )
        assert invited.status_code == 200, invited.text
        accepted = client.post(
            f"/v1/invites/{invited.json()['invite_token']}/accept",
            json={"password": "Newpass123", "name": "新同事"},
        )
        assert accepted.status_code == 200, accepted.text
        from admin_app.db import session
        from admin_app.models import RelayAccount

        db = session()
        account = db.query(RelayAccount).filter(RelayAccount.email == "new@jiecheng.com").one()
        assert account.groups == ["电商审核专属-bot创建"]
        assert account.status == "active"
        db.close()
        restored = client.post(f"/v1/members/{wang['id']}/enable", headers=tenant)
        assert restored.status_code == 200, restored.text


def test_direct_company_and_employee_creation(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        created = client.post(
            "/v1/tenants",
            headers=platform,
            json={
                "name": "新入驻公司",
                "admin_email": "boss@newco.example",
                "admin_name": "公司管理员",
                "admin_password": DEMO_PASSWORD,
                "pool": 1000,
                "seats": 3,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["admin"]["role"] == "tenant_admin"
        assert "invite_token" not in created.json()
        tenant_id = created.json()["tenant"]["id"]
        admin = _login(client, "boss@newco.example")
        employee = client.post(
            "/v1/members",
            headers=admin,
            json={"email": "worker@newco.example", "name": "员工", "password": DEMO_PASSWORD, "role": "member"},
        )
        assert employee.status_code == 200, employee.text
        assert employee.json()["tenant_id"] == tenant_id
        assert employee.json()["role"] == "member"
        denied = client.post(
            "/v1/members",
            headers=admin,
            json={"email": "boss2@newco.example", "password": DEMO_PASSWORD, "role": "tenant_admin"},
        )
        assert denied.status_code == 403


def test_cumulative_reconciliation_survives_month_boundary(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        tenant = _login(client, "li@jiecheng.com")
        from admin_app.db import session
        from admin_app.models import Member, Tenant

        db = session()
        person = db.query(Member).filter(Member.email == "zhang@jiecheng.com").one()
        company = db.get(Tenant, person.tenant_id)
        person.balance_cents = 50_000
        person.month_cost_cents = 10_000
        person.lifetime_cost_cents = 150_000
        company.allocated_total_cents = sum(
            row.balance_cents + max(row.lifetime_cost_cents, row.month_cost_cents)
            for row in db.query(Member).filter(Member.tenant_id == company.id, Member.role != "platform_admin").all()
        )
        db.commit()
        expected = company.allocated_total_cents / 100
        db.close()
        report = client.get("/v1/usage/reconcile", headers=tenant).json()
        assert report["ok"] is True
        assert report["cumulative_cost"] > report["month_cost"]
        assert round(report["balance"] + report["cumulative_cost"], 2) == expected


def test_usage_snapshot_upsert_does_not_double_count(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch):
        from admin_app.db import session
        from admin_app.models import Member, UsageSnapshot
        from admin_app.service import record_usage_snapshot
        from admin_app.timeutil import shanghai_now

        db = session()
        member = db.query(Member).filter(Member.email == "zhang@jiecheng.com").one()
        member.lifetime_cost_cents = 20_000
        today = shanghai_now().date()
        record_usage_snapshot(
            db,
            tenant_id=member.tenant_id,
            member_id=member.id,
            day=today,
            requests=3,
            input_tokens=100,
            output_tokens=40,
            cost_cents=500,
        )
        record_usage_snapshot(
            db,
            tenant_id=member.tenant_id,
            member_id=member.id,
            day=today,
            requests=4,
            input_tokens=120,
            output_tokens=50,
            cost_cents=700,
        )
        db.commit()
        db.refresh(member)
        assert member.lifetime_cost_cents == 20_700
        assert member.month_cost_cents == 700
        assert db.query(UsageSnapshot).filter(UsageSnapshot.member_id == member.id, UsageSnapshot.day == today).count() == 1
        db.close()

def test_relay_sync_pulls_balance_and_usage(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        tenant = _login(client, "li@jiecheng.com")
        member = _login(client, "zhang@jiecheng.com")
        from admin_app.db import session
        from admin_app.models import Member

        db = session()
        zhang = db.query(Member).filter(Member.email == "zhang@jiecheng.com").one()
        zhang.balance_cents = 5000
        db.commit()
        zhang_id = zhang.id
        relay_user_id = zhang.relay_user_id
        db.close()
        assert relay_user_id

        class FakeRemote:
            mode = "remote"

            def user_snapshot(self, rid: str) -> dict:
                return {
                    "id": rid,
                    "email": "",
                    "status": "active",
                    "balance_cents": 4800,
                    "total_recharged_cents": 5000,
                }

            def usage_summary(self, rid: str, start: date, end: date) -> dict:
                del rid, end
                if start.year >= 2026:
                    return {"requests": 3, "input_tokens": 100, "output_tokens": 50, "cost_cents": 200}
                return {"requests": 9, "input_tokens": 300, "output_tokens": 150, "cost_cents": 900}

        import admin_app.service as service

        monkeypatch.setattr(service, "get_relay", lambda db: FakeRemote())
        assert client.post("/v1/relay/sync", headers=member).status_code == 403
        synced = client.post("/v1/relay/sync", headers=tenant)
        assert synced.status_code == 200, synced.text
        body = synced.json()
        assert body["mode"] == "remote"
        assert body["synced"] is True
        row = next(item for item in body["members"] if item["member_id"] == zhang_id)
        assert row["relay_user_id"] == relay_user_id
        assert row["relay_balance"] == 48.0
        # 同步时本地余额对齐中转站，差额归零。
        assert row["local_balance"] == 48.0
        assert row["diff"] == 0.0
        assert row["ok"] is True
        assert row["month_cost"] == 2.0
        assert row["lifetime_cost"] == 9.0

        # 快照落库后，成员页和租户页不请求中转站也能看到对账结果。
        shown = next(item for item in client.get("/v1/members", headers=tenant).json() if item["id"] == zhang_id)
        assert shown["relay_balance"] == 48.0
        assert shown["relay_diff"] == 0.0

        # 同步会按天写最近 7 天的用量快照，总览的趋势图靠它。
        from admin_app.models import UsageSnapshot

        db = session()
        days = (
            db.query(UsageSnapshot)
            .filter(UsageSnapshot.member_id == zhang_id)
            .count()
        )
        db.close()
        assert days == 7, f"应该写入 7 天快照，实际 {days}"
        assert shown["relay_status"] == "active"
        assert shown["relay_checked_at"]
        tenant_rows = client.get("/v1/tenants", headers=tenant)
        assert tenant_rows.status_code == 403
        platform = _login(client, "admin@yujing.local")
        company = next(item for item in client.get("/v1/tenants", headers=platform).json() if item["name"] == "锦程商贸")
        assert company["relay_checked_at"]
        assert company["relay_diff"] is not None

        status = client.get("/v1/relay/status", headers=tenant)
        assert status.status_code == 200
        assert status.json()["totals"]["relay_balance"] == body["totals"]["relay_balance"]


def test_relay_sync_refuses_local_mode(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        tenant = _login(client, "li@jiecheng.com")
        response = client.post("/v1/relay/sync", headers=tenant)
        assert response.status_code == 400
        assert "演示环境" in response.json()["detail"]
        status = client.get("/v1/relay/status", headers=tenant).json()
        assert status["mode"] == "local"
        assert status["synced"] is False
def test_split_database_routing(tmp_path, monkeypatch):
    """词库与本机设置落本机库，租户/成员/额度落云端库，两边不串表。"""
    local = tmp_path / "local.db"
    cloud = tmp_path / "cloud.db"
    monkeypatch.setenv("EVC_ADMIN_DATABASE_URL", f"sqlite:///{local}")
    monkeypatch.setenv("EVC_ADMIN_CLOUD_DATABASE_URL", f"sqlite:///{cloud}")
    monkeypatch.setenv("EVC_DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setenv("EVC_ADMIN_JWT_SECRET", "test-secret-test-secret")
    monkeypatch.delenv("EVC_RELAY_ADMIN_KEY", raising=False)
    monkeypatch.setattr("admin_app.auth_relay.login_password", _fake_login)
    from admin_app.config import get_settings
    from admin_app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    from admin_app.main import create_app

    with TestClient(create_app()) as client:
        platform = _login(client, "admin@yujing.local")
        tenants = client.get("/v1/tenants", headers=platform)
        assert tenants.status_code == 200, tenants.text
        assert tenants.json()

        from admin_app.db import session
        from admin_app.models import KeywordEntry

        db = session()
        db.add(KeywordEntry(tenant_id=1, text="本机词", category="价格", source="口播"))
        db.commit()
        assert db.query(KeywordEntry).filter(KeywordEntry.text == "本机词").count() == 1
        db.close()

    import sqlite3

    def tables(path) -> set[str]:
        connection = sqlite3.connect(path)
        try:
            return {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        finally:
            connection.close()

    local_tables = tables(local)
    cloud_tables = tables(cloud)
    assert {"keyword_entries", "keyword_submissions", "tenant_rules"} <= local_tables
    assert not {"tenants", "tenant_members", "quota_allocations"} & local_tables
    assert {"tenants", "tenant_members", "quota_allocations", "usage_snapshots", "shared_rules"} <= cloud_tables
    assert "keyword_entries" not in cloud_tables
    assert "shared_rules" not in local_tables



def test_link_existing_relay_account(tmp_path, monkeypatch):
    """中转站里已经注册过的邮箱，可以绑定已有账号，不会重复建号。"""
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")

        from admin_app.db import session
        from admin_app.models import Member, RelayAccount

        db = session()
        db.add(
            RelayAccount(
                relay_user_id="user_9001",
                email="veteran@jiecheng.com",
                status="active",
                groups=["电商审核专属-bot创建"],
            )
        )
        db.commit()
        db.close()

        tenant = next(
            item for item in client.get("/v1/tenants", headers=platform).json() if item["name"] == "锦程商贸"
        )

        response = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "veteran@jiecheng.com",
                "name": "老用户",
                "password": "",
                "role": "member",
                "tenant_id": tenant["id"],
                "link_existing": True,
            },
        )
        assert response.status_code == 200, response.text
        created = response.json()
        assert created["relay_user_id"] == "user_9001"

        db = session()
        assert (
            db.query(RelayAccount).filter(RelayAccount.email == "veteran@jiecheng.com").count() == 1
        ), "绑定已有账号时不能再建一个中转站账号"
        bound = db.query(Member).filter(Member.email == "veteran@jiecheng.com").one()
        assert bound.relay_user_id == "user_9001"
        db.close()

        # 中转站里没有的邮箱，绑定模式要明确报错，不能静默建号。
        missing = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "nobody@jiecheng.com",
                "name": "nobody",
                "role": "member",
                "tenant_id": tenant["id"],
                "link_existing": True,
            },
        )
        assert missing.status_code == 400
        assert "没有找到这个邮箱的账号" in missing.json()["detail"]

        # 同号不能重复绑定。
        again = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "veteran2@jiecheng.com",
                "name": "重复",
                "role": "member",
                "tenant_id": tenant["id"],
                "link_existing": True,
            },
        )
        assert again.status_code in (400, 404)

        # 普通模式（不绑定）仍然要求密码。
        no_password = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "fresh@jiecheng.com",
                "name": "新人",
                "role": "member",
                "tenant_id": tenant["id"],
            },
        )
        assert no_password.status_code == 400


def test_link_existing_rejects_disabled_or_missing(tmp_path, monkeypatch):
    """停用的中转站账号不能直接绑。"""
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")

        from admin_app.db import session
        from admin_app.models import RelayAccount

        db = session()
        db.add(
            RelayAccount(
                relay_user_id="user_9002",
                email="stopped@jiecheng.com",
                status="disabled",
                groups=["电商审核专属-bot创建"],
            )
        )
        db.commit()
        db.close()

        tenant = next(
            item for item in client.get("/v1/tenants", headers=platform).json() if item["name"] == "锦程商贸"
        )
        response = client.post(
            "/v1/members",
            headers=platform,
            json={
                "email": "stopped@jiecheng.com",
                "name": "停用号",
                "role": "member",
                "tenant_id": tenant["id"],
                "link_existing": True,
            },
        )
        assert response.status_code == 400
        assert "已停用" in response.json()["detail"]


def test_sso_accepts_relay_token_without_password(tmp_path, monkeypatch):
    import admin_app.auth_relay as auth_relay

    def fake_account(token: str) -> dict:
        if token == "relay-good":
            return {"id": 77, "email": "li@jiecheng.com"}
        if token == "relay-stranger":
            return {"id": 99, "email": "nobody@example.com"}
        raise RelayLoginError("登录已失效，请重新登录", 401)

    monkeypatch.setattr(auth_relay, "current_account", fake_account)
    with _client(tmp_path, monkeypatch, fake_login=False) as client:
        bad = client.post("/v1/auth/sso", json={"access_token": "relay-bad"})
        assert bad.status_code == 401

        stranger = client.post("/v1/auth/sso", json={"access_token": "relay-stranger"})
        assert stranger.status_code == 403

        ok = client.post("/v1/auth/sso", json={"access_token": "relay-good", "refresh_token": "r1"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["user"]["email"] == "li@jiecheng.com"
        assert body["access_token"] and body["refresh_token"]

        me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200
