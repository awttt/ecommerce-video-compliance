"""已通过的共享违规词要进入检测。请求都走假的传输层。"""

import httpx

from app.core.rules import apply_rules
from app.core.shared_rules import (
    SharedRulesError,
    admin_shared_request,
    fetch_approved_shared_rules,
    merge_shared_rules,
    phrases_from_shared,
)
from app.core.transcript import SpeechSentence
from app.schemas import DetectionRules


def test_approved_shared_word_is_detected_in_speech():
    shared = phrases_from_shared(
        [
            {"word": "全网最低", "category": "价格", "status": "approved"},
            {"word": "还没过", "category": "价格", "status": "pending"},
            {"word": "绝对第一", "category": "夸大宣传", "status": "approved"},
        ]
    )
    texts = {(item.source, item.text) for item in shared}
    assert ("口播", "全网最低") in texts
    assert ("画面", "全网最低") in texts
    assert ("口播", "绝对第一") in texts
    assert ("画面", "绝对第一") not in texts
    assert all(item.text != "还没过" for item in shared)

    rules = merge_shared_rules(DetectionRules(), shared)
    hits = apply_rules([], rules, sentences=[SpeechSentence("号称全网最低", 1.0, 2.0)])
    assert any("全网最低" in hit.text for hit in hits)
    again = merge_shared_rules(rules, shared)
    assert len(again.extra_phrases) == len(rules.extra_phrases)


def test_fetch_hides_token_and_returns_approved_rows():
    token = "relay-token-for-test"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {token}"
        if request.url.path.endswith("/boom"):
            return httpx.Response(502, json={"detail": f"失败 {token}"})
        return httpx.Response(
            200,
            json=[{"word": "全网最低", "category": "价格", "status": "approved"}],
        )

    transport = httpx.MockTransport(handler)
    rows = fetch_approved_shared_rules(token, base_url="https://admin.example.test", transport=transport)
    assert rows[0]["word"] == "全网最低"
    try:
        admin_shared_request(
            token,
            "GET",
            "/v1/shared-rules/boom",
            base_url="https://admin.example.test",
            transport=transport,
        )
    except SharedRulesError as exc:
        assert token not in str(exc)
        assert exc.status == 502
    else:
        raise AssertionError("expected SharedRulesError")
