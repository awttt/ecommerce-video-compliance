"""公司审核规则：默认词进表，提交按条 diff，通过一条才改一条。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admin import _client, _login  # noqa: E402


def _doc(client, headers, tenant_id=None):
    path = "/v1/company-rules" if tenant_id is None else f"/v1/company-rules?tenant_id={tenant_id}"
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _phrase(document, text, source="口播", category="价格"):
    return next(
        item
        for item in document["phrases"]
        if item["text"] == text and item["source"] == source and item["category"] == category
    )


def _submit(client, headers, changes):
    response = client.post("/v1/company-rules/submissions", headers=headers, json={"changes": changes})
    return response


def test_defaults_submit_review_and_direct_edit(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        from admin_app.db import session
        from admin_app.models import CompanyRule

        db = session()
        assert db.query(CompanyRule).count() == 0
        db.close()

        member = _login(client, "zhang@jiecheng.com")
        other = _login(client, "wang@jiecheng.com")
        company_admin = _login(client, "li@jiecheng.com")
        other_company = _login(client, "zhou@youpin.com")
        platform = _login(client, "admin@yujing.local")

        current = _doc(client, member)
        texts = {item["text"] for item in current["phrases"]}
        assert "到手" in texts
        assert "logo" in texts
        assert "夸大宣传" in current["categories"]
        daoshou = _phrase(current, "到手")
        logo = _phrase(current, "logo", "画面", "品牌")
        assert daoshou["id"] and logo["id"]

        same = client.put(
            "/v1/company-rules",
            headers=company_admin,
            json={
                "categories": current["categories"],
                "phrases": [
                    {"text": item["text"], "category": item["category"], "source": item["source"]}
                    for item in current["phrases"]
                ],
                "ignore_phrases": [],
                "note": "",
            },
        )
        assert same.status_code == 200, same.text
        kept_doc = same.json()
        assert _phrase(kept_doc, "到手")["id"] == daoshou["id"]
        assert _phrase(kept_doc, "logo", "画面", "品牌")["id"] == logo["id"]

        created = _submit(
            client,
            member,
            [
                {"op": "add", "target": "phrase", "after": {"text": "全 网最低", "category": "价格", "source": "口播"}},
                {"op": "add", "target": "ignore", "after": {"text": "包 邮"}},
                {"op": "update", "target": "note", "before": {"text": ""}, "after": {"text": "只补充"}},
            ],
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["submitted"] == 3
        summaries = [item["summary"] for item in body["items"]]
        assert any("全网最低" in item for item in summaries)
        assert any("包邮" in item for item in summaries)
        assert all(item["status"] == "pending" for item in body["items"])
        phrase_id = next(item["id"] for item in body["items"] if item["target"] == "phrase")
        ignore_id = next(item["id"] for item in body["items"] if item["target"] == "ignore")
        note_id = next(item["id"] for item in body["items"] if item["target"] == "note")

        still = _doc(client, other)
        assert "到手" in {item["text"] for item in still["phrases"]}
        assert "全网最低" not in {item["text"] for item in still["phrases"]}
        assert still["ignore_phrases"] == []
        assert still["note"] == ""

        assert client.get("/v1/company-rules/submissions/pending", headers=member).status_code == 403
        assert client.get("/v1/company-rules/submissions/pending", headers=platform).status_code == 403
        assert client.post(f"/v1/company-rules/submissions/{phrase_id}/approve", headers=member, json={}).status_code == 403
        assert client.post(f"/v1/company-rules/submissions/{phrase_id}/approve", headers=platform, json={}).status_code == 403
        cross = client.post(f"/v1/company-rules/submissions/{phrase_id}/approve", headers=other_company, json={})
        assert cross.status_code == 403
        assert "本公司" in cross.json()["detail"]

        pending = client.get("/v1/company-rules/submissions/pending", headers=company_admin)
        assert pending.status_code == 200, pending.text
        assert [item["id"] for item in pending.json()] == [phrase_id, ignore_id, note_id]
        assert client.get("/v1/company-rules/submissions/pending", headers=other_company).json() == []

        picture = _submit(
            client,
            member,
            [{"op": "add", "target": "phrase", "after": {"text": "绝对第一", "category": "夸大宣传", "source": "画面"}}],
        )
        assert picture.status_code == 400
        assert "口播" in picture.json()["detail"]
        guessed = _submit(
            client,
            member,
            [{"op": "update", "target": "note", "before": {"text": ""}, "after": {"text": "这里允许推测"}}],
        )
        assert guessed.status_code == 400
        assert "推测" in guessed.json()["detail"]
        empty = _submit(client, member, [])
        assert empty.status_code == 400
        assert "没有改动" in empty.json()["detail"]
        again = _submit(
            client,
            member,
            [{"op": "add", "target": "phrase", "after": {"text": "全网最低", "category": "价格", "source": "口播"}}],
        )
        assert again.status_code == 400
        assert "待审" in again.json()["detail"]

        passed = client.post(f"/v1/company-rules/submissions/{phrase_id}/approve", headers=company_admin, json={})
        assert passed.status_code == 200, passed.text
        assert passed.json()["status"] == "approved"
        approved = _doc(client, other)
        assert "全网最低" in {item["text"] for item in approved["phrases"]}
        assert "到手" in {item["text"] for item in approved["phrases"]}
        assert approved["ignore_phrases"] == []
        assert approved["note"] == ""
        assert _phrase(approved, "到手")["id"] == daoshou["id"]
        outsider = _doc(client, other_company)
        assert "全网最低" not in {item["text"] for item in outsider["phrases"]}
        assert "到手" in {item["text"] for item in outsider["phrases"]}

        missing = client.post(f"/v1/company-rules/submissions/{ignore_id}/reject", headers=company_admin, json={})
        assert missing.status_code == 400
        denied = client.post(
            f"/v1/company-rules/submissions/{ignore_id}/reject",
            headers=company_admin,
            json={"remark": "先不收"},
        )
        assert denied.status_code == 200, denied.text
        assert denied.json()["review_comment"] == "先不收"
        kept = _doc(client, member)
        assert kept["ignore_phrases"] == []
        assert "到手" in {item["text"] for item in kept["phrases"]}

        noted = client.post(f"/v1/company-rules/submissions/{note_id}/approve", headers=company_admin, json={})
        assert noted.status_code == 200, noted.text
        assert _doc(client, member)["note"] == "只补充"

        edited = client.put(
            "/v1/company-rules",
            headers=company_admin,
            json={
                "categories": ["价格", "规格"],
                "phrases": [{"text": "活动价", "category": "价格", "source": "口播"}],
                "ignore_phrases": [],
                "note": "",
            },
        )
        assert edited.status_code == 200, edited.text
        assert [item["text"] for item in edited.json()["phrases"]] == ["活动价"]
        assert edited.json()["phrases"][0]["id"]
        assert client.put(
            "/v1/company-rules",
            headers=member,
            json={"categories": ["价格"], "phrases": [{"text": "员工改", "category": "价格", "source": "口播"}]},
        ).status_code == 403
        assert client.put(
            "/v1/company-rules",
            headers=other_company,
            json={
                "tenant_id": current["tenant_id"],
                "categories": ["价格"],
                "phrases": [{"text": "越权", "category": "价格", "source": "口播"}],
            },
        ).status_code == 403

        forced = client.put(
            "/v1/company-rules",
            headers=platform,
            json={
                "tenant_id": current["tenant_id"],
                "categories": ["品牌"],
                "phrases": [{"text": "官方", "category": "品牌", "source": "口播"}],
            },
        )
        assert forced.status_code == 200, forced.text
        assert [item["text"] for item in _doc(client, member)["phrases"]] == ["官方"]
        assert client.put(
            "/v1/company-rules",
            headers=platform,
            json={"categories": ["价格"], "phrases": [{"text": "没选公司", "category": "价格", "source": "口播"}]},
        ).status_code == 400


def test_update_keeps_other_phrases_and_stale_review_waits(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        member = _login(client, "zhang@jiecheng.com")
        company_admin = _login(client, "li@jiecheng.com")
        current = _doc(client, member)
        daoshou = _phrase(current, "到手")
        logo_id = _phrase(current, "logo", "画面", "品牌")["id"]
        created = _submit(
            client,
            member,
            [
                {
                    "op": "update",
                    "target": "phrase",
                    "item_id": daoshou["id"],
                    "before": {"text": "到手", "category": "价格", "source": "口播"},
                    "after": {"text": "到手价", "category": "价格", "source": "口播"},
                }
            ],
        )
        assert created.status_code == 200, created.text
        submission_id = created.json()["items"][0]["id"]
        assert "修改" in created.json()["items"][0]["summary"]

        replaced = [
            {"text": item["text"], "category": item["category"], "source": item["source"]}
            for item in current["phrases"]
        ]
        for item in replaced:
            if item["text"] == "到手" and item["source"] == "口播" and item["category"] == "价格":
                item["text"] = "活动到手"
        edited = client.put(
            "/v1/company-rules",
            headers=company_admin,
            json={
                "categories": current["categories"],
                "phrases": replaced,
                "ignore_phrases": [],
                "note": "",
            },
        )
        assert edited.status_code == 200, edited.text
        assert _phrase(edited.json(), "活动到手")["id"] == daoshou["id"]
        assert _phrase(edited.json(), "logo", "画面", "品牌")["id"] == logo_id

        stale = client.post(f"/v1/company-rules/submissions/{submission_id}/approve", headers=company_admin, json={})
        assert stale.status_code == 400
        assert "已经变了" in stale.json()["detail"]
        pending = client.get("/v1/company-rules/submissions/pending", headers=company_admin).json()
        assert submission_id in [item["id"] for item in pending]
        assert _phrase(_doc(client, member), "活动到手")["text"] == "活动到手"


def test_admin_cannot_approve_own_rule_submission(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        company_admin = _login(client, "li@jiecheng.com")
        created = _submit(
            client,
            company_admin,
            [{"op": "add", "target": "phrase", "after": {"text": "管理员自提", "category": "品牌", "source": "口播"}}],
        )
        assert created.status_code == 200, created.text
        submission_id = created.json()["items"][0]["id"]
        own = client.post(f"/v1/company-rules/submissions/{submission_id}/approve", headers=company_admin, json={})
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
        queued = client.get("/v1/company-rules/submissions/pending", headers=second).json()
        assert submission_id in [item["id"] for item in queued]
        passed = client.post(f"/v1/company-rules/submissions/{submission_id}/approve", headers=second, json={})
        assert passed.status_code == 200, passed.text
        texts = {item["text"] for item in _doc(client, company_admin)["phrases"]}
        assert "管理员自提" in texts
        assert "到手" in texts


def test_relay_token_reads_company_rules(tmp_path, monkeypatch):
    def fake_account(token: str) -> dict:
        if token != "relay-token-for-test":
            raise AssertionError("unexpected token")
        return {"id": 5, "email": "zhang@jiecheng.com"}

    monkeypatch.setattr("admin_app.auth_relay.current_account", fake_account)
    with _client(tmp_path, monkeypatch) as client:
        via_relay = client.get("/v1/company-rules", headers={"Authorization": "Bearer relay-token-for-test"})
        assert via_relay.status_code == 200, via_relay.text
        assert "到手" in {item["text"] for item in via_relay.json()["phrases"]}
        assert "relay-token-for-test" not in via_relay.text


def test_platform_admin_cannot_submit_company_rules(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        platform = _login(client, "admin@yujing.local")
        response = _submit(
            client,
            platform,
            [{"op": "add", "target": "phrase", "after": {"text": "平台词", "category": "价格", "source": "口播"}}],
        )
        assert response.status_code == 403
