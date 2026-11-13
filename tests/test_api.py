import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.paths import audio_dir, frames_dir, video_dir
from app.services.manager import JobState, _fold_pause, get_manager, reset_manager
from tests.media import ffmpeg_bin, has_encoder, make_video, probe_duration


def sign_in() -> None:
    manager = get_manager()
    manager._settings["relay_email"] = "user@example.com"
    manager._settings["relay_access_token"] = "access-token-for-tests"


def test_health_and_settings_do_not_leak_the_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-example-9876")
    with TestClient(create_app()) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        hidden = client.get("/api/settings")
        assert hidden.status_code == 200
        body = hidden.json()
        assert body["api_key_source"] == "env"
        assert body["has_api_key"] is True
        assert "sk-env-example-9876" not in hidden.text
        assert body["api_key_masked"].endswith("9876")
        saved = client.put("/api/settings", json={"api_key": "sk-saved-4321", "fps": 0.5, "concurrency": 2})
        assert saved.status_code == 200
        assert "sk-saved-4321" not in saved.text
        assert saved.json()["api_key_source"] == "saved"
        assert saved.json()["fps"] == 0.5
        assert saved.json()["asr_enabled"] is True
        secret = "AKIDEXAMPLESECRET1234"
        key = "secretkey-example-5678"
        stored = client.put(
            "/api/settings",
            json={
                "tencent_secret_id": secret,
                "tencent_secret_key": key,
                "tencent_region": "ap-guangzhou",
                "asr_enabled": False,
            },
        )
        assert stored.status_code == 200
        assert secret not in stored.text
        assert key not in stored.text
        assert stored.json()["tencent_secret_source"] == "saved"
        assert stored.json()["tencent_secret_id_masked"].endswith("1234")
        assert stored.json()["asr_enabled"] is False
        assert stored.json()["tencent_region"] == "ap-guangzhou"
        cleared_secret = client.put("/api/settings", json={"clear_tencent_secret": True, "asr_enabled": True})
        assert cleared_secret.json()["tencent_secret_source"] == "none"
        assert cleared_secret.json()["has_tencent_secret"] is False
        cleared = client.put("/api/settings", json={"clear_api_key": True})
        assert cleared.json()["api_key_source"] == "env"


def test_custom_rules_roundtrip_and_reject_an_empty_rule(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-example-9876")
    with TestClient(create_app()) as client:
        saved = client.put(
            "/api/settings",
            json={
                "rules": {
                    "categories": ["规格", "规格"],
                    "extra_phrases": [{"text": "全网最低", "category": "价格"}],
                    "ignore_phrases": ["包邮"],
                    "note": "只看标签上的原字",
                }
            },
        )
        assert saved.status_code == 200, saved.text
        rules = saved.json()["rules"]
        assert rules["categories"] == ["规格"]
        assert rules["extra_phrases"] == [{"text": "全网最低", "category": "价格", "source": "口播"}]
        assert rules["ignore_phrases"] == ["包邮"]
        assert "sk-" not in saved.text
        again = client.get("/api/settings")
        assert again.json()["rules"]["note"] == "只看标签上的原字"
        empty = client.put(
            "/api/settings",
            json={"rules": {"categories": [], "extra_phrases": [], "ignore_phrases": [], "note": ""}},
        )
        assert empty.status_code == 422


def test_batch_upload_accepts_several_files(monkeypatch):
    monkeypatch.setenv("EVC_INLINE_JOBS", "1")
    with TestClient(create_app()) as client:
        sign_in()
        response = client.post(
            "/api/jobs/batch",
            files=[
                ("files", ("第一段.mp4", b"not a video", "video/mp4")),
                ("files", ("第二段.mp4", b"not a video", "video/mp4")),
                ("files", ("说明.txt", b"text", "text/plain")),
            ],
            data={"fps": "1", "concurrency": "1"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["filename"] for item in body["jobs"]] == ["第一段.mp4", "第二段.mp4"]
        assert {item["status"] for item in body["jobs"]} == {"failed"}
        assert all("API Key" in item["message"] for item in body["jobs"])
        assert body["errors"][0]["filename"] == "说明.txt"
        assert "不支持" in body["errors"][0]["detail"]
        listed = client.get("/api/jobs")
        assert len(listed.json()) == 2


def test_missing_api_key_fails_before_calling_ffmpeg(monkeypatch):
    monkeypatch.setenv("EVC_INLINE_JOBS", "1")
    with TestClient(create_app()) as client:
        sign_in()
        response = client.post(
            "/api/jobs",
            files={"file": ("空视频.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "API Key" in body["message"]
    assert body["started_at"]
    assert body["finished_at"]
    assert body["started_at"] <= body["finished_at"]
    assert "sk-" not in response.text


@pytest.mark.skipif(ffmpeg_bin() is None or not has_encoder("libx264"), reason="需要带 libx264 的 ffmpeg")
def test_upload_mock_pipeline_export_and_cleanup(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EVC_INLINE_JOBS", "1")
    monkeypatch.setenv("EVC_MOCK", "1")
    video = tmp_path / "素材 库" / "价格 演示.mp4"
    make_video(video, 2)
    with TestClient(create_app()) as client:
        sign_in()
        with video.open("rb") as handle:
            response = client.post(
                "/api/jobs",
                files={"file": ("价格 演示.mp4", handle, "video/mp4")},
                data={"fps": "1", "concurrency": "2"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "done", body
        assert body["mock"] is True
        assert body.get("asr_error") in (None, "")
        visual = next(item for item in body["findings"] if item["来源"] == "画面")
        oral = next(item for item in body["findings"] if item["来源"] == "口播")
        assert visual["原文"] == "¥199"
        assert visual["类别"] == "价格"
        assert oral["原文"] == "这款只要九十九元"
        assert oral["位置"] == "口播内容"
        assert oral["类别"] == "价格"
        assert oral["置信度"] == "确定"
        assert "时间点" in oral
        assert not frames_dir(body["id"]).exists()
        assert not audio_dir(body["id"]).exists()
        saved = video_dir(body["id"]) / "价格 演示.mp4"
        assert saved.is_file()

        exported = client.get(f"/api/jobs/{body['id']}/export", params={"fmt": "json"})
        assert exported.status_code == 200
        payload = json.loads(exported.content.decode("utf-8"))
        exported_visual = next(item for item in payload["结果"] if item["来源"] == "画面")
        exported_oral = next(item for item in payload["结果"] if item["来源"] == "口播")
        assert exported_visual["原文"] == "¥199"
        assert exported_oral["原文"] == "这款只要九十九元"
        assert set(exported_visual) == {"时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注"}

        sheet = client.get(f"/api/jobs/{body['id']}/export", params={"fmt": "xlsx"})
        assert sheet.status_code == 200
        assert sheet.content[:2] == b"PK"
        from io import BytesIO

        from openpyxl import load_workbook

        book = load_workbook(BytesIO(sheet.content))
        header = [cell.value for cell in book.active[1]]
        assert header[:3] == ["时间点", "类别", "来源"]
        assert body["cuts"] == []
        assert body["has_edit_plan"] is False
        assert body["edit_rendered"] is False

        early = client.post(f"/api/jobs/{body['id']}/cut/render")
        assert early.status_code == 400
        assert "还没有剪辑方案" in early.text

        output = video_dir(body["id"]) / "clean.mp4"
        planned = client.post(f"/api/jobs/{body['id']}/edit/plan", json={"padding_sec": 0.2})
        assert planned.status_code == 200, planned.text
        assert planned.json()["has_edit_plan"] is True
        assert planned.json()["edit_rendered"] is False
        assert planned.json()["cuts"]
        assert not output.exists()
        assert "整句" not in planned.json()["message"]

        strip = client.get(f"/api/jobs/{body['id']}/strip/0")
        assert strip.status_code == 200, strip.text
        assert strip.headers["content-type"].startswith("image/jpeg")
        assert strip.content[:2] == b"\xff\xd8"
        missing = client.get(f"/api/jobs/{body['id']}/strip/99")
        assert missing.status_code == 404

        saved = client.put(
            f"/api/jobs/{body['id']}/cuts",
            json={"cuts": [{"start_sec": 1.0, "end_sec": 0.4}]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["cuts"] == [{"start_sec": 0.4, "end_sec": 1.0}]
        assert saved.json()["edit_rendered"] is False
        assert not output.exists()

        rendered = client.post(f"/api/jobs/{body['id']}/cut/render")
        assert rendered.status_code == 200, rendered.text
        assert rendered.json()["edit_rendered"] is True
        assert rendered.json()["cuts"] == [{"start_sec": 0.4, "end_sec": 1.0}]
        assert output.is_file()
        assert 1.1 < probe_duration(output) < 1.8

        cut = client.post(f"/api/jobs/{body['id']}/cut", json={"padding_sec": 0.1})
        assert cut.status_code == 200, cut.text
        assert cut.json()["output_ready"] is True
        output = video_dir(body["id"]) / "clean.mp4"
        assert output.is_file()
        assert 0.3 < probe_duration(output) < 2.2


def _clock_job(status: str) -> JobState:
    return JobState(
        id="a" * 32,
        filename="计时.mp4",
        source_path="",
        status=status,
        stage="recognizing",
        message="",
        progress=10,
        fps=1,
        concurrency=1,
        duration_sec=10,
        frames_estimated=1,
        frames_extracted=1,
        frames_recognized=0,
        failed_frames=0,
        hit_count=0,
        error=None,
        speech_stage="",
        speech_message="",
        asr_error=None,
        options={},
        output_path=None,
        created_at="2026-11-13T00:00:00+00:00",
        updated_at="2026-11-13T00:00:00+00:00",
        finished_at=None,
    )


def test_fold_pause_removes_the_gap_from_a_running_job():
    state = _clock_job("paused")
    state.started_at = "2026-11-13T00:00:00+00:00"
    state.paused_at = "2026-11-13T00:00:10+00:00"
    _fold_pause(state, "2026-11-13T00:00:16+00:00")
    assert state.paused_total_sec == 6
    assert state.hold_sec == 6
    assert state.paused_at is None


def test_fold_pause_keeps_a_queued_job_timer_clear_until_it_starts():
    state = _clock_job("queued")
    state.paused_at = "2026-11-13T00:00:10+00:00"
    _fold_pause(state, "2026-11-13T00:00:16+00:00")
    assert state.paused_total_sec == 0
    assert state.hold_sec == 6
    assert state.paused_at is None


def test_pause_stamps_an_open_interval_until_resume():
    with TestClient(create_app()) as client:
        sign_in()
        state = _clock_job("running")
        state.started_at = "2026-11-13T00:00:00+00:00"
        get_manager()._jobs[state.id] = state
        assert client.post("/api/queue/pause").json()["paused"] is True
        paused = client.get(f"/api/jobs/{state.id}").json()
        assert paused["status"] == "paused"
        assert paused["paused_at"]
        assert paused["paused_total_sec"] == 0
        assert client.post("/api/queue/resume").json()["paused"] is False
        resumed = client.get(f"/api/jobs/{state.id}").json()
        assert resumed["status"] == "running"
        assert resumed["paused_at"] is None
        assert resumed["paused_total_sec"] >= 0
        assert resumed["hold_sec"] == resumed["paused_total_sec"]


def test_pause_resume_and_delete_history(monkeypatch):
    monkeypatch.setenv("EVC_INLINE_JOBS", "1")
    with TestClient(create_app()) as client:
        sign_in()
        created = client.post(
            "/api/jobs",
            files={"file": ("空视频.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]
        assert client.get("/api/queue").json() == {"paused": False}
        paused = client.post("/api/queue/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json() == {"paused": True}
        assert client.get("/api/queue").json()["paused"] is True
        resumed = client.post("/api/queue/resume")
        assert resumed.json() == {"paused": False}
        cleared = client.delete("/api/jobs")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json() == {"ok": True, "deleted": 1}
        assert client.get("/api/jobs").json() == []
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert not video_dir(job_id).exists()
        again = client.delete("/api/jobs")
        assert again.json()["deleted"] == 0


def test_clear_queue_keeps_finished_jobs(monkeypatch):
    monkeypatch.setenv("EVC_INLINE_JOBS", "1")
    with TestClient(create_app()) as client:
        sign_in()
        finished = client.post(
            "/api/jobs",
            files={"file": ("已结束.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
        assert finished.status_code == 200, finished.text
        finished_id = finished.json()["id"]
        assert finished.json()["status"] in {"failed", "done"}
        assert client.post("/api/queue/pause").json()["paused"] is True
        waiting = client.post(
            "/api/jobs",
            files={"file": ("排队.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
        assert waiting.status_code == 200, waiting.text
        waiting_id = waiting.json()["id"]
        assert waiting.json()["status"] == "queued"
        assert video_dir(waiting_id).exists()
        cleared = client.delete("/api/queue")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json() == {"ok": True, "deleted": 1}
        assert client.get("/api/queue").json()["paused"] is True
        body = client.get("/api/jobs").json()
        ids = [item["id"] for item in body]
        assert waiting_id not in ids
        assert finished_id in ids
        assert not video_dir(waiting_id).exists()
        assert video_dir(finished_id).exists()
        assert client.delete("/api/queue").json()["deleted"] == 0


def test_queue_pause_survives_restart():
    with TestClient(create_app()) as client:
        sign_in()
        assert client.post("/api/queue/pause").json()["paused"] is True
        created = client.post(
            "/api/jobs",
            files={"file": ("排队.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]
        assert created.json()["status"] == "queued"
    reset_manager()
    with TestClient(create_app()) as client:
        sign_in()
        assert client.get("/api/queue").json()["paused"] is True
        time.sleep(0.3)
        held = client.get(f"/api/jobs/{job_id}")
        assert held.status_code == 200, held.text
        assert held.json()["status"] == "queued"


def test_pause_keeps_new_jobs_queued_until_resume():
    with TestClient(create_app()) as client:
        sign_in()
        assert client.post("/api/queue/pause").json()["paused"] is True
        created = client.post(
            "/api/jobs",
            files={"file": ("空视频.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["status"] == "queued"
        time.sleep(0.3)
        held = client.get(f"/api/jobs/{body['id']}")
        assert held.json()["status"] == "queued"
        assert "API Key" not in held.json()["message"]
        assert client.post("/api/queue/resume").json()["paused"] is False
        deadline = time.time() + 5
        status = "queued"
        message = ""
        while time.time() < deadline:
            current = client.get(f"/api/jobs/{body['id']}").json()
            status = current["status"]
            message = current["message"]
            if status in {"failed", "done", "cancelled"}:
                break
            time.sleep(0.05)
        assert status == "failed"
        assert "API Key" in message


def test_feature_routes_require_login():
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/settings").status_code == 200
        listed = client.get("/api/jobs")
        assert listed.status_code == 401
        assert listed.json()["detail"] == "请先登录"
        created = client.post(
            "/api/jobs",
            files={"file": ("空视频.mp4", b"not a video", "video/mp4")},
        )
        assert created.status_code == 401
        assert created.json()["detail"] == "请先登录"
        batch = client.post(
            "/api/jobs/batch",
            files=[("files", ("第一段.mp4", b"not a video", "video/mp4"))],
        )
        assert batch.status_code == 401
        assert batch.json()["detail"] == "请先登录"
        opened = client.post("/api/system/open-dir", json={"target": "exports"})
        assert opened.status_code == 401
        assert opened.json()["detail"] == "请先登录"
        assert client.get("/api/queue").status_code == 401
        assert client.post("/api/queue/pause").status_code == 401
        assert client.post("/api/queue/resume").status_code == 401
        assert client.delete("/api/jobs").status_code == 401
        sign_in()
        assert client.get("/api/jobs").status_code == 200
