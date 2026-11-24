"""审计日志按页返回，每页只允许 20、50、100，公司范围仍按角色切开。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admin import _client, _login  # noqa: E402


def _fill(db, tenant_id: int, actor_id: int, prefix: str, count: int) -> None:
    from admin_app.models import AuditLog

    for index in range(count):
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                actor_id=actor_id,
                role="member",
                action="page.probe",
                object=f"{prefix}-{index}",
                summary=f"{prefix}-{index}",
                ip="127.0.0.1",
            )
        )


def test_audit_page_size_matches_total_and_company_scope(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        from admin_app.db import session
        from admin_app.models import AuditLog, Member, Tenant

        db = session()
        jie = db.query(Tenant).filter(Tenant.name == "锦程商贸").one()
        you = db.query(Tenant).filter(Tenant.name == "优品优选").one()
        zhang = db.query(Member).filter(Member.email == "zhang@jiecheng.com").one()
        chen = db.query(Member).filter(Member.email == "chen@youpin.com").one()
        _fill(db, jie.id, zhang.id, "锦程分页", 25)
        _fill(db, you.id, chen.id, "优品分页", 25)
        db.commit()
        all_total = db.query(AuditLog).count()
        jie_total = db.query(AuditLog).filter(AuditLog.tenant_id == jie.id).count()
        zhang_total = db.query(AuditLog).filter(AuditLog.actor_id == zhang.id).count()
        you_id = you.id
        db.close()

        platform = _login(client, "admin@yujing.local")
        default = client.get("/v1/audit-logs", headers=platform)
        assert default.status_code == 200, default.text
        body = default.json()
        assert body["page"] == 1
        assert body["page_size"] == 20
        assert body["total"] == all_total
        assert len(body["items"]) == 20
        first_ids = [item["id"] for item in body["items"]]
        assert first_ids == sorted(first_ids, reverse=True)

        second = client.get("/v1/audit-logs?page=2&page_size=20", headers=platform).json()
        assert second["total"] == all_total
        assert len(second["items"]) == 20
        assert set(first_ids).isdisjoint(item["id"] for item in second["items"])

        last_page = (all_total + 19) // 20
        last = client.get(f"/v1/audit-logs?page={last_page}&page_size=20", headers=platform).json()
        assert len(last["items"]) == all_total - (last_page - 1) * 20
        assert last["items"]

        empty = client.get("/v1/audit-logs?page=99&page_size=20", headers=platform).json()
        assert empty["items"] == []
        assert empty["total"] == all_total

        for size in (50, 100):
            page = client.get(f"/v1/audit-logs?page=1&page_size={size}", headers=platform).json()
            assert page["page_size"] == size
            assert page["total"] == all_total
            assert len(page["items"]) == min(size, all_total)

        rejected = client.get("/v1/audit-logs?page_size=10", headers=platform)
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "每页条数只能是 20、50 或 100"
        assert client.get("/v1/audit-logs?page_size=200", headers=platform).status_code == 400

        company = _login(client, "li@jiecheng.com")
        own_company = client.get("/v1/audit-logs?page_size=100", headers=company).json()
        assert own_company["total"] == jie_total
        assert len(own_company["items"]) == jie_total
        assert any(item["summary"].startswith("锦程分页") for item in own_company["items"])
        assert all(not item["summary"].startswith("优品分页") for item in own_company["items"])
        denied = client.get(f"/v1/audit-logs?tenant_id={you_id}&page_size=20", headers=company)
        assert denied.status_code == 403

        member = _login(client, "zhang@jiecheng.com")
        own_rows = client.get("/v1/audit-logs?page_size=100", headers=member).json()
        assert own_rows["total"] == zhang_total
        assert len(own_rows["items"]) == zhang_total
        assert own_rows["total"] < jie_total
        assert all(item["summary"].startswith("锦程分页") for item in own_rows["items"])
