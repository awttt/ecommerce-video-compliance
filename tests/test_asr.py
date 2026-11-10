import asyncio
import json
import sqlite3

import httpx
import pytest

from app.core.aggregate import aggregate
from app.core.parsing import parse_speech_result
from app.core.tencent_asr import MockAsrClient, TencentAsrClient, chunk_spans, parse_asr_data
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
        assert "腾讯云密钥" in error
        assert seen and "腾讯云密钥" in seen[0]
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


def test_run_config_accepts_asr_fields():
    config = RunConfig(asr_enabled=False, asr_engine="16k_zh", tencent_region="ap-shanghai")
    assert config.asr_enabled is False
    with pytest.raises(Exception):
        from app.schemas import SettingUpdate

        SettingUpdate(tencent_region="not a region")
