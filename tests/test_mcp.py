"""MCP 工具。不读取真实密钥，也不改识别流水线。"""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.mcp_server import (
    _batches,
    batch_report,
    cancel_job,
    cut_job,
    export_job,
    get_findings,
    get_job,
    health,
    list_jobs,
    scan_folder,
    submit_video,
)
from app.schemas import Finding
from app.services.manager import BusyError, JobState, get_manager, reset_manager
from tests.media import ffmpeg_bin, has_encoder, make_video

_COLUMNS = {"时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注"}


def _finding(text: str, start: float = 1) -> Finding:
    return Finding(
        时间点="00:00:01-00:00:02",
        类别="价格",
        来源="画面",
        原文=text,
        位置="画面中部",
        置信度="确定",
        判定理由="画面上有这个价格",
        备注="",
        start_sec=start,
        end_sec=start + 1,
    )


def _done(path: Path, findings: list[Finding]) -> JobState:
    now = "2026-11-14T00:00:00Z"
    return JobState(
        id=uuid.uuid4().hex,
        filename=path.name,
        source_path=str(path),
        status="done",
        stage="done",
        message="识别完成",
        progress=100,
        fps=1,
        concurrency=1,
        duration_sec=2,
        frames_estimated=1,
        frames_extracted=1,
        frames_recognized=1,
        failed_frames=0,
        hit_count=len(findings),
        error=None,
        speech_stage="",
        speech_message="",
        asr_error=None,
        options={"mock": True},
        output_path=None,
        created_at=now,
        updated_at=now,
        finished_at=now,
        findings=findings,
    )


def _sign_in() -> None:
    manager = get_manager()
    manager._settings["relay_email"] = "user@example.com"
    manager._settings["relay_access_token"] = "access-token-for-tests"


def _mcp_post(client: TestClient):
    return client.post(
        "/mcp",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


def test_mcp_stays_off_and_the_api_still_works():
    with TestClient(create_app()) as client:
        health_response = client.get("/api/health")
        assert health_response.status_code == 200
        assert health_response.json()["mcp"] is False
        assert _mcp_post(client).status_code != 200


def test_a_broken_mount_does_not_stop_the_api(monkeypatch):
    monkeypatch.setenv("EVC_MCP", "1")
    import app.mcp_server as mcp_server

    def boom(app):
        raise RuntimeError("挂载失败")

    monkeypatch.setattr(mcp_server, "attach_mcp", boom)
    with TestClient(create_app()) as client:
        health_response = client.get("/api/health")
        assert health_response.status_code == 200
        assert health_response.json()["ok"] is True
        assert health_response.json()["mcp"] is False


def test_env_off_overrides_a_saved_flag(monkeypatch):
    with TestClient(create_app()) as client:
        saved = client.put("/api/settings", json={"mcp_enabled": True})
        assert saved.status_code == 200
        assert saved.json()["mcp_enabled"] is True
    reset_manager()
    monkeypatch.setenv("EVC_MCP", "0")
    with TestClient(create_app()) as client:
        health_response = client.get("/api/health")
        assert health_response.status_code == 200
        assert health_response.json()["mcp"] is False
        assert _mcp_post(client).status_code != 200


def test_saved_flag_mounts_on_the_next_start():
    with TestClient(create_app()) as client:
        assert client.put("/api/settings", json={"mcp_enabled": True}).status_code == 200
    reset_manager()
    with TestClient(create_app(), base_url="http://127.0.0.1") as client:
        health_response = client.get("/api/health")
        assert health_response.status_code == 200
        assert health_response.json()["mcp"] is True
        listed = _mcp_post(client)
        assert listed.status_code == 200, listed.text
        assert "scan_folder" in listed.text
        assert "submit_video" in listed.text
        assert "sk-" not in listed.text


async def test_submit_requires_login_and_hides_secrets(tmp_path: Path):
    manager = get_manager()
    await manager.init()
    manager._settings["api_key"] = "sk-saved-secret-9876"
    text = await submit_video(str(tmp_path / "价格.mp4"))
    body = json.loads(text)
    assert body == {"ok": False, "error": "请先登录"}
    assert manager._jobs == {}
    assert "sk-saved-secret-9876" not in text
    assert "sk-" not in await health()
    await manager.shutdown()


async def test_submit_rejects_a_bad_file_before_queueing(tmp_path: Path):
    manager = get_manager()
    await manager.init()
    _sign_in()
    empty = tmp_path / "空.mp4"
    empty.write_bytes(b"")
    note = tmp_path / "说明.txt"
    note.write_text("文本", encoding="utf-8")
    assert "视频文件是空的" in await submit_video(str(empty))
    assert "不支持的视频格式" in await submit_video(str(note))
    assert manager._jobs == {}
    await manager.shutdown()


async def test_tools_report_a_finished_job_without_secrets(tmp_path: Path, monkeypatch):
    manager = get_manager()
    await manager.init()
    _sign_in()
    video = tmp_path / "价格.mp4"
    video.write_bytes(b"video")
    state = _done(video, [_finding("¥199")])
    state.error = "失败 sk-saved-secret-9876"
    state.asr_error = "AKIDEXAMPLESECRET1234"
    manager._jobs[state.id] = state

    body = json.loads(await get_job(state.id))
    assert body["ok"] is True
    assert body["id"] == state.id
    assert "sk-saved-secret-9876" not in json.dumps(body)
    assert "AKIDEXAMPLESECRET1234" not in json.dumps(body)
    assert "***" in body["error"]

    findings = json.loads(await get_findings(state.id))
    assert findings["findings_total"] == 1
    assert set(findings["findings"][0]) >= _COLUMNS | {"视频", "start_sec", "end_sec"}
    assert findings["findings"][0]["原文"] == "¥199"

    exported = json.loads(await export_job(state.id, "json"))
    payload = json.loads(Path(exported["export_path"]).read_text(encoding="utf-8"))
    assert set(payload["结果"][0]) == _COLUMNS
    assert payload["结果"][0]["原文"] == "¥199"

    output = tmp_path / "clean.mp4"
    output.write_bytes(b"mp4")

    async def fake_cut(job_id: str, padding_sec: float) -> JobState:
        assert job_id == state.id
        assert padding_sec == 0.4
        state.output_path = str(output)
        state.message = "已生成剪辑视频"
        return state

    monkeypatch.setattr(manager, "cut", fake_cut)
    cut = json.loads(await cut_job(state.id, 0.4))
    assert cut["output_path"] == str(output)
    assert "边距需要在 0 到 2 秒之间" in await cut_job(state.id, 3)

    async def fake_cancel(job_id: str) -> JobState:
        state.status = "cancelled"
        state.message = "已取消"
        return state

    monkeypatch.setattr(manager, "cancel", fake_cancel)
    cancelled = json.loads(await cancel_job(state.id))
    assert cancelled["status"] == "cancelled"
    await manager.shutdown()


async def test_scan_folder_retries_a_full_queue_and_returns_one_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.mcp_server._POLL_SEC", 0.01)
    folder = tmp_path / "clips"
    folder.mkdir()
    (folder / "甲.mp4").write_bytes(b"video")
    (folder / "乙.mp4").write_bytes(b"video")
    (folder / "说明.txt").write_bytes(b"text")
    outside = tmp_path / "外面.mp4"
    outside.write_bytes(b"video")
    (folder / "链接.mp4").symlink_to(outside)
    manager = get_manager()
    await manager.init()
    _sign_in()
    calls = {"n": 0}

    async def fake_create(source: Path, filename: str, **kwargs) -> JobState:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BusyError("排队已满（最多 20 个），请等一部分完成后再上传")
        text = "¥199" if filename.startswith("甲") else "5支"
        state = _done(source, [_finding(text)])
        manager._jobs[state.id] = state
        return state

    monkeypatch.setattr(manager, "create_job", fake_create)
    notes: list[str] = []

    async def progress(done: int, total: int, message: str) -> None:
        notes.append(message)

    body = json.loads(await scan_folder(str(folder), progress=progress))
    assert body["ok"] is True, body
    assert calls["n"] == 3
    assert {item["filename"] for item in body["summary"]} == {"甲.mp4", "乙.mp4"}
    assert {item["status"] for item in body["summary"]} == {"done"}
    assert {item["filename"] for item in body["skipped"]} == {"说明.txt", "链接.mp4"}
    assert {row["原文"] for row in body["findings"]} == {"¥199", "5支"}
    assert Path(body["export_path"]).read_bytes()[:2] == b"PK"
    assert notes
    again = json.loads(await batch_report(body["batch_id"]))
    assert again["export_path"] == body["export_path"]
    assert json.loads(await batch_report("missing"))["ok"] is False
    await manager.shutdown()


async def test_one_video_and_a_long_report_still_export(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.mcp_server._POLL_SEC", 0.01)
    folder = tmp_path / "only"
    folder.mkdir()
    video = folder / "一条.mp4"
    video.write_bytes(b"video")
    manager = get_manager()
    await manager.init()
    _sign_in()
    findings = [_finding(f"价{index}", start=index * 10) for index in range(201)]

    async def fake_create(source: Path, filename: str, **kwargs) -> JobState:
        state = _done(source, findings)
        manager._jobs[state.id] = state
        return state

    monkeypatch.setattr(manager, "create_job", fake_create)
    body = json.loads(await scan_folder(str(folder)))
    assert body["ok"] is True, body
    assert body["findings_omitted"] is True
    assert body["findings_total"] == 201
    assert "findings" not in body
    assert "明细在" in body["message"]
    assert Path(body["export_path"]).is_file()
    running = json.loads(await batch_report("没有这批"))
    assert running["ok"] is False
    await manager.shutdown()


async def test_scan_folder_can_be_read_again_while_it_is_running(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.mcp_server._POLL_SEC", 0.01)
    folder = tmp_path / "wait"
    folder.mkdir()
    video = folder / "等待.mp4"
    video.write_bytes(b"video")
    manager = get_manager()
    await manager.init()
    _sign_in()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_create(source: Path, filename: str, **kwargs) -> JobState:
        started.set()
        await release.wait()
        state = _done(source, [_finding("¥199")])
        manager._jobs[state.id] = state
        return state

    monkeypatch.setattr(manager, "create_job", fake_create)
    task = asyncio.create_task(scan_folder(str(folder)))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        batch_id = next(iter(_batches))
        progress = json.loads(await batch_report(batch_id))
        assert progress["status"] == "running"
        assert progress["total"] == 1
        release.set()
        done = json.loads(await task)
        assert done["ok"] is True
        assert done["findings"][0]["原文"] == "¥199"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await manager.shutdown()


async def test_missing_folder_does_not_create_a_batch(tmp_path: Path):
    manager = get_manager()
    await manager.init()
    _sign_in()
    missing = tmp_path / "没有这个文件夹"
    body = json.loads(await scan_folder(str(missing)))
    assert body["ok"] is False
    assert "找不到这个文件夹" in body["error"]
    assert _batches == {}
    await manager.shutdown()


@pytest.mark.skipif(ffmpeg_bin() is None or not has_encoder("libx264"), reason="需要带 libx264 的 ffmpeg")
async def test_scan_folder_mock_pipeline_matches_export_columns(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EVC_MOCK", "1")
    monkeypatch.setenv("EVC_INLINE_JOBS", "1")
    folder = tmp_path / "素材"
    make_video(folder / "价格 演示.mp4", 1)
    (folder / "说明.txt").write_text("不要当视频", encoding="utf-8")
    manager = get_manager()
    await manager.init()
    _sign_in()
    manager._settings["api_key"] = "sk-should-not-appear-1234"
    text = await scan_folder(str(folder))
    assert "sk-should-not-appear-1234" not in text
    body = json.loads(text)
    assert body["ok"] is True, body
    assert body["summary"][0]["status"] == "done"
    assert body["skipped"][0]["filename"] == "说明.txt"
    texts = {row["原文"] for row in body["findings"]}
    assert "¥199" in texts
    assert "这款只要九十九元" in texts
    assert set(body["findings"][0]) >= _COLUMNS | {"视频", "start_sec", "end_sec"}
    saved = Path(body["export_path"])
    assert saved.is_file()
    assert saved.read_bytes()[:2] == b"PK"
    follow = json.loads(await get_findings(body["summary"][0]["job_id"]))
    assert {row["原文"] for row in follow["findings"]} == texts
    await manager.shutdown()
