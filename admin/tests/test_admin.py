import io
import json

import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.auth_relay import SUPER_ADMIN_EMAIL, RelayLoginError
from app.seed import DEMO_PASSWORD


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
    monkeypatch.setenv("EVC_ADMIN_JWT_SECRET", "test-secret-test-secret")
    monkeypatch.delenv("EVC_RELAY_ADMIN_KEY", raising=False)
    from app.config import get_settings
    from app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    if fake_login:
        import app.auth_relay as auth_relay

        monkeypatch.setattr(auth_relay, "login_password", _fake_login)
    from app.main import create_app

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

    import app.auth_relay as auth_relay

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
    import app.relay as relay
    from app.config import Settings

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
    sender.client = FakeClient()

    assert sender.create_user("staff@example.com", "password123") == "77"
    user_body = next(body for method, url, _, body in calls if method == "POST" and url.endswith("/admin/users"))
    assert user_body["allowed_groups"] == [9]
    assert user_body["restrict_public_groups"] is True
    assert "balance" not in user_body

    sender.add_balance("77", 1000, "idem-add")
    method, _, headers, body = calls[-1]
    assert method == "POST" and body == {"balance": 10.0, "operation": "add"}
    assert headers["Idempotency-Key"] == "idem-add"

    sender.subtract_balance("77", 300, "idem-sub")
    assert calls[-1][2]["Idempotency-Key"] == "idem-sub"
    assert calls[-1][3] == {"balance": 3.0, "operation": "subtract"}

    sender.set_status("77", "disabled")
    assert calls[-1][:2] == ("PUT", "https://relay.example/api/v1/admin/users/77")
    assert calls[-1][3] == {"status": "disabled"}


def test_login_2fa_keeps_temp_token_on_server(tmp_path, monkeypatch):
    import app.auth_relay as auth_relay

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
        assert report["balance"] + report["month_cost"] == report["allocated"]
        assert report["balance"] == 23573.5
        assert report["month_cost"] == 8426.5
        assert report["allocated"] == 32000
        assert report["pool_remaining"] == 18000
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
        assert after["balance"] == round(before["balance"] + 100, 2)
        assert after["allocated"] == round(before["allocated"] + 100, 2)
        assert after["pool_remaining"] == round(before["pool_remaining"] - 100, 2)
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
        from app.config import get_settings
        from app.db import session
        from app.models import Member

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


def test_jobs_follow_role_and_export_columns(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        tenant = _login(client, "li@jiecheng.com")
        member = _login(client, "zhang@jiecheng.com")
        other = _login(client, "chen@youpin.com")
        assert {item["filename"] for item in client.get("/v1/jobs", headers=platform).json()} == {
            "直播间-1117.mp4",
            "主图视频.mp4",
            "详情页口播.mp4",
        }
        assert {item["filename"] for item in client.get("/v1/jobs", headers=tenant).json()} == {
            "直播间-1117.mp4",
            "主图视频.mp4",
        }
        assert {item["filename"] for item in client.get("/v1/jobs", headers=member).json()} == {
            "直播间-1117.mp4",
            "主图视频.mp4",
        }
        assert [item["filename"] for item in client.get("/v1/jobs", headers=other).json()] == ["详情页口播.mp4"]
        done = next(item for item in client.get("/v1/jobs", headers=member).json() if item["filename"] == "主图视频.mp4")
        detail = client.get(f"/v1/jobs/{done['id']}", headers=member).json()
        assert [row["类别"] for row in detail["findings"]] == ["价格", "品牌", "夸大宣传", "解析失败"]
        hidden = client.get(f"/v1/jobs/{done['id']}", headers=other)
        assert hidden.status_code == 404
        exported = client.get(f"/v1/jobs/{done['id']}/export", headers=member)
        assert exported.status_code == 200
        book = load_workbook(io.BytesIO(exported.content))
        sheet = book.active
        assert [cell.value for cell in sheet[1]] == ["时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注"]
        assert sheet["D2"].value == "只要九十九元"


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
        from app.db import session
        from app.models import RelayAccount

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
        from app.db import session
        from app.models import Member, Tenant

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
        from app.db import session
        from app.models import Member, UsageSnapshot
        from app.service import record_usage_snapshot
        from app.timeutil import shanghai_now

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
