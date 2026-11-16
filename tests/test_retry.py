import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.deepseek import SpeechJudgeError
from app.core.ffmpeg import frame_bounds
from app.core.speech import SpeechProgress, run_speech
from app.core.transcript import SpeechSentence, SpeechWord, dump_sentences, load_sentences
from app.factory import create_app
from app.schemas import Finding, RawHit
from app.services.manager import BusyError, JobManager, JobState, _state_from_row, get_manager, speech_retry_kind
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


def _sentence() -> list[dict]:
    return dump_sentences(
        [SpeechSentence("这款只要九十九元", 0.2, 0.8, [SpeechWord("九十九元", 0.2, 0.8)])]
    )


def _picture() -> Finding:
    return Finding(
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


def _spoken(text: str = "旧口播") -> Finding:
    return Finding(
        time_range="0.2-0.8",
        category="价格",
        source="口播",
        text=text,
        position="口播内容",
        confidence="确定",
        reason="口播里连续说了这几个字",
        start_sec=0.2,
        end_sec=0.8,
    )


def test_transcript_roundtrip_keeps_word_times():
    raw = _sentence()
    loaded = load_sentences(json.dumps(raw))
    assert loaded[0].text == "这款只要九十九元"
    assert loaded[0].words[0].text == "九十九元"
    assert loaded[0].words[0].end_sec == 0.8
    assert load_sentences("{") == []
    assert load_sentences([]) == []


def test_speech_retry_kind_splits_transcribe_and_judge():
    options = {"asr_enabled": True, "mock": True}
    transcribe = _job(
        "a" * 32,
        status="done",
        speech_stage="failed",
        speech_message="腾讯云识别失败",
        asr_error="腾讯云识别失败",
        options=options,
    )
    assert speech_retry_kind(transcribe) == "transcribe"
    assert transcribe.to_public().speech_retry == "transcribe"
    silent = _job(
        "b" * 32,
        status="done",
        speech_stage="failed",
        speech_message="视频没有音轨，已跳过口播检测",
        asr_error="视频没有音轨，已跳过口播检测",
        options=options,
    )
    assert speech_retry_kind(silent) == ""
    logged_out = _job(
        "c" * 32,
        status="done",
        speech_stage="failed",
        speech_message="未登录，已跳过口播检测",
        asr_error="未登录，已跳过口播检测",
        options=options,
    )
    assert speech_retry_kind(logged_out) == ""
    judged = _job(
        "d" * 32,
        status="done",
        speech_stage="done",
        speech_judge="partial",
        speech_message="模型请求超时，已按词表保留 1 条",
        speech_transcript=_sentence(),
        options=options,
    )
    assert speech_retry_kind(judged) == "judge"
    public = judged.to_public().model_dump()
    assert public["speech_retry"] == "judge"
    assert "这款只要九十九元" not in json.dumps(public, ensure_ascii=False)
    finished = _job(
        "e" * 32,
        status="done",
        speech_stage="done",
        speech_judge="ok",
        speech_message="口播判定完成，命中 1 条",
        speech_transcript=_sentence(),
        options=options,
    )
    assert speech_retry_kind(finished) == ""
    legacy = _job(
        "f" * 32,
        status="done",
        speech_stage="done",
        speech_message="模型请求超时，已按词表保留 1 条",
        options=options,
    )
    assert speech_retry_kind(legacy) == "transcribe"
    muted = _job("1" * 32, status="done", speech_stage="failed", options={"asr_enabled": False})
    assert speech_retry_kind(muted) == ""


async def test_transcript_is_saved_before_the_model_judges(tmp_path, monkeypatch):
    async def fake_probe(_video):
        return SimpleNamespace(has_audio=True, duration=1.0)

    async def fake_pieces(*_args, **_kwargs):
        return [(0.0, tmp_path / "speech.wav")]

    monkeypatch.setattr("app.core.speech.probe_video", fake_probe)
    monkeypatch.setattr("app.core.speech._pieces", fake_pieces)
    order: list[str] = []

    class Asr:
        async def transcribe(self, _pieces, *, cancel, on_status, resume=None):
            order.append("asr")
            return [SpeechSentence("九十九元", 0.1, 0.4, [SpeechWord("九十九元", 0.1, 0.4)])]

    class Judge:
        async def judge_speech(self, _sentences):
            order.append("judge")
            raise RuntimeError("模型请求超时")

    async def on_progress(_progress):
        return None

    async def on_transcript(sentences):
        order.append("save")
        assert sentences[0].words[0].start_sec == 0.1

    with pytest.raises(RuntimeError, match="^模型请求超时$"):
        await run_speech(
            tmp_path / "v.mp4",
            tmp_path / "work",
            Asr(),
            Judge(),
            asyncio.Event(),
            on_progress,
            on_transcript=on_transcript,
        )
    assert order == ["asr", "save", "judge"]


async def test_partial_judgment_is_marked_for_a_text_retry(tmp_path, monkeypatch):
    async def fake_probe(_video):
        return SimpleNamespace(has_audio=True, duration=1.0)

    async def fake_pieces(*_args, **_kwargs):
        return [(0.0, tmp_path / "speech.wav")]

    monkeypatch.setattr("app.core.speech.probe_video", fake_probe)
    monkeypatch.setattr("app.core.speech._pieces", fake_pieces)
    hit = RawHit(
        start_sec=0.1,
        end_sec=0.4,
        category="价格",
        text="九十九元",
        position="口播内容",
        confidence="确定",
        reason="词表命中",
        source="口播",
    )
    seen: list[SpeechProgress] = []

    class Asr:
        async def transcribe(self, _pieces, *, cancel, on_status, resume=None):
            return [SpeechSentence("九十九元", 0.1, 0.4)]

    class Judge:
        async def judge_speech(self, _sentences):
            raise SpeechJudgeError("模型请求超时，已按词表保留 1 条", [hit])

    async def on_progress(progress):
        seen.append(progress)

    hits = await run_speech(tmp_path / "v.mp4", tmp_path / "work", Asr(), Judge(), asyncio.Event(), on_progress)
    assert hits == [hit]
    assert seen[-1].judge == "partial"
    assert seen[-1].stage == "done"


async def test_frame_retry_does_not_rerun_failed_speech(tmp_path, monkeypatch):
    calls = {"frames": 0, "speech": 0, "pipeline": 0}

    async def fake_selected(*_args, **_kwargs):
        calls["frames"] += 1
        return [], [], ""

    async def fake_pipeline(*_args, **_kwargs):
        calls["pipeline"] += 1
        raise AssertionError("画面重试跑了整段流水线")

    async def fake_speech(*_args, **_kwargs):
        calls["speech"] += 1
        raise AssertionError("画面重试又去跑了口播")

    monkeypatch.setattr("app.services.manager.run_selected_frames", fake_selected)
    monkeypatch.setattr("app.services.manager.run_pipeline", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_speech", fake_speech)
    manager = JobManager()
    await manager.init()
    try:
        video = tmp_path / "a.mp4"
        video.write_bytes(b"video")
        state = _job(
            "a" * 32,
            source_path=str(video),
            status="done",
            stage="done",
            speech_stage="failed",
            speech_judge="failed",
            speech_message="模型请求超时",
            asr_error="模型请求超时",
            speech_transcript=_sentence(),
            failed_frames=1,
            failed_indexes=[2],
            findings=[_picture(), _spoken()],
            hit_count=2,
            error=None,
            options={"asr_enabled": True, "mock": True, "fps": 1, "concurrency": 1},
        )
        manager._jobs[state.id] = state
        await manager.db.upsert_job(state.to_row())
        await manager.db.replace_findings(state.id, state.findings)
        manager._queue_paused = True
        manager._resume.clear()
        queued = await manager.retry_failed_frames(state.id)
        assert queued.retry_part == "frames"
        row = await manager.db.get_job(state.id)
        assert row["retry_part"] == "frames"
        manager._queue_paused = False
        manager._resume.set()
        await manager.run(state.id)
        assert state.status == "done"
        assert state.failed_frames == 0
        assert state.speech_stage == "failed"
        assert state.speech_judge == "failed"
        assert state.speech_transcript[0]["text"] == "这款只要九十九元"
        assert state.to_public().speech_retry == "judge"
        texts = {(item.source, item.text) for item in state.findings}
        assert ("画面", "¥199") in texts
        assert ("口播", "旧口播") in texts
        assert calls == {"frames": 1, "speech": 0, "pipeline": 0}
    finally:
        await manager.shutdown()


async def test_judge_retry_keeps_the_picture_and_skips_tencent(tmp_path, monkeypatch):
    calls = {"speech": 0, "pipeline": 0}

    async def fake_pipeline(*_args, **_kwargs):
        calls["pipeline"] += 1
        raise AssertionError("文字判定重试跑了画面")

    async def fake_speech(*_args, **_kwargs):
        calls["speech"] += 1
        raise AssertionError("文字判定重试又去转写")

    monkeypatch.setattr("app.services.manager.run_pipeline", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_selected_frames", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_speech", fake_speech)
    manager = JobManager()
    await manager.init()
    try:
        state = _job(
            "b" * 32,
            status="done",
            stage="done",
            speech_stage="done",
            speech_judge="partial",
            speech_message="模型请求超时，已按词表保留 1 条",
            speech_transcript=_sentence(),
            failed_frames=0,
            error=None,
            findings=[_picture(), _spoken()],
            hit_count=2,
            options={"asr_enabled": True, "mock": True, "fps": 1, "concurrency": 1, "rules": {}},
        )
        manager._jobs[state.id] = state
        await manager.db.upsert_job(state.to_row())
        await manager.db.replace_findings(state.id, state.findings)
        manager._queue_paused = True
        manager._resume.clear()
        with pytest.raises(BusyError, match="转写已经完成"):
            await manager.retry_speech(state.id, "transcribe")
        queued = await manager.retry_speech(state.id, "judge")
        assert queued.retry_part == "judge"
        assert queued.status == "queued"
        manager._queue_paused = False
        manager._resume.set()
        await manager.run(state.id)
        assert calls == {"speech": 0, "pipeline": 0}
        assert state.status == "done"
        assert state.speech_judge == "ok"
        assert state.retry_part == ""
        assert state.to_public().speech_retry == ""
        texts = {(item.source, item.text) for item in state.findings}
        assert ("画面", "¥199") in texts
        assert ("口播", "旧口播") not in texts
        assert any(source == "口播" and "九十九元" in text for source, text in texts)
        saved = _state_from_row(await manager.db.get_job(state.id), [])
        assert saved.speech_transcript[0]["words"][0]["text"] == "九十九元"
    finally:
        await manager.shutdown()


async def test_transcribe_retry_does_not_touch_frames(tmp_path, monkeypatch):
    calls = {"speech": 0, "pipeline": 0}

    async def fake_pipeline(*_args, **_kwargs):
        calls["pipeline"] += 1
        raise AssertionError("转写重试跑了画面")

    async def fake_speech(*args, **kwargs):
        calls["speech"] += 1
        on_transcript = kwargs.get("on_transcript")
        on_progress = kwargs.get("on_progress", args[5] if len(args) > 5 else None)
        sentence = SpeechSentence("这款只要九十九元", 0.2, 0.8, [SpeechWord("九十九元", 0.2, 0.8)])
        if on_transcript is not None:
            await on_transcript([sentence])
        hit = RawHit(
            start_sec=0.2,
            end_sec=0.8,
            category="价格",
            text="九十九元",
            position="口播内容",
            confidence="确定",
            reason="口播里连续说了这几个字",
            source="口播",
        )
        await on_progress(SpeechProgress(stage="done", message="口播判定完成，命中 1 条", hits=[hit], ratio=1, judge="ok"))
        return [hit]

    monkeypatch.setattr("app.services.manager.run_pipeline", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_selected_frames", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_speech", fake_speech)
    manager = JobManager()
    await manager.init()
    try:
        video = tmp_path / "a.mp4"
        video.write_bytes(b"video")
        state = _job(
            "c" * 32,
            source_path=str(video),
            status="done",
            stage="done",
            speech_stage="failed",
            speech_message="腾讯云识别失败",
            asr_error="腾讯云识别失败",
            failed_frames=0,
            error=None,
            findings=[_picture()],
            hit_count=1,
            options={"asr_enabled": True, "mock": True, "fps": 1, "concurrency": 1},
        )
        manager._jobs[state.id] = state
        await manager.db.upsert_job(state.to_row())
        await manager.db.replace_findings(state.id, state.findings)
        manager._queue_paused = True
        manager._resume.clear()
        with pytest.raises(BusyError, match="还没有口播文本"):
            await manager.retry_speech(state.id, "judge")
        missing = _job(
            "d" * 32,
            source_path=str(tmp_path / "gone.mp4"),
            status="failed",
            speech_stage="failed",
            speech_message="腾讯云识别失败",
            asr_error="腾讯云识别失败",
            options={"asr_enabled": True},
        )
        manager._jobs[missing.id] = missing
        with pytest.raises(FileNotFoundError):
            await manager.retry_speech(missing.id, "transcribe")
        queued = await manager.retry_speech(state.id, "transcribe")
        assert queued.retry_part == "transcribe"
        assert queued.message == "排队重试口播转写"
        manager._queue_paused = False
        manager._resume.set()
        await manager.run(state.id)
        assert calls == {"speech": 1, "pipeline": 0}
        assert state.status == "done"
        assert state.speech_judge == "ok"
        assert state.frames_recognized == 2
        texts = {(item.source, item.text) for item in state.findings}
        assert ("画面", "¥199") in texts
        assert ("口播", "九十九元") in texts
        assert state.to_public().speech_retry == ""
    finally:
        await manager.shutdown()


async def test_queued_retry_part_survives_restart(tmp_path, monkeypatch):
    async def fake_pipeline(*_args, **_kwargs):
        raise AssertionError("重启后的文字判定重试跑了画面")

    async def fake_speech(*_args, **_kwargs):
        raise AssertionError("重启后的文字判定重试又去转写")

    monkeypatch.setattr("app.services.manager.run_pipeline", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_selected_frames", fake_pipeline)
    monkeypatch.setattr("app.services.manager.run_speech", fake_speech)
    manager = JobManager()
    await manager.init()
    paused = _job("a" * 32, status="paused", stage="recognizing", failed_frames=1)
    queued = _job(
        "b" * 32,
        status="queued",
        stage="queued",
        message="排队重试文字判定",
        error=None,
        failed_frames=0,
        retry_part="judge",
        speech_stage="judging",
        speech_transcript=_sentence(),
        findings=[_picture()],
        hit_count=1,
        options={"asr_enabled": True, "mock": True, "fps": 1, "concurrency": 1, "rules": {}},
    )
    await manager.db.upsert_job(paused.to_row())
    await manager.db.upsert_job(queued.to_row())
    await manager.db.replace_findings(queued.id, queued.findings)
    await manager.shutdown()

    again = JobManager()
    await again.init()
    try:
        assert again.queue_paused is True
        restored = again._jobs[queued.id]
        assert restored.status == "queued"
        assert restored.retry_part == "judge"
        assert restored.speech_transcript[0]["text"] == "这款只要九十九元"
        again._queue_paused = False
        again._resume.set()
        await again.run(restored.id)
        assert restored.status == "done"
        assert restored.speech_judge == "ok"
        assert any(item.source == "画面" and item.text == "¥199" for item in restored.findings)
        assert any(item.source == "口播" and "九十九元" in item.text for item in restored.findings)
    finally:
        await again.shutdown()


def test_retry_speech_route_missing_job_is_404():
    with TestClient(create_app()) as client:
        manager = get_manager()
        manager._settings["relay_email"] = "user@example.com"
        manager._settings["relay_access_token"] = "access-token-for-tests"
        missing = client.post(f"/api/jobs/{'ab' * 16}/retry-speech", json={"part": "judge"})
        assert missing.status_code == 404
        assert missing.json()["detail"] == "任务不存在"
        rejected = client.post(f"/api/jobs/{'cd' * 16}/retry-speech", json={"part": "frames"})
        assert rejected.status_code == 422
