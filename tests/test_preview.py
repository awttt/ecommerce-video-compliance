"""窗口播不了的原片转一份预览。识别和剪辑仍用原片。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.ffmpeg import FFmpegError, VideoProbe, probe_video, window_can_play
from app.factory import create_app
from app.services.manager import JobManager, JobState, get_manager, preview_file
from tests.media import ffmpeg_bin, has_encoder, make_video


def _probe(codec: str, pix: str) -> VideoProbe:
    return VideoProbe(1.0, True, True, 16, 16, 30, codec, pix)


def _job(job_id: str, **overrides) -> JobState:
    data = dict(
        id=job_id,
        filename="视频.mp4",
        source_path="视频.mp4",
        status="done",
        stage="done",
        message="完成",
        progress=100,
        fps=1,
        concurrency=1,
        duration_sec=1,
        frames_estimated=1,
        frames_extracted=1,
        frames_recognized=1,
        failed_frames=0,
        hit_count=0,
        error=None,
        speech_stage="",
        speech_message="",
        asr_error=None,
        options={"asr_enabled": False, "mock": True},
        output_path=None,
        created_at="2026-11-13T00:00:00+00:00",
        updated_at="2026-11-13T00:00:00+00:00",
        finished_at="2026-11-13T00:01:00+00:00",
    )
    data.update(overrides)
    return JobState(**data)


def _sign_in() -> None:
    manager = get_manager()
    manager._settings["relay_email"] = "user@example.com"
    manager._settings["relay_access_token"] = "access-token-for-tests"


def test_window_can_play_accepts_only_eight_bit_h264_mp4_and_webm():
    h264 = _probe("h264", "yuv420p")
    assert window_can_play(Path("a.mp4"), h264)
    assert window_can_play(Path("a.m4v"), h264)
    assert window_can_play(Path("a.MP4"), _probe("H264", "YUV420P"))
    assert window_can_play(Path("a.mp4"), _probe("h264", "yuvj420p"))
    assert window_can_play(Path("a.webm"), _probe("vp9", "yuv420p"))
    assert window_can_play(Path("a.webm"), _probe("vp8", ""))
    assert not window_can_play(Path("a.mp4"), _probe("hevc", "yuv420p"))
    assert not window_can_play(Path("a.mp4"), _probe("h265", "yuv420p"))
    assert not window_can_play(Path("a.mp4"), _probe("h264", "yuv420p10le"))
    assert not window_can_play(Path("a.mp4"), _probe("h264", "yuv444p"))
    assert not window_can_play(Path("a.mkv"), h264)
    assert not window_can_play(Path("a.mov"), h264)
    assert not window_can_play(Path("a.webm"), _probe("av1", "yuv420p"))
    assert not window_can_play(Path("a.mp4"), _probe("", ""))


def test_media_hides_the_original_until_a_preview_exists(tmp_path: Path):
    with TestClient(create_app()) as client:
        _sign_in()
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"original-bytes")
        state = _job("d" * 32, filename="clip.mp4", source_path=str(source), preview_status="pending")
        get_manager()._jobs[state.id] = state
        blocked = client.get(f"/api/jobs/{state.id}/media")
        assert blocked.status_code == 404
        assert blocked.json()["detail"] == "预览还没准备好"
        assert b"original-bytes" not in blocked.content

        state.preview_status = "direct"
        direct = client.get(f"/api/jobs/{state.id}/media")
        assert direct.status_code == 200
        assert direct.content == b"original-bytes"
        assert direct.headers["content-type"].startswith("video/mp4")

        preview = preview_file(state.id)
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"preview-bytes")
        state.preview_status = "ready"
        played = client.get(f"/api/jobs/{state.id}/media")
        assert played.status_code == 200
        assert played.content == b"preview-bytes"
        assert played.headers["content-type"].startswith("video/mp4")
        assert "preview.mp4" in played.headers["content-disposition"]

        state.preview_status = "direct"
        still_original = client.get(f"/api/jobs/{state.id}/media")
        assert still_original.content == b"original-bytes"


def test_unreadable_upload_still_creates_a_job():
    with TestClient(create_app()) as client:
        _sign_in()
        response = client.post(
            "/api/jobs",
            files={"file": ("坏片.mp4", b"not a video", "video/mp4")},
            data={"fps": "1", "concurrency": "1"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"]
        assert body["preview_status"] == "failed"
        assert body["preview_hint"] == "原片窗口播不了，预览没有生成。识别仍用原片。"


async def test_listing_jobs_does_not_probe(tmp_path: Path, monkeypatch):
    async def fail_probe(_path):
        raise AssertionError("列表不应该探测视频")

    monkeypatch.setattr("app.services.manager.probe_video", fail_probe)
    manager = JobManager()
    await manager.init()
    try:
        source = tmp_path / "later.mp4"
        source.write_bytes(b"not a video")
        state = _job("e" * 32, filename="later.mp4", source_path=str(source), status="done")
        await manager.db.upsert_job(state.to_row())
        listed = await manager.list_jobs()
        assert listed[0].preview_status == ""
        assert listed[0].preview_hint == ""
        preview = preview_file(state.id)
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"proxy")
        again = await manager.list_jobs()
        assert again[0].preview_status == "ready"
        assert "预览副本" in again[0].preview_hint
    finally:
        await manager.shutdown()


async def test_saved_preview_is_ready_without_another_probe(tmp_path: Path, monkeypatch):
    async def fail_probe(_path):
        raise AssertionError("已有预览文件时不应该再探测")

    monkeypatch.setattr("app.services.manager.probe_video", fail_probe)
    manager = JobManager()
    await manager.init()
    try:
        source = tmp_path / "a.mp4"
        source.write_bytes(b"original")
        state = _job("c" * 32, filename="a.mp4", source_path=str(source))
        preview = preview_file(state.id)
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"proxy")
        await manager.db.upsert_job(state.to_row())
        loaded = await manager.get(state.id)
        assert loaded.preview_status == "ready"
        assert manager.media_source(loaded).read_bytes() == b"proxy"
    finally:
        await manager.shutdown()


async def _hevc_probe(_path: Path) -> VideoProbe:
    return VideoProbe(1.0, True, False, 320, 240, 30, "hevc", "yuv420p")


async def test_cancelling_detection_leaves_the_preview_running(tmp_path: Path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def hang(_source, _dest, *, has_audio, cancel, on_process=None):
        del has_audio, on_process
        started.set()
        await release.wait()
        if cancel.is_set():
            raise FFmpegError("预览已取消")

    monkeypatch.setattr("app.services.manager.probe_video", _hevc_probe)
    monkeypatch.setattr("app.services.manager.transcode_preview", hang)
    manager = JobManager()
    await manager.init()
    try:
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"original")
        state = _job(
            "f" * 32,
            filename="clip.mp4",
            source_path=str(source),
            status="running",
            stage="extracting",
            finished_at=None,
        )
        manager._jobs[state.id] = state
        await manager.ensure_preview(state)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert state.preview_status == "pending"
        await manager.cancel(state.id)
        assert state.cancel_event.is_set()
        assert not state.preview_cancel.is_set()
        assert state.preview_task is not None and not state.preview_task.done()
        release.set()
        await asyncio.wait_for(state.preview_task, timeout=2)
        assert state.preview_status == "ready"
    finally:
        release.set()
        await manager.shutdown()


async def test_deleting_a_job_stops_the_preview(tmp_path: Path, monkeypatch):
    started = asyncio.Event()

    async def hang(source, dest, *, has_audio, cancel, on_process=None):
        del source, has_audio, on_process
        dest.parent.mkdir(parents=True, exist_ok=True)
        started.set()
        dest.write_bytes(b"partial")
        await cancel.wait()
        raise FFmpegError("预览已取消")

    monkeypatch.setattr("app.services.manager.probe_video", _hevc_probe)
    monkeypatch.setattr("app.services.manager.transcode_preview", hang)
    manager = JobManager()
    await manager.init()
    try:
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"original")
        state = _job("a" * 32, filename="clip.mp4", source_path=str(source), status="done")
        await manager.db.upsert_job(state.to_row())
        manager._jobs[state.id] = state
        await manager.ensure_preview(state)
        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.delete(state.id)
        assert state.preview_cancel.is_set()
        assert state.preview_task is not None and state.preview_task.done()
        assert not preview_file(state.id).exists()
        assert source.is_file()
    finally:
        await manager.shutdown()


def _needs_preview_encoders() -> bool:
    return bool(ffmpeg_bin() and has_encoder("libx264") and has_encoder("mpeg4") and has_encoder("aac"))


def _make_mpeg4(path: Path) -> None:
    binary = ffmpeg_bin()
    assert binary is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1920x1080:d=0.4",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(not ffmpeg_bin() or not has_encoder("libx264"), reason="没有 libx264")
async def test_h264_mp4_plays_directly_and_is_probed_again_after_restart(tmp_path: Path):
    video = tmp_path / "ok.mp4"
    make_video(video, 0.4)
    manager = JobManager()
    await manager.init()
    try:
        state = _job("b" * 32, filename="ok.mp4", source_path=str(video))
        await manager.db.upsert_job(state.to_row())
        loaded = await manager.get(state.id)
        assert loaded.preview_status == "direct"
        assert loaded.to_public().preview_hint == ""
        assert manager.media_source(loaded) == video
        probed = await probe_video(video)
        assert probed.codec == "h264"
        assert probed.pix_fmt == "yuv420p"
    finally:
        await manager.shutdown()

    again = JobManager()
    await again.init()
    try:
        reopened = await again.get("b" * 32)
        assert reopened.preview_status == "direct"
        assert again.media_source(reopened) == video
    finally:
        await again.shutdown()


@pytest.mark.skipif(not _needs_preview_encoders(), reason="没有 mpeg4 或 libx264")
async def test_other_mp4_becomes_an_h264_preview_and_keeps_the_original(tmp_path: Path):
    source = tmp_path / "wide.mp4"
    _make_mpeg4(source)
    manager = JobManager()
    await manager.init()
    try:
        state = _job("9" * 32, filename="wide.mp4", source_path=str(source), status="running", stage="extracting", finished_at=None)
        manager._jobs[state.id] = state
        await manager.ensure_preview(state)
        assert state.preview_status == "pending"
        assert state.to_public().preview_hint == "原片窗口播不了，正在生成预览。识别仍用原片。"
        with pytest.raises(FileNotFoundError):
            manager.media_source(state)
        state.cancel_event.set()
        assert state.preview_task is not None
        await asyncio.wait_for(state.preview_task, timeout=30)
        assert state.preview_status == "ready"
        assert not state.preview_cancel.is_set()
        preview = manager.media_source(state)
        assert preview.name == "preview.mp4"
        assert preview != source
        copied = await probe_video(preview)
        original = await probe_video(source)
        assert original.codec == "mpeg4"
        assert copied.codec == "h264"
        assert copied.pix_fmt == "yuv420p"
        assert copied.has_audio is True
        assert max(copied.width, copied.height) <= 1280
        assert state.source_path == str(source)
        public = state.to_public()
        assert public.preview_status == "ready"
        assert public.preview_hint == "窗口播放的是预览副本，识别仍用原片。"
        assert not preview.with_name("preview.part.mp4").exists()
    finally:
        await manager.shutdown()
