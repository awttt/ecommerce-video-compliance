import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.deepseek import DeepSeekRecognizer, build_recognizer
from app.core.flash import FLASH_INDEX_BASE
from app.core.model_route import (
    LUNA_MODEL,
    REACHABLE_HTTP_GLM_LUNA,
    REACHABLE_HTTP_QWEN,
    ModelRoute,
    clamp_luna_percent,
    initial_route_fields,
    max_http_per_logical_dispatch,
    uses_luna,
)
from app.core.relay import DEEPSEEK_FLASH_MODEL, PICTURE_MODEL, QWEN_FLASH_MODEL
from app.core.transcript import SpeechSentence
from app.db import Database
from app.factory import create_app
from app.schemas import DEFAULT_SETTINGS, Frame, RunConfig
from app.services.manager import _clear_speech_assignments, get_manager

VALID = (
    '{"items":[{"类别":"价格","原文":"¥9.9","位置":"左下角","置信度":"确定",'
    '"判定理由":"画面写着价格","备注":""}]}'
)


def _response(content: str, status: int = 200) -> httpx.Response:
    if status != 200:
        return httpx.Response(status, json={"error": {"message": content}})
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def no_sleep(monkeypatch):
    async def _sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.core.deepseek.asyncio.sleep", _sleep)


def test_percent_is_request_ordinal_not_a_global_half():
    assert clamp_luna_percent(None) == 0
    assert clamp_luna_percent(True) == 0
    assert clamp_luna_percent("50") == 50
    assert clamp_luna_percent(150) == 100
    assert max_http_per_logical_dispatch() == 12
    assert [uses_luna(index, 0) for index in range(1, 5)] == [False, False, False, False]
    assert [uses_luna(index, 100) for index in range(1, 4)] == [True, True, True]
    assert [uses_luna(index, 50) for index in range(1, 5)] == [False, True, False, True]
    assert sum(uses_luna(index, 1) for index in range(1, 101)) == 1
    # 两个任务各自从第 1 帧开始，50% 时都是 GLM，不会跨任务凑成对半。
    assert uses_luna(1, 50) is False
    assert uses_luna(1, 50) is False


async def test_picture_and_speech_keep_separate_ratios_and_retries_do_not_switch():
    options = initial_route_fields(50)
    route = ModelRoute.bind(options, selected_model=PICTURE_MODEL)
    assert [await route.begin_picture(index) for index in (1, 2, 3, 4)] == [
        PICTURE_MODEL,
        LUNA_MODEL,
        PICTURE_MODEL,
        LUNA_MODEL,
    ]
    assert await route.begin_speech(0) == PICTURE_MODEL
    assert await route.begin_speech(1) == LUNA_MODEL
    assert route.stats[PICTURE_MODEL]["logical"] == 3
    assert route.stats[LUNA_MODEL]["logical"] == 3
    assert await route.begin_picture(2) == LUNA_MODEL
    assert route.stats[LUNA_MODEL]["logical"] == 3

    qwen = ModelRoute(selected_model=QWEN_FLASH_MODEL, percent=50)
    assert await qwen.begin_picture(2) == QWEN_FLASH_MODEL
    deepseek = ModelRoute(selected_model=DEEPSEEK_FLASH_MODEL, percent=100)
    assert await deepseek.begin_speech(0) == DEEPSEEK_FLASH_MODEL
    assert LUNA_MODEL not in qwen.stats
    assert LUNA_MODEL not in deepseek.stats


async def test_flash_assignment_is_flushed_before_return_and_survives_reload():
    options = initial_route_fields(100)
    flushed: list[str] = []
    first = FLASH_INDEX_BASE + 1000
    second = FLASH_INDEX_BASE + 2000

    async def flush() -> None:
        flushed.append(options["model_assignments"]["picture"][str(first)])

    route = ModelRoute.bind(options, selected_model=PICTURE_MODEL, allow_new_flash=True, flush=flush)
    assert await route.begin_picture(first) == LUNA_MODEL
    assert flushed == [LUNA_MODEL]
    assert await route.begin_picture(second) == LUNA_MODEL
    picture_flushes = len(flushed)
    assert await route.begin_picture(1) == LUNA_MODEL
    assert len(flushed) == picture_flushes
    assert await route.begin_speech(0) == LUNA_MODEL
    assert len(flushed) == picture_flushes + 1

    restored = json.loads(json.dumps(options))
    again = ModelRoute.bind(restored, selected_model=PICTURE_MODEL, allow_new_flash=False)
    assert await again.begin_picture(first) == LUNA_MODEL
    assert await again.begin_picture(second) == LUNA_MODEL
    missing = FLASH_INDEX_BASE + 3000
    assert await again.begin_picture(missing) == PICTURE_MODEL
    assert str(missing) not in again.assignments["flash_order"]
    assert again.stats[LUNA_MODEL]["logical"] == 4


async def test_rejudge_keeps_speech_model_and_retranscribe_counts_new_batches():
    options = initial_route_fields(50)
    route = ModelRoute.bind(options, selected_model=PICTURE_MODEL)
    assert await route.begin_speech(0) == PICTURE_MODEL
    assert await route.begin_speech(1) == LUNA_MODEL
    await route.finish(LUNA_MODEL, None)
    assert route.stats[LUNA_MODEL]["runs"] == 1
    assert await route.begin_speech(1) == LUNA_MODEL
    assert route.stats[LUNA_MODEL]["logical"] == 1
    _clear_speech_assignments(options)
    assert await route.begin_speech(0) == PICTURE_MODEL
    assert route.stats[PICTURE_MODEL]["logical"] == 2


async def test_stats_are_locked_and_timeout_is_a_failed_subset():
    route = ModelRoute(selected_model=PICTURE_MODEL, percent=50)

    async def one(index: int) -> None:
        model = await route.begin_picture(index)
        await route.note_http(model)
        if index == 2:
            await route.finish(model, RuntimeError("模型请求超时"))
        else:
            await route.finish(model, None)

    import asyncio

    await asyncio.gather(*(one(index) for index in range(1, 21)))
    assert route.stats[PICTURE_MODEL]["logical"] == 10
    assert route.stats[LUNA_MODEL]["logical"] == 10
    assert route.stats[PICTURE_MODEL]["ok"] == 10
    assert route.stats[LUNA_MODEL]["failed"] == 1
    assert route.stats[LUNA_MODEL]["timeout"] == 1
    assert route.stats[LUNA_MODEL]["timeout"] <= route.stats[LUNA_MODEL]["failed"]
    assert route.stats[PICTURE_MODEL]["http_attempts"] == 10
    assert route.stats[LUNA_MODEL]["runs"] == 10


async def test_saved_settings_keep_model_timeout_and_default_percent_to_zero(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.connect()
    legacy = dict(DEFAULT_SETTINGS)
    legacy.pop("luna_request_percent")
    legacy["rules_initialized"] = True
    legacy["exaggeration_initialized"] = True
    legacy["model"] = PICTURE_MODEL
    legacy["timeout_sec"] = 120.0
    legacy["base_url"] = "https://example.invalid"
    legacy["tencent_region"] = "ap-shanghai"
    await db.save_settings(legacy)
    loaded = await db.load_settings()
    assert loaded["luna_request_percent"] == 0
    assert loaded["model"] == PICTURE_MODEL
    assert loaded["timeout_sec"] == 120.0
    assert loaded["base_url"] == "https://example.invalid"
    cursor = await db.conn.execute("SELECT value FROM settings WHERE key='config'")
    raw = json.loads((await cursor.fetchone())["value"])
    assert "luna_request_percent" not in raw
    assert raw["model"] == PICTURE_MODEL
    assert raw["timeout_sec"] == 120.0
    await db.close()


def test_settings_api_keeps_percent_without_changing_model_or_timeout():
    with TestClient(create_app()) as client:
        current = client.get("/api/settings").json()
        assert current["model"] == PICTURE_MODEL
        assert current["timeout_sec"] == 120
        assert current["luna_request_percent"] == 0
        assert "sk-" not in client.get("/api/settings").text
        saved = client.put("/api/settings", json={"luna_request_percent": 50}).json()
        assert saved["luna_request_percent"] == 50
        assert saved["model"] == PICTURE_MODEL
        assert saved["timeout_sec"] == 120
        assert client.put("/api/settings", json={"luna_request_percent": 101}).status_code == 422
        assert client.get("/api/settings").json()["luna_request_percent"] == 50
        qwen = client.put("/api/settings", json={"model": QWEN_FLASH_MODEL}).json()
        assert qwen["model"] == QWEN_FLASH_MODEL
        assert qwen["luna_request_percent"] == 50
        collapsed = client.put("/api/settings", json={"model": LUNA_MODEL}).json()
        assert collapsed["model"] == PICTURE_MODEL
        assert collapsed["luna_request_percent"] == 50
        assert collapsed["timeout_sec"] == 120


async def test_job_snapshot_does_not_follow_later_settings(tmp_path):
    manager = get_manager()
    await manager.init()
    try:
        manager._settings["relay_email"] = "user@example.com"
        manager._settings["relay_access_token"] = "access-token-for-tests"
        manager._settings["model"] = PICTURE_MODEL
        manager._settings["timeout_sec"] = 120
        manager._settings["luna_request_percent"] = 50
        await manager.pause_queue()
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-a-video")
        state = await manager.create_job(video, "clip.mp4")
        manager._settings["luna_request_percent"] = 100
        manager._settings["model"] = QWEN_FLASH_MODEL
        manager._settings["timeout_sec"] = 30
        assert state.options["luna_request_percent"] == 50
        assert state.options["model"] == PICTURE_MODEL
        assert state.options["timeout_sec"] == 120
        row = await manager.db.get_job(state.id)
        stored = json.loads(row["options_json"])
        assert stored["luna_request_percent"] == 50
        assert stored["timeout_sec"] == 120
        assert state.to_public().luna_request_percent == 50
        assert state.to_public().model_stats == {}
    finally:
        await manager.shutdown()


async def _run_frames(tmp_path, *, model: str, percent: int, handler, thinking: bool = False):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8frame")
    calls: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"].startswith("Bearer ")
        assert "thinking" not in request.headers["authorization"]
        return handler(calls)

    options = initial_route_fields(percent)
    route = ModelRoute.bind(options, selected_model=model)
    recognizer = DeepSeekRecognizer(
        RunConfig(
            api_key="sk-test",
            model=model,
            thinking=thinking,
            luna_request_percent=percent,
            timeout_sec=120,
            base_url="https://token4u.cc/v1",
        ),
        transport=httpx.MockTransport(transport),
        route=route,
    )
    return recognizer, route, calls


async def test_luna_body_has_no_glm_controls_and_timeout_stays_120(tmp_path, no_sleep):
    seen: dict = {}

    def handler(calls: list[dict]) -> httpx.Response:
        body = calls[-1]
        assert body["model"] == LUNA_MODEL
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        assert "temperature" not in body
        assert body["max_tokens"] == 4096
        assert body["response_format"] == {"type": "json_object"}
        return _response(VALID)

    recognizer, route, calls = await _run_frames(
        tmp_path, model=PICTURE_MODEL, percent=100, handler=handler, thinking=True
    )
    original = recognizer._complete

    async def spy(client, body, *, timeout=None, attempts=2, budget=None):
        seen["timeout"] = timeout
        seen["attempts"] = attempts
        return await original(client, body, timeout=timeout, attempts=attempts, budget=budget)

    recognizer._complete = spy
    async with recognizer:
        hits = await recognizer.recognize(Frame(index=1, start_sec=0, end_sec=1, path=str(tmp_path / "frame.jpg")))
    assert [item.text for item in hits] == ["¥9.9"]
    assert seen["attempts"] == 2
    assert seen["timeout"].read == 120
    assert route.stats[LUNA_MODEL]["http_attempts"] == 1
    assert route.stats[LUNA_MODEL]["logical"] == 1
    assert route.stats[LUNA_MODEL]["ok"] == 1
    assert calls[0]["model"] == LUNA_MODEL


async def test_other_models_ignore_the_percent(tmp_path, no_sleep):
    def handler(calls: list[dict]) -> httpx.Response:
        body = calls[-1]
        assert body["model"] == QWEN_FLASH_MODEL
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "low"
        return _response('{"items":[]}')

    recognizer, route, calls = await _run_frames(
        tmp_path, model=QWEN_FLASH_MODEL, percent=50, handler=handler
    )
    async with recognizer:
        await recognizer.recognize(Frame(index=2, start_sec=1, end_sec=2, path=str(tmp_path / "frame.jpg")))
    assert calls[0]["model"] == QWEN_FLASH_MODEL
    assert route.stats[QWEN_FLASH_MODEL]["logical"] == 1
    assert LUNA_MODEL not in route.stats


async def test_retryable_failure_stays_on_the_same_model(tmp_path, no_sleep):
    def handler(calls: list[dict]) -> httpx.Response:
        return _response("upstream unavailable", status=503)

    recognizer, route, calls = await _run_frames(
        tmp_path, model=PICTURE_MODEL, percent=50, handler=handler
    )
    async with recognizer:
        with pytest.raises(RuntimeError):
            await recognizer.recognize(Frame(index=2, start_sec=1, end_sec=2, path=str(tmp_path / "frame.jpg")))
    assert len(calls) == 2
    assert [item["model"] for item in calls] == [LUNA_MODEL, LUNA_MODEL]
    assert route.stats[LUNA_MODEL]["http_attempts"] == 2
    assert route.stats[LUNA_MODEL]["failed"] == 1
    assert route.stats[LUNA_MODEL]["timeout"] == 0
    assert route.stats[PICTURE_MODEL]["http_attempts"] == 0
    async with recognizer:
        with pytest.raises(RuntimeError):
            await recognizer.recognize(Frame(index=2, start_sec=1, end_sec=2, path=str(tmp_path / "frame.jpg")))
    assert route.stats[LUNA_MODEL]["logical"] == 1
    assert route.stats[LUNA_MODEL]["runs"] == 2
    assert [item["model"] for item in calls] == [LUNA_MODEL] * 4


async def test_timeout_then_success_is_not_counted_as_timeout(tmp_path, no_sleep):
    def handler(calls: list[dict]) -> httpx.Response:
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return _response(VALID)

    recognizer, route, calls = await _run_frames(
        tmp_path, model=PICTURE_MODEL, percent=0, handler=handler
    )
    async with recognizer:
        await recognizer.recognize(Frame(index=1, start_sec=0, end_sec=1, path=str(tmp_path / "frame.jpg")))
    assert len(calls) == 2
    assert route.stats[PICTURE_MODEL]["http_attempts"] == 2
    assert route.stats[PICTURE_MODEL]["ok"] == 1
    assert route.stats[PICTURE_MODEL]["failed"] == 0
    assert route.stats[PICTURE_MODEL]["timeout"] == 0


async def test_combined_luna_faults_stop_at_the_reachable_cap(tmp_path, no_sleep):
    def handler(calls: list[dict]) -> httpx.Response:
        body = calls[-1]
        assert body["model"] == LUNA_MODEL
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        step = len(calls)
        if step == 1:
            return _response("response_format is unsupported", status=400)
        if step == 2:
            raise httpx.ReadTimeout("timed out")
        if step == 3:
            return _response("不是 JSON")
        if step in {4, 5}:
            raise httpx.ReadTimeout("timed out")
        raise AssertionError("超过 GLM/Luna 可达上限")

    recognizer, route, calls = await _run_frames(
        tmp_path, model=PICTURE_MODEL, percent=50, handler=handler
    )
    async with recognizer:
        with pytest.raises(RuntimeError, match="模型请求超时"):
            await recognizer.recognize(Frame(index=2, start_sec=1, end_sec=2, path=str(tmp_path / "frame.jpg")))
    assert len(calls) == REACHABLE_HTTP_GLM_LUNA
    assert len(calls) < max_http_per_logical_dispatch()
    assert "response_format" not in calls[-1]
    assert route.stats[LUNA_MODEL]["http_attempts"] == REACHABLE_HTTP_GLM_LUNA
    assert route.stats[LUNA_MODEL]["failed"] == 1
    assert route.stats[LUNA_MODEL]["timeout"] == 1
    assert route.stats[PICTURE_MODEL]["http_attempts"] == 0


async def test_combined_qwen_faults_stop_at_the_reachable_cap(tmp_path, no_sleep):
    def handler(calls: list[dict]) -> httpx.Response:
        step = len(calls)
        if step == 1:
            return _response("unknown parameter: thinking", status=400)
        if step == 2:
            return _response("upstream unavailable", status=503)
        if step == 3:
            return _response("不是 JSON")
        if step == 4:
            return _response("response_format is unsupported", status=400)
        if step == 5:
            return _response("unknown parameter: thinking", status=400)
        if step in {6, 7}:
            return _response("upstream unavailable", status=503)
        raise AssertionError("超过 Qwen 可达上限")

    recognizer, route, calls = await _run_frames(
        tmp_path, model=QWEN_FLASH_MODEL, percent=100, handler=handler, thinking=True
    )
    async with recognizer:
        with pytest.raises(RuntimeError):
            await recognizer.recognize(Frame(index=4, start_sec=3, end_sec=4, path=str(tmp_path / "frame.jpg")))
    assert len(calls) == REACHABLE_HTTP_QWEN
    assert len(calls) <= max_http_per_logical_dispatch()
    assert [item["model"] for item in calls] == [QWEN_FLASH_MODEL] * REACHABLE_HTTP_QWEN
    assert route.stats[QWEN_FLASH_MODEL]["http_attempts"] == REACHABLE_HTTP_QWEN
    assert route.stats[QWEN_FLASH_MODEL]["failed"] == 1
    assert route.stats[QWEN_FLASH_MODEL]["timeout"] == 0


async def test_non_retryable_status_does_not_spend_the_rest_of_the_budget(tmp_path, no_sleep):
    def handler(calls: list[dict]) -> httpx.Response:
        return _response("denied", status=401)

    recognizer, _route, calls = await _run_frames(
        tmp_path, model=PICTURE_MODEL, percent=100, handler=handler
    )
    async with recognizer:
        with pytest.raises(RuntimeError, match="API Key"):
            await recognizer.recognize(Frame(index=3, start_sec=2, end_sec=3, path=str(tmp_path / "frame.jpg")))
    assert len(calls) == 1
    assert calls[0]["model"] == LUNA_MODEL


async def test_mock_mode_does_not_increase_http_attempts():
    options = initial_route_fields(50)
    route = ModelRoute.bind(options, selected_model=PICTURE_MODEL)
    recognizer = build_recognizer(RunConfig(mock=True, luna_request_percent=50, timeout_sec=120), route)
    async with recognizer:
        await recognizer.recognize(Frame(index=1, start_sec=0, end_sec=1, path="missing.jpg"))
        await recognizer.judge_speech([SpeechSentence("九十九元", 0, 1)])
    assert route.stats[PICTURE_MODEL]["http_attempts"] == 0
    assert route.stats[LUNA_MODEL]["http_attempts"] == 0
    assert route.stats[PICTURE_MODEL]["logical"] == 0
    assert route.stats[LUNA_MODEL]["logical"] == 0


async def test_speech_batches_use_their_own_ratio_and_same_model_on_repair(tmp_path, no_sleep):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return _response("不是 JSON")
        return _response('{"items":[]}')

    options = initial_route_fields(50)
    route = ModelRoute.bind(options, selected_model=PICTURE_MODEL)
    sentences = [SpeechSentence(f"第{index}句没有价格", index, index + 0.4) for index in range(9)]
    recognizer = DeepSeekRecognizer(
        RunConfig(api_key="sk-test", timeout_sec=120, base_url="https://token4u.cc/v1", luna_request_percent=50),
        transport=httpx.MockTransport(handler),
        route=route,
    )
    async with recognizer:
        await recognizer.judge_speech(sentences)
    assert [item["model"] for item in calls] == [PICTURE_MODEL, PICTURE_MODEL, LUNA_MODEL]
    assert route.stats[PICTURE_MODEL]["logical"] == 1
    assert route.stats[LUNA_MODEL]["logical"] == 1
    assert route.stats[PICTURE_MODEL]["http_attempts"] == 2
    assert route.stats[LUNA_MODEL]["http_attempts"] == 1
