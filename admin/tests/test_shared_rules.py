"""共享违规词：提交、自己的状态、审批、可见范围和权限。不访问中转站。"""

import sqlite3
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from admin_app.db import ensure_shared_rules

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admin import _client, _login  # noqa: E402


def test_submit_review_visibility_and_permissions(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        member = _login(client, "zhang@jiecheng.com")
        other = _login(client, "wang@jiecheng.com")
        company_admin = _login(client, "li@jiecheng.com")
        other_company = _login(client, "zhou@youpin.com")
        platform = _login(client, "admin@yujing.local")

        created = client.post(
            "/v1/shared-rules",
            headers=member,
            json={"word": "全 网最低", "category": "价格", "description": "口播里的绝对低价"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["word"] == "全网最低"
        assert body["status"] == "pending"
        rule_id = body["id"]

        approved = client.get("/v1/shared-rules/approved", headers=other)
        assert approved.status_code == 200
        assert approved.json() == []
        assert client.get("/v1/shared-rules/approved", headers=other_company).json() == []

        own = client.get("/v1/shared-rules/mine", headers=member).json()
        assert [item["id"] for item in own] == [rule_id]
        assert client.get("/v1/shared-rules/mine", headers=other).json() == []

        assert client.get("/v1/shared-rules/pending", headers=member).status_code == 403
        assert client.get("/v1/shared-rules/pending", headers=platform).status_code == 403
        assert client.post(f"/v1/shared-rules/{rule_id}/approve", headers=member, json={}).status_code == 403
        assert client.post(f"/v1/shared-rules/{rule_id}/approve", headers=platform, json={}).status_code == 403
        assert client.post(f"/v1/shared-rules/{rule_id}/reject", headers=platform, json={"remark": "不行"}).status_code == 403
        cross = client.post(f"/v1/shared-rules/{rule_id}/approve", headers=other_company, json={})
        assert cross.status_code == 403
        assert "本公司" in cross.json()["detail"]

        pending = client.get("/v1/shared-rules/pending", headers=company_admin)
        assert pending.status_code == 200, pending.text
        assert [item["id"] for item in pending.json()] == [rule_id]
        assert client.get("/v1/shared-rules/pending", headers=other_company).json() == []

        passed = client.post(f"/v1/shared-rules/{rule_id}/approve", headers=company_admin, json={"remark": ""})
        assert passed.status_code == 200, passed.text
        assert passed.json()["status"] == "approved"

        for headers in (member, other, other_company, platform):
            visible = client.get("/v1/shared-rules/approved", headers=headers).json()
            assert [item["word"] for item in visible] == ["全网最低"]

        duplicate = client.post(
            "/v1/shared-rules",
            headers=other,
            json={"word": "全网最低", "category": "价格"},
        )
        assert duplicate.status_code == 400
        assert "相同" in duplicate.json()["detail"]

        rejected = client.post(
            "/v1/shared-rules",
            headers=member,
            json={"word": "绝对第一", "category": "夸大宣传"},
        )
        assert rejected.status_code == 200, rejected.text
        rejected_id = rejected.json()["id"]
        assert client.get("/v1/shared-rules/approved", headers=other).json() == [
            item for item in client.get("/v1/shared-rules/approved", headers=other).json() if item["word"] == "全网最低"
        ]
        denied = client.post(
            f"/v1/shared-rules/{rejected_id}/reject",
            headers=company_admin,
            json={"remark": "表述太绝对"},
        )
        assert denied.status_code == 200, denied.text
        assert denied.json()["status"] == "rejected"
        assert denied.json()["review_comment"] == "表述太绝对"
        mine = client.get("/v1/shared-rules/mine", headers=member).json()
        own_rejected = next(item for item in mine if item["id"] == rejected_id)
        assert own_rejected["review_comment"] == "表述太绝对"
        assert all(item["word"] != "绝对第一" for item in client.get("/v1/shared-rules/approved", headers=other).json())
        assert all(item["word"] != "绝对第一" for item in client.get("/v1/shared-rules/mine", headers=other).json())

        logs = client.get("/v1/audit-logs?page_size=100", headers=platform).json()["items"]
        actions = {item["action"] for item in logs}
        assert "shared_rule.submit" in actions
        assert "shared_rule.approve" in actions
        assert "shared_rule.reject" in actions
        text = client.get("/v1/audit-logs?page_size=100", headers=platform).text
        assert "全网最低" in text


def test_relay_token_can_read_approved_without_admin_jwt(tmp_path, monkeypatch):
    def fake_account(token: str) -> dict:
        if token != "relay-token-for-test":
            raise AssertionError("unexpected token")
        return {"id": 5, "email": "zhang@jiecheng.com"}

    monkeypatch.setattr("admin_app.auth_relay.current_account", fake_account)
    with _client(tmp_path, monkeypatch) as client:
        company_admin = _login(client, "li@jiecheng.com")
        member = _login(client, "zhang@jiecheng.com")
        created = client.post(
            "/v1/shared-rules",
            headers=member,
            json={"word": "假一赔十", "category": "夸大宣传"},
        )
        assert created.status_code == 200, created.text
        approved = client.post(
            f"/v1/shared-rules/{created.json()['id']}/approve",
            headers=company_admin,
            json={},
        )
        assert approved.status_code == 200, approved.text
        via_relay = client.get(
            "/v1/shared-rules/approved",
            headers={"Authorization": "Bearer relay-token-for-test"},
        )
        assert via_relay.status_code == 200, via_relay.text
        assert [item["word"] for item in via_relay.json()] == ["假一赔十"]
        assert "relay-token-for-test" not in via_relay.text


def test_company_admin_cannot_review_own_submission(tmp_path, monkeypatch):
    """自己提交的词不自动通过，要等同一家公司的另一位管理员。"""
    with _client(tmp_path, monkeypatch) as client:
        company_admin = _login(client, "li@jiecheng.com")
        created = client.post(
            "/v1/shared-rules",
            headers=company_admin,
            json={"word": "管理员自提", "category": "品牌"},
        )
        assert created.status_code == 200, created.text
        rule_id = created.json()["id"]
        assert created.json()["status"] == "pending"
        own = client.post(f"/v1/shared-rules/{rule_id}/approve", headers=company_admin, json={})
        assert own.status_code == 403
        assert "自己" in own.json()["detail"]

        from admin_app.db import session
        from admin_app.models import Member

        db = session()
        li = db.query(Member).filter(Member.email == "li@jiecheng.com").one()
        db.add(
            Member(
                tenant_id=li.tenant_id,
                email="li2@jiecheng.com",
                name="第二管理员",
                role="tenant_admin",
                password_hash="",
                status="active",
            )
        )
        db.commit()
        db.close()

        second = _login(client, "li2@jiecheng.com")
        queued = client.get("/v1/shared-rules/pending", headers=second).json()
        assert rule_id in [item["id"] for item in queued]
        passed = client.post(f"/v1/shared-rules/{rule_id}/approve", headers=second, json={})
        assert passed.status_code == 200, passed.text
        assert passed.json()["status"] == "approved"


def test_platform_admin_cannot_submit_shared_rule(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        response = client.post(
            "/v1/shared-rules",
            headers=platform,
            json={"word": "平台词", "category": "价格"},
        )
        assert response.status_code == 403


def test_migration_adds_table_without_seed_rows(tmp_path):
    path = tmp_path / "prod.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE keep_me (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO keep_me (name) VALUES ('原有数据')"))
    ensure_shared_rules(engine)
    ensure_shared_rules(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM keep_me")).scalar() == "原有数据"
        assert connection.execute(text("SELECT COUNT(*) FROM shared_rules")).scalar() == 0
    raw = sqlite3.connect(path)
    try:
        names = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        raw.close()
    assert "shared_rules" in names
    assert "uq_shared_rules_active_word" in indexes
