import asyncio
import json
import random
import sqlite3
import time

import httpx
import pytest

from app.core.aggregate import aggregate
from app.core.parsing import parse_speech_result
from app.core.pipeline import JobCancelled
from app.core.tencent_asr import (
    AsrCredentials,
    MockAsrClient,
    TencentAsrClient,
    chunk_spans,
    parse_asr_data,
    retry_backoff_seconds,
    sign_tc3,
    wait_backoff,
)
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


def test_speech_json_accepts_trailing_commas_and_unquoted_keys():
    from app.core.parsing import extract_json

    loose = (
        '{"items":[{句子:1,"类别":"价格","原文":"九十九元","置信度":"确定","判定理由":"口播中明确提及价格",}]}'
    )
    hits = parse_speech_result(loose, [_sentence()])
    assert [item.text for item in hits] == ["九十九元"]
    with pytest.raises(ValueError, match="可解析"):
        extract_json("Expecting property name")


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
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "AKIDLOCALDEBUG")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "local-debug-secret")

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
                    "expired_at": 4_000_000_000,
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
        assert client.expired_at == 4_000_000_000
        assert order.count("credentials") == 1
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

    relay_calls = {"n": 0}

    def upstream(_request: httpx.Request) -> httpx.Response:
        relay_calls["n"] += 1
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
        assert relay_calls["n"] == 1
    finally:
        await manager.shutdown()


def test_asr_engine_choice_is_kept():
    from app.core.asr_engine import configured_engine
    from app.schemas import SettingUpdate

    assert configured_engine("16k_zh_en_2.0") == "16k_zh_en_2.0"
    assert configured_engine("") == "16k_zh"
    assert configured_engine("16k_zh") == "16k_zh"
    assert SettingUpdate(asr_engine="16k_zh_en_2.0").asr_engine == "16k_zh_en_2.0"
    assert SettingUpdate(asr_engine="16k_zh").asr_engine == "16k_zh"


def test_run_config_accepts_asr_fields():
    config = RunConfig(asr_enabled=False, asr_engine="16k_zh", tencent_region="ap-shanghai")
    assert config.asr_enabled is False
    with pytest.raises(Exception):
        from app.schemas import SettingUpdate

        SettingUpdate(tencent_region="not a region")


def _audio(tmp_path, payload: bytes = b"fake-audio"):
    path = tmp_path / "speech.mp3"
    path.write_bytes(payload)
    return path


def _created() -> httpx.Response:
    return httpx.Response(200, json={"Response": {"Data": {"TaskId": 7}, "RequestId": "a"}})


def _done() -> httpx.Response:
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


def _api_error(code: str, message: str = "no", status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"Response": {"Error": {"Code": code, "Message": message}, "RequestId": "e"}},
    )


def _asr_client(handler, **kwargs) -> TencentAsrClient:
    backoff = kwargs.pop("backoff", lambda _index: 0)
    client = TencentAsrClient(
        kwargs.pop("secret_id", "AKIDexample"),
        kwargs.pop("secret_key", "secret-key-value"),
        kwargs.pop("region", "ap-shanghai"),
        token=kwargs.pop("token", "session-token"),
        poll_interval=kwargs.pop("poll_interval", 0),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    client.backoff_delay = backoff
    return client


async def _transcribe(client: TencentAsrClient, audio, on_status=_accept_status, cancel=None, resume=None):
    async with client:
        return await client.transcribe(
            [(0.0, audio)],
            cancel=cancel or asyncio.Event(),
            on_status=on_status,
            resume=resume,
        )


def test_credential_repr_hides_the_secrets():
    creds = AsrCredentials("AKIDsupersecretvalue", "key-value-secret", "session-token", "ap-shanghai", 10)
    text = f"{creds!r} {creds}"
    assert "AKIDsupersecretvalue" not in text
    assert "key-value-secret" not in text
    assert "session-token" not in text
    assert "ap-shanghai" in text


def test_credential_due_within_about_five_minutes(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr("app.core.tencent_asr.time.time", lambda: now)
    client = TencentAsrClient("AKIDexample", "secret-key-value", "ap-shanghai", token="session-token", expired_at=now + 300)
    assert client._credential_due() is True
    client.expired_at = now + 301
    assert client._credential_due() is False
    client.expired_at = None
    assert client._credential_due() is False
    client.token = ""
    assert client._credential_due() is True


def test_retry_backoff_grows_with_jitter_and_caps_at_thirty_seconds():
    rng = random.Random(1)
    first = [retry_backoff_seconds(1, rng=rng) for _ in range(30)]
    assert all(0.8 <= item <= 1.2 for item in first)
    assert len(set(first)) > 1
    fifth = [retry_backoff_seconds(5, rng=random.Random(index)) for index in range(10)]
    assert all(16 * 0.8 <= item <= 16 * 1.2 for item in fifth)
    capped = retry_backoff_seconds(10, rng=random.Random(3))
    assert 0 < capped <= 30


async def test_wait_backoff_stays_paused_until_resume_and_stops_on_cancel():
    resume = asyncio.Event()
    cancel = asyncio.Event()
    finished = False

    async def paused():
        nonlocal finished
        await wait_backoff(0, cancel, resume)
        finished = True

    task = asyncio.create_task(paused())
    await asyncio.sleep(0.05)
    assert finished is False
    resume.set()
    await asyncio.wait_for(task, timeout=1)
    assert finished is True

    resume.set()
    waiting = asyncio.create_task(wait_backoff(30, cancel, resume))
    await asyncio.sleep(0.05)
    cancel.set()
    with pytest.raises(JobCancelled):
        await asyncio.wait_for(waiting, timeout=1)


async def test_refreshes_credentials_before_each_call_when_they_expire_soon(tmp_path, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr("app.core.tencent_asr.time.time", lambda: now)
    issued = {"n": 0}

    async def provider():
        issued["n"] += 1
        return AsrCredentials(
            f"AKIDfresh{issued['n']}",
            f"secret-{issued['n']}-value",
            f"token-{issued['n']}",
            "ap-guangzhou",
            now + 30,
        )

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["X-TC-Action"], request.headers.get("X-TC-Token", "")))
        if request.headers["X-TC-Action"] == "CreateRecTask":
            return _created()
        return _done()

    client = _asr_client(
        handler,
        token="old-token",
        expired_at=now + 60,
        credential_provider=provider,
    )
    sentences = await _transcribe(client, _audio(tmp_path))
    assert sentences[0].text == "这款只要九十九元"
    assert issued["n"] == 2
    assert seen == [("CreateRecTask", "token-1"), ("DescribeTaskStatus", "token-2")]
    assert client.region == "ap-guangzhou"
    assert "secret-2-value" not in repr(client)


async def test_keeps_credentials_that_are_not_near_expiry(tmp_path, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr("app.core.tencent_asr.time.time", lambda: now)
    issued = {"n": 0}

    async def provider():
        issued["n"] += 1
        return AsrCredentials("AKIDfresh", "fresh-secret-value", "fresh-token", "ap-shanghai", now + 7200)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-TC-Token", ""))
        if request.headers["X-TC-Action"] == "CreateRecTask":
            return _created()
        return _done()

    client = _asr_client(handler, token="old-token", expired_at=now + 3600, credential_provider=provider)
    await _transcribe(client, _audio(tmp_path))
    assert issued["n"] == 0
    assert seen == ["old-token", "old-token"]


@pytest.mark.parametrize(
    "code",
    [
        "AuthFailure.TokenFailure",
        "AuthFailure.SignatureExpire",
        "AuthFailure.SecretIdNotFound",
        "AuthFailure.InvalidSecretId",
    ],
)
async def test_auth_error_refreshes_once_and_retries(tmp_path, code, caplog):
    caplog.set_level("WARNING")
    issued = {"n": 0}
    secret = "fresh-secret-value"

    async def provider():
        issued["n"] += 1
        return AsrCredentials("AKIDfresh99", secret, "fresh-token", "ap-shanghai", time.time() + 7200)

    creates = {"n": 0}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["X-TC-Action"] != "CreateRecTask":
            return _done()
        creates["n"] += 1
        seen.append(request.headers.get("X-TC-Token", ""))
        if creates["n"] == 1:
            return _api_error(code, f"token expired {secret}")
        return _created()

    messages: list[str] = []

    async def on_status(message: str) -> None:
        messages.append(message)

    client = _asr_client(handler, token="old-token", expired_at=time.time() + 7200, credential_provider=provider)
    await _transcribe(client, _audio(tmp_path), on_status=on_status)
    assert seen == ["old-token", "fresh-token"]
    assert issued["n"] == 1
    assert "腾讯云识别重试中（1/5）" in messages
    assert "腾讯云识别重试中（2/5）" not in messages
    assert secret not in caplog.text
    assert "AKIDfresh99" not in caplog.text
    assert secret not in "".join(messages)


async def test_auth_error_without_a_provider_is_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _api_error("AuthFailure.TokenFailure", "token expired")

    client = _asr_client(handler, token="old-token", expired_at=time.time() + 7200)
    with pytest.raises(Exception, match="AuthFailure.TokenFailure"):
        await _transcribe(client, _audio(tmp_path))
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "failure",
    ["limit", "internal", "unavailable", "http503", "http429", "network", "timeout"],
)
async def test_transient_asr_errors_retry_five_times_then_fail(tmp_path, failure, caplog):
    caplog.set_level("WARNING")
    calls = {"n": 0}
    secret = "secret-key-value"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if failure == "network":
            raise httpx.ConnectError("offline")
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out")
        if failure == "http503":
            return httpx.Response(503, json={"Response": {"Error": {"Code": "InternalError", "Message": secret}}})
        if failure == "http429":
            return httpx.Response(429, text="slow down")
        code = {
            "limit": "RequestLimitExceeded",
            "internal": "InternalError",
            "unavailable": "ResourceUnavailable",
        }[failure]
        return _api_error(code, f"busy {secret} AKIDEXAMPLESECRET99")

    messages: list[str] = []

    async def on_status(message: str) -> None:
        messages.append(message)

    client = _asr_client(handler)
    with pytest.raises(Exception, match="腾讯云") as caught:
        await _transcribe(client, _audio(tmp_path), on_status=on_status)
    assert calls["n"] == 6
    assert [item for item in messages if item.startswith("腾讯云识别重试中")] == [
        "腾讯云识别重试中（1/5）",
        "腾讯云识别重试中（2/5）",
        "腾讯云识别重试中（3/5）",
        "腾讯云识别重试中（4/5）",
        "腾讯云识别重试中（5/5）",
    ]
    assert secret not in str(caught.value)
    assert "AKIDEXAMPLESECRET99" not in str(caught.value)
    assert secret not in caplog.text


async def test_transient_retry_stops_after_the_call_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        if request.headers["X-TC-Action"] == "CreateRecTask":
            return _created()
        return _done()

    messages: list[str] = []

    async def on_status(message: str) -> None:
        messages.append(message)

    client = _asr_client(handler)
    sentences = await _transcribe(client, _audio(tmp_path), on_status=on_status)
    assert sentences[0].text == "这款只要九十九元"
    assert calls["n"] == 4
    assert "腾讯云识别重试中（2/5）" in messages
    assert "腾讯云识别重试中（3/5）" not in messages


@pytest.mark.parametrize(
    "code,message",
    [
        ("InvalidParameter", "EngineModelType"),
        ("InvalidParameterValue", "bad field"),
        ("FailedOperation.UserHasNoFreeAmount", "no balance"),
        ("FailedOperation.ServiceIsolate", "unpaid"),
        ("FailedOperation.UserHasNoAmount", "余额不足"),
    ],
)
async def test_permanent_asr_errors_are_not_retried(tmp_path, code, message):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _api_error(code, message)

    messages: list[str] = []

    async def on_status(text: str) -> None:
        messages.append(text)

    client = _asr_client(handler)
    with pytest.raises(Exception, match="腾讯云识别失败"):
        await _transcribe(client, _audio(tmp_path), on_status=on_status)
    assert calls["n"] == 1
    assert not any("重试" in item for item in messages)


async def test_recognition_failure_for_bad_audio_is_not_retried(tmp_path):
    describes = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["X-TC-Action"] == "CreateRecTask":
            return _created()
        describes["n"] += 1
        return httpx.Response(
            200,
            json={"Response": {"Data": {"TaskId": 7, "Status": 3, "StatusStr": "failed", "ErrorMsg": "音频质量太差"}}},
        )

    client = _asr_client(handler)
    with pytest.raises(Exception, match="音频质量太差"):
        await _transcribe(client, _audio(tmp_path))
    assert describes["n"] == 1


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "空"), (5 * 1024 * 1024 + 1, "5MB")],
)
async def test_empty_or_oversized_audio_is_not_sent(tmp_path, size, text):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _created()

    client = _asr_client(handler)
    with pytest.raises(Exception, match=text):
        await _transcribe(client, _audio(tmp_path, b"\x00" * size))
    assert calls["n"] == 0


async def test_cancel_during_asr_retry_stops_without_another_call(tmp_path):
    calls = {"n": 0}
    cancel = asyncio.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    async def on_status(message: str) -> None:
        if "重试" in message:
            cancel.set()

    client = _asr_client(handler, backoff=lambda _index: 30)
    with pytest.raises(JobCancelled):
        await _transcribe(client, _audio(tmp_path), on_status=on_status, cancel=cancel)
    assert calls["n"] == 1


def _credential_payload(token: str = "session-token", expired_at: float = 4_000_000_000) -> dict:
    return {
        "code": 0,
        "data": {
            "tmp_secret_id": "AKIDtmp",
            "tmp_secret_key": "tmp-secret-value",
            "token": token,
            "region": "ap-guangzhou",
            "expired_at": expired_at,
        },
    }


class _QuietJudge:
    async def judge_speech(self, _sentences):
        return []


async def _relay_speech(monkeypatch, tmp_path, upstream, tencent=None):
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
    monkeypatch.setattr("app.services.manager.retry_backoff_seconds", lambda _index, rng=None: 0)

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
        seen: list[str] = []

        async def on_speech(progress) -> None:
            seen.append(progress.message)

        task = manager._start_speech(state, config, _QuietJudge(), holder, on_speech)
        assert task is not None
        if tencent is not None:
            holder["asr"]._transport = httpx.MockTransport(tencent)
            holder["asr"].backoff_delay = lambda _index: 0
        hits, error = await task
        return hits, error, holder, seen
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("status", [401, 403, 400])
async def test_relay_auth_and_client_errors_do_not_retry_credential_fetch(monkeypatch, tmp_path, status):
    calls = {"n": 0}

    def upstream(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if status == 403:
            return httpx.Response(403, json={"code": 403, "message": "当前账号没有口播分组，请联系管理员分配"})
        if status == 401:
            return httpx.Response(401, json={"code": 401, "message": "登录已过期"})
        return httpx.Response(400, json={"message": "bad request"})

    hits, error, _holder, _seen = await _relay_speech(monkeypatch, tmp_path, upstream)
    assert hits == []
    assert error
    assert calls["n"] == 1


async def test_relay_credential_fetch_retries_transient_errors_then_succeeds(monkeypatch, tmp_path):
    calls = {"n": 0}

    def upstream(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json=_credential_payload())

    def tencent(request: httpx.Request) -> httpx.Response:
        if request.headers["X-TC-Action"] == "CreateRecTask":
            return _created()
        return _done()

    hits, error, holder, _seen = await _relay_speech(monkeypatch, tmp_path, upstream, tencent)
    assert error == ""
    assert hits == []
    assert calls["n"] == 3
    assert holder["asr"].token == "session-token"


@pytest.mark.parametrize("failure", ["http503", "http429", "network"])
async def test_relay_credential_fetch_stops_after_five_retries(monkeypatch, tmp_path, failure):
    calls = {"n": 0}
    tencent_calls = {"n": 0}

    def upstream(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if failure == "network":
            raise httpx.ConnectError("offline")
        if failure == "http429":
            return httpx.Response(429, json={"message": "too many"})
        return httpx.Response(503, json={"message": "unavailable"})

    def tencent(_request: httpx.Request) -> httpx.Response:
        tencent_calls["n"] += 1
        return httpx.Response(500, json={})

    hits, error, _holder, _seen = await _relay_speech(monkeypatch, tmp_path, upstream, tencent)
    assert hits == []
    assert error
    assert calls["n"] == 6
    assert tencent_calls["n"] == 0


async def test_logged_in_speech_refreshes_expiring_credentials(monkeypatch, tmp_path):
    now = time.time()
    relay_calls = {"n": 0}
    seen: list[str] = []

    def upstream(_request: httpx.Request) -> httpx.Response:
        relay_calls["n"] += 1
        if relay_calls["n"] == 1:
            return httpx.Response(200, json=_credential_payload("token-old", now + 60))
        return httpx.Response(200, json=_credential_payload("token-new", now + 7200))

    def tencent(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-TC-Token", ""))
        if request.headers["X-TC-Action"] == "CreateRecTask":
            return _created()
        return _done()

    _hits, error, holder, _messages = await _relay_speech(monkeypatch, tmp_path, upstream, tencent)
    assert error == ""
    assert relay_calls["n"] == 2
    assert seen == ["token-new", "token-new"]
    assert holder["asr"].expired_at == now + 7200
    assert "tmp-secret-value" not in repr(holder["asr"])


async def test_exhausted_tencent_retries_keep_the_speech_error(monkeypatch, tmp_path):
    tencent_calls = {"n": 0}

    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_credential_payload())

    def tencent(_request: httpx.Request) -> httpx.Response:
        tencent_calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    hits, error, _holder, messages = await _relay_speech(monkeypatch, tmp_path, upstream, tencent)
    assert hits == []
    assert "HTTP 503" in error
    assert tencent_calls["n"] == 6
    assert "腾讯云识别重试中（2/5）" in messages
    assert "腾讯云识别重试中（5/5）" in messages
