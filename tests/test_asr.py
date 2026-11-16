import asyncio
import json
import sqlite3

import httpx
import pytest

from app.core.aggregate import aggregate
from app.core.parsing import parse_speech_result
from app.core.tencent_asr import MockAsrClient, TencentAsrClient, chunk_spans, parse_asr_data, sign_tc3
from app.core.transcript import SpeechSentence, SpeechWord
from app.db import Database
from app.schemas import RunConfig
from app.services.manager import JobState


def _sentence() -> SpeechSentence:
    return SpeechSentence(
        text="这款只要九十九元",
        start_sec=12,
        end_sec=15,
        words=[
            SpeechWord("这款只要", 12, 13.2),
            SpeechWord("九十九元", 13.2, 15),
        ],
    )


def test_speech_quote_must_occur_in_the_sentence_and_keeps_word_time():
    content = json.dumps(
        {
            "items": [
                {
                    "句子": 1,
                    "类别": "价格",
                    "原文": "九十九元",
                    "置信度": "确定",
                    "判定理由": "口播中明确提及价格",
                    "备注": "",
                },
                {
                    "句子": 1,
                    "类别": "价格",
                    "原文": "一百九十九元",
                    "置信度": "确定",
                    "判定理由": "口播中明确提及价格",
                },
                {
                    "句子": 1,
                    "类别": "品牌",
                    "原文": "九十九元",
                    "置信度": "确定",
                    "判定理由": "根据常识推测是某品牌",
                },
            ]
        },
        ensure_ascii=False,
    )
    hits = parse_speech_result(content, [_sentence()])
    assert len(hits) == 1
    assert hits[0].text == "九十九元"
    assert hits[0].source == "口播"
    assert hits[0].position == "口播内容"
    assert hits[0].start_sec == 13.2
    assert hits[0].end_sec == 15
    assert hits[0].sentence_start == 12
    assert hits[0].sentence_end == 15
    assert hits[0].next_start is None
    finding = aggregate(hits)[0]
    assert finding.export_dict()["来源"] == "口播"
    assert finding.time_range == "00:13-00:15"


def test_full_sentence_quote_matches_requested_shape():
    content = json.dumps(
        {
            "items": [
                {
                    "句子": 1,
                    "类别": "价格",
                    "原文": "这款只要九十九元",
                    "置信度": "确定",
                    "判定理由": "口播中明确提及价格",
                    "备注": "",
                }
            ]
        },
        ensure_ascii=False,
    )
    finding = aggregate(parse_speech_result(content, [_sentence()]))[0]
    assert finding.export_dict() == {
        "时间点": "00:12-00:15",
        "类别": "价格",
        "来源": "口播",
        "原文": "这款只要九十九元",
        "位置": "口播内容",
        "置信度": "确定",
        "判定理由": "口播中明确提及价格",
        "备注": "",
    }
    assert finding.sentence_start == 12
    assert finding.sentence_end == 15


def test_speech_hit_records_the_following_sentence():
    following = SpeechSentence("下一句没有价格", 15.2, 16.5)
    content = json.dumps(
        {
            "items": [
                {
                    "句子": 1,
                    "类别": "价格",
                    "原文": "这款只要九十九元",
                    "置信度": "确定",
                    "判定理由": "口播中明确提及价格",
                }
            ]
        },
        ensure_ascii=False,
    )
    finding = aggregate(parse_speech_result(content, [_sentence(), following]))[0]
    assert finding.next_start == 15.2
    assert finding.sentence_end == 15
    assert finding.time_range == "00:12-00:15"


def test_chunk_spans_cover_long_audio_without_overlap():
    spans = chunk_spans(100, 10_000_000, limit=4_500_000)
    assert len(spans) >= 2
    assert spans[0][0] == 0
    assert spans[-1][1] == 100
    for previous, current in zip(spans, spans[1:]):
        assert previous[1] == current[0]


def test_bracket_timestamps_shift_by_chunk_offset():
    data = {"Result": "[0:12.000,0:15.000]  这款只要九十九元\n", "ResultDetail": []}
    sentences = parse_asr_data(data, 60)
    assert sentences[0].text == "这款只要九十九元"
    assert sentences[0].start_sec == 72
    assert sentences[0].end_sec == 75


def test_result_detail_words_are_offset_from_the_chunk():
    data = {
        "ResultDetail": [
            {
                "FinalSentence": "这款只要九十九元",
                "StartMs": 0,
                "EndMs": 1500,
                "Words": [{"Word": "九十九元", "OffsetStartMs": 800, "OffsetEndMs": 1500}],
            }
        ]
    }
    sentences = parse_asr_data(data, 12)
    assert sentences[0].start_sec == 12
    assert sentences[0].end_sec == 13.5
    assert sentences[0].words[0].start_sec == 12.8


async def test_tencent_client_polls_and_signs_without_network(tmp_path):
    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"fake-audio")
    calls = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-TC-Version"] == "2019-06-14"
        assert request.headers["Authorization"].startswith("TC3-HMAC-SHA256 ")
        assert "SignedHeaders=content-type;host" in request.headers["Authorization"]
        body = json.loads(request.content.decode("utf-8"))
        action = request.headers["X-TC-Action"]
        if action == "CreateRecTask":
            assert body["SourceType"] == 1
            assert body["ResTextFormat"] == 2
            assert body["ChannelNum"] == 1
            assert body["ConvertNumMode"] == 0
            assert body["DataLen"] == len(b"fake-audio")
            return httpx.Response(200, json={"Response": {"Data": {"TaskId": 99}, "RequestId": "a"}})
        calls["status"] += 1
        if calls["status"] == 1:
            data = {"TaskId": 99, "Status": 1, "StatusStr": "doing"}
        else:
            data = {
                "TaskId": 99,
                "Status": 2,
                "StatusStr": "success",
                "ResultDetail": [
                    {"FinalSentence": "这款只要九十九元", "StartMs": 0, "EndMs": 3000, "Words": []}
                ],
            }
        return httpx.Response(200, json={"Response": {"Data": data, "RequestId": "b"}})

    client = TencentAsrClient(
        "AKIDexample",
        "secret",
        "ap-shanghai",
        poll_interval=0,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        sentences = await client.transcribe(
            [(12.0, audio)],
            cancel=asyncio.Event(),
            on_status=_accept_status,
        )
    assert calls["status"] == 2
    assert sentences[0].text == "这款只要九十九元"
    assert sentences[0].start_sec == 12
    assert sentences[0].end_sec == 15


async def _accept_status(_message: str) -> None:
    return None


def _state(asr_enabled: bool, mock: bool) -> JobState:
    return JobState(
        id="a" * 32,
        filename="视频.mp4",
        source_path="视频.mp4",
        status="running",
        stage="extracting",
        message="",
        progress=0,
        fps=1,
        concurrency=1,
        duration_sec=None,
        frames_estimated=0,
        frames_extracted=0,
        frames_recognized=0,
        failed_frames=0,
        hit_count=0,
        error=None,
        speech_stage="",
        speech_message="",
        asr_error=None,
        options={
            "asr_enabled": asr_enabled,
            "mock": mock,
            "asr_engine": "16k_zh",
            "tencent_region": "ap-shanghai",
            "fps": 1,
            "concurrency": 1,
        },
        output_path=None,
        created_at="",
        updated_at="",
        finished_at=None,
    )


async def test_missing_tencent_secret_skips_speech_without_failing_the_switch():
    from app.services.manager import JobManager

    manager = JobManager()
    await manager.init()
    try:
        state = _state(True, False)
        config = manager._config_for(state)
        assert config.asr_enabled is True
        assert config.tencent_secret_id == ""
        seen: list[str] = []

        async def on_speech(progress) -> None:
            seen.append(progress.message)

        task = manager._start_speech(state, config, None, {}, on_speech)
        assert task is not None
        hits, error = await task
        assert hits == []
        assert "未登录" in error
        assert seen and "未登录" in seen[0]
        assert manager._start_speech(state, RunConfig(asr_enabled=False, mock=False), None, {}, on_speech) is None
    finally:
        await manager.shutdown()


async def test_mock_asr_returns_the_sample_sentence():
    client = MockAsrClient()
    sentences = await client.transcribe([], cancel=asyncio.Event(), on_status=_accept_status)
    assert sentences[0].text == "这款只要九十九元"
    assert sentences[0].start_sec == 0
    assert sentences[0].end_sec == 0.5


async def test_old_findings_table_gains_source_column(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL,
            time_range TEXT NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            category TEXT NOT NULL,
            text TEXT NOT NULL,
            position TEXT NOT NULL,
            confidence TEXT NOT NULL,
            reason TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    raw.execute(
        """
        INSERT INTO findings (
            job_id, time_range, start_sec, end_sec, category, text, position, confidence, reason, note
        ) VALUES ('abc', '00:01-00:02', 1, 2, '价格', '¥199', '画面中可见', '确定', '画面中可见对应文字', '')
        """
    )
    raw.commit()
    raw.close()

    database = Database(path)
    await database.connect()
    try:
        findings = await database.get_findings("abc")
        assert findings[0].source == "画面"
        assert findings[0].text == "¥199"
    finally:
        await database.close()


def test_sign_tc3_adds_the_session_token_outside_the_signature():
    headers = sign_tc3(
        secret_id="AKIDexample",
        secret_key="secret",
        region="ap-shanghai",
        action="CreateRecTask",
        payload="{}",
        timestamp=1_700_000_000,
        token="session-token",
    )
    assert headers["X-TC-Token"] == "session-token"
    assert "session-token" not in headers["Authorization"]


def test_sign_tc3_omits_the_token_header_for_a_long_term_key():
    headers = sign_tc3(
        secret_id="AKIDexample",
        secret_key="secret",
        region="ap-shanghai",
        action="CreateRecTask",
        payload="{}",
        timestamp=1_700_000_000,
    )
    assert "X-TC-Token" not in headers


async def test_logged_in_speech_fetches_credentials_while_extracting_audio(monkeypatch, tmp_path):
    from app.core.ffmpeg import VideoProbe
    from app.core.relay import RelayClient
    from app.services.manager import JobManager

    order: list[str] = []

    async def fake_probe(_path):
        return VideoProbe(1.0, True, True, 16, 16)

    async def fake_codec():
        return ".wav", ["-c:a", "pcm_s16le"]

    async def fake_extract(_video, dest, **_kwargs):
        order.append("extract-start")
        await asyncio.sleep(0.05)
        dest.write_bytes(b"\x00\x01")
        order.append("extract-end")

    monkeypatch.setattr("app.core.speech.probe_video", fake_probe)
    monkeypatch.setattr("app.core.speech.speech_codec", fake_codec)
    monkeypatch.setattr("app.core.speech.extract_audio", fake_extract)

    def upstream(request: httpx.Request) -> httpx.Response:
        order.append("credentials")
        assert request.url.path == "/api/v1/asr/credentials"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "tmp_secret_id": "AKIDtmp",
                    "tmp_secret_key": "tmp-secret",
                    "token": "session-token",
                    "region": "ap-guangzhou",
                    "expired_at": 1_700_001_800,
                },
            },
        )

    seen_token: list[str] = []

    def tencent(request: httpx.Request) -> httpx.Response:
        seen_token.append(request.headers.get("X-TC-Token", ""))
        if request.headers.get("X-TC-Action") == "CreateRecTask":
            return httpx.Response(200, json={"Response": {"Data": {"TaskId": 7}, "RequestId": "a"}})
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Data": {
                        "TaskId": 7,
                        "Status": 2,
                        "StatusStr": "success",
                        "ResultDetail": [
                            {"FinalSentence": "这款只要九十九元", "StartMs": 0, "EndMs": 400, "Words": []}
                        ],
                    },
                    "RequestId": "b",
                }
            },
        )

    manager = JobManager()
    await manager.init()
    try:
        manager._settings["relay_access_token"] = "login-token"
        manager._settings["relay_email"] = "user@example.com"
        manager.relay = RelayClient(transport=httpx.MockTransport(upstream))
        state = _state(True, False)
        state.source_path = str(tmp_path / "视频.mp4")
        config = manager._config_for(state)
        asr_holder: dict = {}

        async def on_speech(_progress) -> None:
            return None

        class Judge:
            async def judge_speech(self, _sentences):
                return []

        task = manager._start_speech(state, config, Judge(), asr_holder, on_speech)
        assert task is not None
        client = asr_holder["asr"]
        client._transport = httpx.MockTransport(tencent)
        hits, error = await task
        assert error == ""
        assert hits == []
        assert seen_token and seen_token[0] == "session-token"
        assert client.region == "ap-guangzhou"
        assert order.index("credentials") < order.index("extract-end")
    finally:
        await manager.shutdown()


async def test_credential_failure_skips_speech_without_calling_tencent(monkeypatch, tmp_path):
    from app.core.ffmpeg import VideoProbe
    from app.core.relay import RelayClient
    from app.services.manager import JobManager

    async def fake_probe(_path):
        return VideoProbe(1.0, True, True, 16, 16)

    async def fake_codec():
        return ".wav", ["-c:a", "pcm_s16le"]

    async def fake_extract(_video, dest, **_kwargs):
        dest.write_bytes(b"\x00\x01")

    monkeypatch.setattr("app.core.speech.probe_video", fake_probe)
    monkeypatch.setattr("app.core.speech.speech_codec", fake_codec)
    monkeypatch.setattr("app.core.speech.extract_audio", fake_extract)

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": 403, "message": "当前账号没有口播分组，请联系管理员分配"})

    called = {"tencent": False}

    def tencent(_request: httpx.Request) -> httpx.Response:
        called["tencent"] = True
        return httpx.Response(500, json={})

    manager = JobManager()
    await manager.init()
    try:
        manager._settings["relay_access_token"] = "login-token"
        manager._settings["relay_email"] = "user@example.com"
        manager.relay = RelayClient(transport=httpx.MockTransport(upstream))
        state = _state(True, False)
        state.source_path = str(tmp_path / "视频.mp4")
        config = manager._config_for(state)
        holder: dict = {}

        async def on_speech(_progress) -> None:
            return None

        task = manager._start_speech(state, config, None, holder, on_speech)
        assert task is not None
        holder["asr"]._transport = httpx.MockTransport(tencent)
        hits, error = await task
        assert hits == []
        assert "口播分组" in error
        assert called["tencent"] is False
    finally:
        await manager.shutdown()


def test_legacy_engine_becomes_the_large_model():
    from app.core.asr_engine import configured_engine
    from app.schemas import SettingUpdate

    assert configured_engine("16k_zh") == "16k_zh_en_2.0"
    assert configured_engine("") == "16k_zh_en_2.0"
    assert SettingUpdate(asr_engine="16k_zh_en_2.0").asr_engine == "16k_zh_en_2.0"
    assert SettingUpdate(asr_engine="16k_zh").asr_engine == "16k_zh_en_2.0"


def test_run_config_accepts_asr_fields():
    config = RunConfig(asr_enabled=False, asr_engine="16k_zh", tencent_region="ap-shanghai")
    assert config.asr_enabled is False
    with pytest.raises(Exception):
        from app.schemas import SettingUpdate

        SettingUpdate(tencent_region="not a region")
