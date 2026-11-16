import asyncio

import pytest

from app.core.deepseek import build_recognizer
from app.core.tencent_asr import FaultAsrClient, TencentAsrError
from app.core.transcript import SpeechSentence
from app.schemas import TEST_ACCOUNT_EMAIL, Frame, RunConfig, SettingUpdate
from app.services.manager import JobManager
from tests.test_asr import _accept_status, _state


def _frame(index: int) -> Frame:
    return Frame(index=index, start_sec=0, end_sec=1, path="frame.jpg")


def _sign_in(manager: JobManager, email: str) -> None:
    manager._settings["relay_email"] = email
    manager._settings["relay_access_token"] = "token"


async def test_only_the_test_account_can_enable_the_switches():
    manager = JobManager()
    await manager.init()
    try:
        _sign_in(manager, "other@qq.com")
        hidden = await manager.update_settings(
            SettingUpdate(mock_mode=True, fault_bad_json=True, fault_tencent=True, fault_partial_frames=True)
        )
        assert hidden.mock_mode is False
        assert hidden.fault_bad_json is False
        assert hidden.fault_tencent is False
        assert hidden.fault_partial_frames is False
        assert manager._settings["mock_mode"] is False

        _sign_in(manager, TEST_ACCOUNT_EMAIL)
        shown = await manager.update_settings(
            SettingUpdate(mock_mode=True, fault_bad_json=True, fault_tencent=True, fault_partial_frames=True)
        )
        assert shown.mock_mode is True
        assert shown.fault_bad_json is True
        assert shown.fault_tencent is True
        assert shown.fault_partial_frames is True

        _sign_in(manager, "other@qq.com")
        turned_off = await manager.update_settings(SettingUpdate(mock_mode=False, fault_bad_json=False))
        assert turned_off.mock_mode is False
        assert turned_off.fault_bad_json is False
        assert turned_off.fault_tencent is False
        assert manager._settings["mock_mode"] is False
        assert manager._settings["fault_tencent"] is True
    finally:
        await manager.shutdown()


async def test_faults_apply_only_while_the_test_account_is_logged_in():
    manager = JobManager()
    await manager.init()
    try:
        _sign_in(manager, TEST_ACCOUNT_EMAIL)
        manager._settings["fault_bad_json"] = True
        manager._settings["fault_tencent"] = True
        manager._settings["fault_partial_frames"] = True
        config = manager._config_for(_state(True, False))
        assert config.fault_bad_json is True
        assert config.fault_tencent is True
        assert config.fault_partial_frames is True

        _sign_in(manager, "other@qq.com")
        blocked = manager._config_for(_state(True, False))
        assert blocked.fault_bad_json is False
        assert blocked.fault_tencent is False
        assert blocked.fault_partial_frames is False
        assert blocked.mock is False
    finally:
        await manager.shutdown()


async def test_bad_json_and_partial_frames_skip_the_model():
    recognizer = build_recognizer(RunConfig(fault_bad_json=True, api_key=""))
    with pytest.raises(ValueError, match="可解析的 JSON"):
        await recognizer.recognize(_frame(1))
    with pytest.raises(ValueError, match="可解析的 JSON"):
        await recognizer.judge_speech([SpeechSentence("这款只要九十九元", 0, 1)])

    partial = build_recognizer(RunConfig(mock=True, fault_partial_frames=True))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await partial.recognize(_frame(1))
    assert await partial.recognize(_frame(2)) == []
    hits = await partial.recognize(_frame(3))
    assert hits[0].text == "¥199"
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await partial.recognize(_frame(4))


async def test_tencent_fault_fails_before_a_request():
    client = FaultAsrClient()
    with pytest.raises(TencentAsrError, match="无法连接腾讯云语音识别"):
        await client.transcribe([], cancel=asyncio.Event(), on_status=_accept_status)
