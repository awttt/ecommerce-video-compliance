import json

import pytest
from fastapi.testclient import TestClient

from app.core.ffmpeg import frame_bounds
from app.factory import create_app
from app.schemas import Finding
from app.services.manager import BusyError, JobManager, JobState, _state_from_row, get_manager
from tests.media import ffmpeg_bin, has_encoder, make_video


def _job(job_id: str, **overrides) -> JobState:
    data = dict(
        id=job_id,
        filename="视频.mp4",
        source_path="视频.mp4",
        status="failed",
        stage="failed",
        message="全部分帧识别失败（2 帧）。模型返回 HTTP 503：Service temporarily unavailable",
        progress=20,
        fps=1,
        concurrency=1,
        duration_sec=2,
        frames_estimated=2,
        frames_extracted=2,
        frames_recognized=2,
        failed_frames=2,
        hit_count=0,
        error="全部分帧识别失败（2 帧）。模型返回 HTTP 503：Service temporarily unavailable",
        speech_stage="failed",
        speech_message="模型返回 HTTP 503：Service temporarily unavailable",
        asr_error=None,
        options={"asr_enabled": False, "mock": True, "fps": 1, "concurrency": 1},
        output_path=None,
        created_at="2026-11-13T00:00:00+00:00",
        updated_at="2026-11-13T00:00:00+00:00",
        finished_at="2026-11-13T00:01:00+00:00",
    )
    data.update(overrides)
    return JobState(**data)


def test_frame_bounds_follow_the_sampling_grid():
    assert frame_bounds(1, 1, 2) == (0.0, 1.0)
    assert frame_bounds(2, 1, 2) == (1.0, 2.0)
    assert frame_bounds(3, 1, 2) is None
    assert frame_bounds(0, 1, 2) is None


async def test_failed_indexes_roundtrip_and_ignore_bad_json(tmp_path):
    manager = JobManager()
    await manager.init()
    try:
        state = _job("a" * 32, failed_indexes=[3, 1])
        await manager.db.upsert_job(state.to_row())
        loaded = _state_from_row(await manager.db.get_job(state.id), [])
        assert loaded.failed_indexes == [3, 1]
        assert json.loads(state.to_row()["failed_frames_json"]) == [3, 1]
        await manager.db.conn.execute("UPDATE jobs SET failed_frames_json=? WHERE id=?", ("{", state.id))
        await manager.db.conn.commit()
        broken = _state_from_row(await manager.db.get_job(state.id), [])
        assert broken.failed_indexes == []
    finally:
        await manager.shutdown()


async def test_retry_queues_behind_existing_work_while_paused(tmp_path):
    manager = JobManager()
    await manager.init()
    try:
        video = tmp_path / "a.mp4"
        video.write_bytes(b"video")
        older = _job("b" * 32, status="queued", stage="queued", failed_frames=0, error=None, created_at="2020-01-01T00:00:00+00:00")
        manager._jobs[older.id] = older
        await manager.db.upsert_job(older.to_row())
        state = _job("a" * 32, source_path=str(video), failed_indexes=[1])
        manager._jobs[state.id] = state
        await manager.db.upsert_job(state.to_row())
        manager._queue_paused = True
        manager._resume.clear()
        queued = await manager.retry_failed_frames(state.id)
        assert queued.status == "queued"
        assert queued.retry_failed is True
        assert queued.error is None
        assert "前面还有 1 个" in queued.message
        assert queued.queue_at > older.created_at
        assert manager._tasks == {}
        row = await manager.db.get_job(state.id)
        assert row["status"] == "queued"
        assert json.loads(row["failed_frames_json"]) == [1]

        empty = _job("c" * 32, source_path=str(video), status="done", failed_frames=0, error=None)
        manager._jobs[empty.id] = empty
        with pytest.raises(BusyError, match="没有失败的帧"):
            await manager.retry_failed_frames(empty.id)
        running = _job("d" * 32, status="running", failed_frames=1)
        manager._jobs[running.id] = running
        with pytest.raises(BusyError, match="还在进行"):
            await manager.retry_failed_frames(running.id)
        missing = _job("e" * 32, source_path=str(tmp_path / "gone.mp4"), failed_frames=1)
        manager._jobs[missing.id] = missing
        with pytest.raises(FileNotFoundError):
            await manager.retry_failed_frames(missing.id)
    finally:
        await manager.shutdown()


async def test_restart_keeps_the_queue_paused_when_a_job_was_paused():
    manager = JobManager()
    await manager.init()
    paused = _job("a" * 32, status="paused", stage="recognizing", failed_frames=46, frames_recognized=46, frames_extracted=55)
    queued = _job("b" * 32, status="queued", stage="queued", failed_frames=0, error=None, message="排队中", created_at="2026-11-13T00:02:00+00:00")
    running = _job("c" * 32, status="running", stage="recognizing", failed_frames=0, error=None)
    await manager.db.upsert_job(paused.to_row())
    await manager.db.upsert_job(queued.to_row())
    await manager.db.upsert_job(running.to_row())
    await manager.shutdown()

    again = JobManager()
    await again.init()
    try:
        assert again.queue_paused is True
        assert again._resume.is_set() is False
        assert again._tasks == {}
        restored = again._jobs[queued.id]
        assert restored.status == "queued"
        paused_row = await again.db.get_job(paused.id)
        running_row = await again.db.get_job(running.id)
        queued_row = await again.db.get_job(queued.id)
        assert paused_row["status"] == "failed"
        assert paused_row["error"] == "interrupted"
        assert paused_row["message"] == "程序退出时任务未完成"
        assert paused_row["failed_frames"] == 46
        assert running_row["status"] == "failed"
        assert queued_row["status"] == "queued"
    finally:
        await again.shutdown()


def test_retry_route_missing_job_is_404():
    with TestClient(create_app()) as client:
        manager = get_manager()
        manager._settings["relay_email"] = "user@example.com"
        manager._settings["relay_access_token"] = "access-token-for-tests"
        missing = client.post(f"/api/jobs/{'ab' * 16}/retry-frames")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "任务不存在"


@pytest.mark.skipif(ffmpeg_bin() is None or not has_encoder("libx264"), reason="需要带 libx264 的 ffmpeg")
async def test_selective_retry_keeps_finished_speech_and_picture_hits(tmp_path):
    manager = JobManager()
    await manager.init()
    try:
        video = tmp_path / "a.mp4"
        make_video(video, 2)
        picture = Finding(
            time_range="0.0-1.0",
            category="价格",
            source="画面",
            text="¥199",
            position="画面右下角价格贴纸",
            confidence="确定",
            reason="这一帧写着价格",
            start_sec=0,
            end_sec=1,
        )
        speech = Finding(
            time_range="0.2-0.8",
            category="价格",
            source="口播",
            text="九十九元",
            position="口播内容",
            confidence="确定",
            reason="口播里连续说了这几个字",
            start_sec=0.2,
            end_sec=0.8,
        )
        state = _job(
            "f" * 32,
            source_path=str(video),
            status="done",
            stage="done",
            speech_stage="done",
            speech_message="口播完成",
            failed_frames=1,
            failed_indexes=[2],
            frames_estimated=2,
            frames_extracted=2,
            frames_recognized=2,
            options={"asr_enabled": True, "mock": True, "fps": 1, "concurrency": 1},
            findings=[picture, speech],
            hit_count=2,
            error=None,
        )
        manager._jobs[state.id] = state
        await manager.db.upsert_job(state.to_row())
        await manager.db.replace_findings(state.id, state.findings)
        manager._queue_paused = True
        manager._resume.clear()
        await manager.retry_failed_frames(state.id)
        assert state.retry_failed is True
        manager._queue_paused = False
        manager._resume.set()
        await manager.run(state.id)
        assert state.status == "done"
        assert state.failed_frames == 0
        assert state.speech_stage == "done"
        texts = {(item.source, item.text) for item in state.findings}
        assert ("画面", "¥199") in texts
        assert ("口播", "九十九元") in texts
        assert "部分帧识别失败" not in state.message
    finally:
        await manager.shutdown()
