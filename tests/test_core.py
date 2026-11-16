import asyncio
import json

import httpx
import pytest

from app.core.aggregate import aggregate
from app.core.deepseek import (
    DeepSeekRecognizer,
    SpeechJudgeError,
    _JSON_FIX,
    chat_url,
    speech_request_timeout,
)

from app.core.editor import EditorError, _parse_silences, draft_cuts, normalize_cuts, plan_cuts
from app.core.parsing import parse_frame_result
from app.core.pipeline import drop_retried_reviews, manual_review_hit, wait_while_paused
from app.core.prompts import SPEECH_SYSTEM_PROMPT, SYSTEM_PROMPT, speech_system_prompt, visual_system_prompt
from app.core.rules import apply_rules
from app.core.timeutil import format_clock, format_range
from app.core.transcript import SpeechSentence
from app.paths import safe_filename
from app.schemas import MANUAL_REVIEW_NOTE, DetectionRules, Finding, Frame, PhraseRule, RawHit, RunConfig


def _frame() -> Frame:
    return Frame(index=1, start_sec=12, end_sec=13, path="frame.jpg")


async def test_wait_while_paused_holds_until_resume_or_cancel():
    resume = asyncio.Event()
    cancel = asyncio.Event()
    started = asyncio.Event()

    async def hold():
        started.set()
        await wait_while_paused(cancel, resume)

    task = asyncio.create_task(hold())
    await started.wait()
    await asyncio.sleep(0.05)
    assert not task.done()
    resume.set()
    await asyncio.wait_for(task, timeout=1)

    resume.clear()
    cancel.set()
    await asyncio.wait_for(wait_while_paused(cancel, resume), timeout=1)


def test_format_range_matches_required_example():
    assert format_range(12, 15) == "00:12-00:15"
    assert format_range(0, 1) == "00:00-00:01"
    assert format_clock(3661) == "01:01:01"
    assert format_range(3600, 3661) == "01:00:00-01:01:01"


def test_safe_filename_keeps_chinese_and_drops_path():
    assert safe_filename(r"..\目录\价格 演示.mp4") == "价格 演示.mp4"
    assert safe_filename("CON.mp4") == "_CON.mp4"
    assert safe_filename('a:b?.mp4') == "a_b_.mp4"


def test_prompt_forbids_guessing():
    assert "禁止" in SYSTEM_PROMPT
    assert "推测" in SYSTEM_PROMPT
    assert "价格" in SYSTEM_PROMPT and "规格" in SYSTEM_PROMPT and "品牌" in SYSTEM_PROMPT
    assert "禁止" in SPEECH_SYSTEM_PROMPT
    assert "推测" in SPEECH_SYSTEM_PROMPT
    assert "口播" in SPEECH_SYSTEM_PROMPT


def test_parse_keeps_visible_text_and_drops_speculation():
    content = """```json
{"items":[
  {"类别":"价格","原文":"¥199","位置":"画面右下角价格贴纸","置信度":"确定","判定理由":"带货币符号且位于商品旁价格标签","备注":""},
  {"category":"brand","text":"耐克","position":"左上角","confidence":"疑似","reason":"根据常识推测为耐克","note":""},
  {"类别":"规格","原文":"500ml","位置":"瓶身标签","置信度":"确定","判定理由":"瓶身可见容量文字","备注":""},
  {"类别":"价格","原文":"无","位置":"画面中","置信度":"确定","判定理由":"没有价格","备注":""}
]}
```"""
    hits = parse_frame_result(content, _frame())
    assert [hit.text for hit in hits] == ["¥199", "500ml"]
    assert hits[0].category == "价格"
    assert hits[1].category == "规格"


def test_aggregate_merges_adjacent_frames_and_splits_gaps():
    reason = "带货币符号且位于商品旁价格标签"
    def hit(start: float, end: float, category: str, text: str, position: str, confidence: str, why: str) -> RawHit:
        return RawHit(
            start_sec=start,
            end_sec=end,
            category=category,
            text=text,
            position=position,
            confidence=confidence,
            reason=why,
        )

    hits = [
        hit(12, 13, "价格", "¥199", "画面右下角价格贴纸", "确定", reason),
        hit(13, 14, "价格", "¥199", "画面右下角价格贴纸", "确定", reason),
        hit(14, 15, "价格", "¥199", "画面右下角价格贴纸", "疑似", "数字略微模糊"),
        hit(20, 21, "价格", "¥199", "画面右下角价格贴纸", "确定", reason),
        hit(12, 13, "规格", "500ml", "瓶身标签", "确定", "瓶身可见容量文字"),
    ]
    findings = aggregate(hits)
    assert findings[0].export_dict() == {
        "时间点": "00:12-00:15",
        "类别": "价格",
        "来源": "画面",
        "原文": "¥199",
        "位置": "画面右下角价格贴纸",
        "置信度": "疑似",
        "判定理由": "带货币符号且位于商品旁价格标签；数字略微模糊",
        "备注": "",
    }
    assert findings[0].source == "画面"
    assert findings[1].category == "规格"
    assert findings[1].time_range == "00:12-00:13"
    assert findings[2].time_range == "00:20-00:21"
    assert findings[2].confidence == "确定"


def test_aggregate_folds_a_price_sticker_that_stays_on_screen():
    reason = "画面中上方黄色爆炸贴内可见价格"

    def hit(start: float, end: float, text: str) -> RawHit:
        return RawHit(
            start_sec=start,
            end_sec=end,
            category="价格",
            text=text,
            position="画面中上方黄色爆炸贴内",
            confidence="确定",
            reason=reason,
            source="画面",
        )

    findings = aggregate(
        [
            hit(0, 1, "5.55元"),
            hit(1, 2, "5.55元3把"),
            hit(2, 4, "5.55元"),
            hit(4, 5, "5:55元3把"),
            hit(5, 9, "5.55元"),
            hit(7, 9, "4.9元"),
            hit(12, 13, "200元"),
        ]
    )
    prices = [item for item in findings if item.category == "价格"]
    sticker = next(item for item in prices if item.text == "5.55元3把")
    assert sticker.start_sec == 0
    assert sticker.end_sec == 9
    assert "多种读法" in sticker.note
    assert {item.text for item in prices} == {"5.55元3把", "4.9元", "200元"}


def test_aggregate_joins_one_missed_frame_and_keeps_a_later_flash():
    def hit(start: float, end: float) -> RawHit:
        return RawHit(
            start_sec=start,
            end_sec=end,
            category="规格",
            text="3把",
            position="画面中上方",
            confidence="确定",
            reason="画面可见数量",
            source="画面",
        )

    findings = aggregate([hit(0, 1), hit(2, 3), hit(12, 13)])
    assert [(item.start_sec, item.end_sec) for item in findings] == [(0, 3), (12, 13)]


def test_picture_price_fold_does_not_change_speech_wording():
    findings = aggregate(
        [
            RawHit(
                start_sec=0,
                end_sec=1,
                category="价格",
                text="5.55元",
                position="画面中可见",
                confidence="确定",
                reason="可见价格",
                source="画面",
            ),
            RawHit(
                start_sec=1,
                end_sec=2,
                category="价格",
                text="5.55元3把",
                position="画面中可见",
                confidence="确定",
                reason="可见价格",
                source="画面",
            ),
            RawHit(
                start_sec=0,
                end_sec=2,
                category="价格",
                text="5.55元",
                position="口播内容",
                confidence="确定",
                reason="口播中明确提及价格",
                source="口播",
            ),
        ]
    )
    assert len(findings) == 2
    picture = next(item for item in findings if item.source == "画面")
    speech = next(item for item in findings if item.source == "口播")
    assert picture.text == "5.55元3把"
    assert picture.end_sec == 2
    assert speech.text == "5.55元"


def test_aggregate_keeps_picture_and_speech_apart():
    reason = "口播中明确提及价格"
    shared = dict(
        start_sec=12,
        end_sec=15,
        category="价格",
        text="九十九元",
        confidence="确定",
        reason=reason,
    )
    findings = aggregate(
        [
            RawHit(**shared, position="口播内容", source="口播"),
            RawHit(**shared, position="画面中可见", source="画面"),
        ]
    )
    assert len(findings) == 2
    assert {item.source for item in findings} == {"画面", "口播"}


def _speech(start: float, end: float, sentence_end: float | None = None, next_start: float | None = None) -> Finding:
    return Finding(
        time_range="00:00-00:01",
        category="价格",
        source="口播",
        text="这款只要九十九元",
        position="口播内容",
        confidence="确定",
        reason="口播中明确提及价格",
        start_sec=start,
        end_sec=end,
        sentence_start=start,
        sentence_end=sentence_end if sentence_end is not None else end,
        next_start=next_start,
    )


def test_failed_frames_stay_in_the_export_and_out_of_the_cut():
    hits = [
        manual_review_hit(0, 1, "模型没有返回可解析的 JSON"),
        manual_review_hit(1, 2, "模型请求超时"),
        RawHit(
            start_sec=4,
            end_sec=5,
            category="价格",
            text="¥199",
            position="右下角",
            confidence="确定",
            reason="画面写着价格",
        ),
    ]
    findings = aggregate(hits)
    assert [item.category for item in findings] == ["解析失败", "解析失败", "价格"]
    assert findings[0].note == MANUAL_REVIEW_NOTE
    assert findings[0].confidence == ""
    assert findings[0].start_sec == 0
    assert findings[0].end_sec == 1
    exported = findings[0].export_dict()
    assert exported["备注"] == "解析失败，需要人工审核"
    assert exported["时间点"]
    assert "JSON" in exported["判定理由"]
    assert findings[1].start_sec == 1
    cuts, _keeps = plan_cuts(10, findings, padding=0.5)
    assert cuts == [(3.5, 5.5)]
    with pytest.raises(EditorError, match="没有命中区间"):
        plan_cuts(10, findings[:2], padding=0)
    kept = drop_retried_reviews(findings[:2], [1], fps=1, duration=10)
    assert [(item.start_sec, item.end_sec) for item in kept] == [(1.0, 2.0)]


def test_plan_cuts_removes_hit_range_with_padding():
    finding = Finding(
        time_range="00:02-00:04",
        category="价格",
        text="¥199",
        position="画面右下角价格贴纸",
        confidence="确定",
        reason="带货币符号且位于商品旁价格标签",
        start_sec=2,
        end_sec=4,
    )
    cuts, keeps = plan_cuts(10, [finding], padding=0.5)
    assert cuts == [(1.5, 4.5)]
    assert keeps == [(0.0, 1.5), (4.5, 10)]


def test_speech_cut_follows_the_quoted_words_and_one_character_of_tail():
    # 引用停在 3.6，整句从 2.0 到 4.0。删除段贴着引用，句尾多一个字，不扩到整句。
    finding = _speech(3.6, 3.6, sentence_end=4.0, next_start=4.8)
    finding = finding.model_copy(update={"sentence_start": 2.0})
    cuts, keeps = plan_cuts(10, [finding], padding=0.2)
    assert cuts[0][0] == pytest.approx(3.55, abs=0.02)
    assert cuts[0][1] == pytest.approx(3.86, abs=0.02)
    assert cuts[0][1] < 4.0
    assert keeps[0][0] == 0
    assert keeps[-1][1] == 10


def test_picture_padding_does_not_stretch_the_spoken_quote():
    finding = _speech(3.0, 3.4, sentence_end=9.0, next_start=8.0)
    finding = finding.model_copy(update={"sentence_start": 0.2})
    cuts, _keeps = plan_cuts(10, [finding], padding=2.0)
    assert cuts[0][0] == pytest.approx(2.95, abs=0.02)
    assert cuts[0][1] == pytest.approx(3.66, abs=0.02)


def test_speech_cut_follows_the_pause_when_timestamps_overlap_one_character():
    # 识别把下一句的起点标进了 A 的最后一个字，真正的停顿在 4.22。
    finding = _speech(2.0, 4.0, next_start=4.10)
    cuts, _keeps = plan_cuts(10, [finding], padding=0.2, silences=[(4.22, 4.55)])
    assert cuts[0][1] >= 4.22
    assert cuts[0][1] <= 4.56


def test_speech_cut_crosses_a_touching_timestamp_by_about_one_character():
    finding = _speech(2.0, 4.0, next_start=4.08)
    cuts, _keeps = plan_cuts(10, [finding], padding=0.0)
    assert 4.18 <= cuts[0][1] <= 4.40


def test_normalize_cuts_keeps_several_separate_ranges():
    cuts, keeps = normalize_cuts(22, [(20, 21), (3, 5), (10, 14)])
    assert cuts == [(3.0, 5.0), (10.0, 14.0), (20.0, 21.0)]
    assert keeps == [(0.0, 3.0), (5.0, 10.0), (14.0, 20.0), (21.0, 22.0)]


def test_normalize_cuts_keeps_the_seconds_the_user_dragged():
    cuts, keeps = normalize_cuts(10, [(2, 4)])
    assert cuts == [(2.0, 4.0)]
    assert keeps == [(0.0, 2.0), (4.0, 10.0)]


def test_normalize_cuts_drops_slivers_merges_overlaps_and_swaps_ends():
    cuts, keeps = normalize_cuts(10, [(1, 1.02), (4, 2.5), (3.2, 3.6)])
    assert cuts == [(2.5, 4.0)]
    assert keeps == [(0.0, 2.5), (4.0, 10.0)]


def test_speech_plan_uses_the_quoted_span_when_the_sentence_covers_the_video():
    intro = _speech(5.31, 6.44, sentence_end=14.08, next_start=14.08).model_copy(update={"sentence_start": 0.0})
    ending = _speech(14.09, 16.2, sentence_end=21.84).model_copy(
        update={"sentence_start": 14.08, "text": "品牌是某某官方正品"}
    )
    cuts, keeps = plan_cuts(21.84, [intro, ending], padding=0.2)
    assert cuts[0][0] == pytest.approx(5.26, abs=0.02)
    assert cuts[0][1] == pytest.approx(6.70, abs=0.02)
    assert cuts[1][0] == pytest.approx(14.04, abs=0.02)
    assert cuts[1][1] == pytest.approx(16.46, abs=0.02)
    assert keeps[0] == (0.0, round(cuts[0][0], 3))
    assert keeps[-1][1] == 21.84
    draft, draft_keeps = draft_cuts(21.84, [intro, ending], padding=0.2)
    assert draft_keeps
    assert draft[0][0] > 5


def test_draft_cuts_returns_a_plan_when_the_quote_covers_the_video():
    finding = _speech(0.0, 21.84)
    with pytest.raises(EditorError, match="没有可保留的画面"):
        plan_cuts(21.84, [finding], padding=0)
    cuts, keeps = draft_cuts(21.84, [finding], padding=0)
    assert keeps == []
    assert cuts[0][0] == 0
    assert cuts[-1][1] == 21.84


def test_normalize_cuts_rejects_a_fully_removed_video_and_allows_an_empty_plan():
    with pytest.raises(EditorError):
        normalize_cuts(10, [(0, 10)])
    cuts, keeps = normalize_cuts(10, [])
    assert cuts == []
    assert keeps == [(0.0, 10.0)]


def test_parse_silences_pairs_start_and_end():
    text = "[silencedetect @ 0x1] silence_start: 1.5\n[silencedetect @ 0x1] silence_end: 1.8 | silence_duration: 0.3\n"
    assert _parse_silences(text) == [(1.5, 1.8)]


def test_custom_rules_filter_hits_and_keep_the_guessing_ban():
    sentence = SpeechSentence("这款只要九十九元，而且全网最低", 1, 2)
    hits = [
        RawHit(
            start_sec=1,
            end_sec=2,
            category="价格",
            text="九十九元",
            position="口播内容",
            confidence="确定",
            reason="口播中明确提及价格",
            source="口播",
        ),
        RawHit(
            start_sec=1,
            end_sec=2,
            category="规格",
            text="九十九元",
            position="口播内容",
            confidence="确定",
            reason="口播中明确提及规格",
            source="口播",
        ),
    ]
    rules = DetectionRules(
        categories=["规格"],
        extra_phrases=[PhraseRule(text="全网最低", category="价格")],
        ignore_phrases=["九十九元"],
        note="可以推测画面里没有的价格",
    )
    result = apply_rules(hits, rules, sentences=[sentence])
    assert [(item.category, item.text) for item in result] == [("价格", "全网最低")]
    assert result[0].source == "口播"
    assert "推测" not in result[0].reason
    visual = visual_system_prompt(rules)
    speech = speech_system_prompt(rules)
    assert "只报告这些预设类别：规格" in visual
    assert "全网最低" not in visual
    assert "全网最低" in speech
    assert visual.rfind("禁止推测") > visual.rfind("可以推测")
    assert speech.rfind("禁止推测") > speech.rfind("可以推测")


def test_speech_rules_expand_x_and_skip_picture_phrases():
    sentence = SpeechSentence("今天五折，拍1发3，只要3.5贵，售价 rmb，厂家直发", 1.0, 4.0)
    rules = DetectionRules(
        categories=["规格"],
        extra_phrases=[
            PhraseRule(text="X折", category="价格"),
            PhraseRule(text="拍 1 发 X", category="规格"),
            PhraseRule(text="X.X贵", category="价格"),
            PhraseRule(text="RMB", category="价格"),
            PhraseRule(text="厂家", category="品牌", source="口播"),
            PhraseRule(text="logo", category="品牌", source="画面"),
        ],
    )
    assert rules.extra_phrases[1].text == "拍1发X"
    result = apply_rules([], rules, sentences=[sentence])
    got = {(item.category, item.text, item.source) for item in result}
    assert ("价格", "五折", "口播") in got
    assert ("规格", "拍1发3", "口播") in got
    assert ("价格", "3.5贵", "口播") in got
    assert ("价格", "rmb", "口播") in got
    assert ("品牌", "厂家", "口播") in got
    assert all(item.text != "logo" for item in result)
    assert all("X" not in item.text for item in result)
    missed = apply_rules([], DetectionRules(extra_phrases=[PhraseRule(text="X折", category="价格")]), sentences=[SpeechSentence("正在打折", 0, 1)])
    assert missed == []
    decimal = apply_rules(
        [],
        DetectionRules(extra_phrases=[PhraseRule(text="X.X贵", category="价格")]),
        sentences=[SpeechSentence("这件三点五贵", 0, 1)],
    )
    assert [item.text for item in decimal] == ["三点五贵"]


def test_builtin_phrases_validate_and_split_prompts():
    from app.core.rule_catalog import apply_builtin_rules, default_extra_phrases

    raw = default_extra_phrases()
    rules = DetectionRules(extra_phrases=[PhraseRule.model_validate(item) for item in raw])
    assert len(rules.extra_phrases) == 111
    assert sum(item.source == "口播" and item.category == "价格" for item in rules.extra_phrases) == 43
    assert sum(item.source == "口播" and item.category == "规格" for item in rules.extra_phrases) == 48
    assert sum(item.source == "口播" and item.category == "品牌" for item in rules.extra_phrases) == 6
    assert sum(item.source == "画面" and item.category == "品牌" for item in rules.extra_phrases) == 14
    speech = speech_system_prompt(rules)
    visual = visual_system_prompt(rules)
    assert "白菜价" in speech
    assert "X 表示数字" in speech
    assert "logo" not in speech
    assert "口播违规字幕" not in speech
    assert "logo" in visual
    assert "口播违规字幕" in visual
    assert "白菜价" not in visual
    fresh = {"rules": {"categories": ["价格", "规格", "品牌"], "extra_phrases": [], "ignore_phrases": [], "note": "保留"}}
    assert apply_builtin_rules(fresh) is True
    assert fresh["rules_initialized"] is True
    assert len(fresh["rules"]["extra_phrases"]) == 111
    assert fresh["rules"]["note"] == "保留"
    fresh["rules"]["extra_phrases"] = []
    assert apply_builtin_rules(fresh) is False
    assert fresh["rules"]["extra_phrases"] == []


def test_exaggeration_keeps_the_absolute_words_and_drops_ordinary_speech():
    from app.core.exaggeration import clip_exaggeration, quote_is_exaggeration
    from app.core.parsing import parse_speech_result
    from app.core.rule_catalog import enable_exaggeration

    assert quote_is_exaggeration("全网最好")
    assert quote_is_exaggeration("100%有效")
    assert not quote_is_exaggeration("最后一波")
    assert not quote_is_exaggeration("质量很好")
    assert not quote_is_exaggeration("最小号")
    assert clip_exaggeration("这是全网最好的肠衣") == "全网最好"
    sentence = SpeechSentence("这是全网最好的肠衣，最后一波，质量很好", 1.0, 4.0)
    content = (
        '{"items":['
        '{"句子":1,"类别":"夸大宣传","原文":"这是全网最好的肠衣","置信度":"确定","判定理由":"口播中出现夸大宣传用语"},'
        '{"句子":1,"类别":"夸大宣传","原文":"质量很好","置信度":"确定","判定理由":"口播中出现夸大宣传用语"},'
        '{"句子":1,"类别":"夸大宣传","原文":"最后一波","置信度":"确定","判定理由":"口播中出现夸大宣传用语"}'
        "]}"
    )
    hits = parse_speech_result(content, [sentence])
    assert [item.text for item in hits] == ["全网最好"]
    assert hits[0].category == "夸大宣传"
    assert hits[0].position == "口播内容"
    picture = parse_frame_result(
        '{"items":[{"类别":"夸大宣传","原文":"全网最好","位置":"字幕","置信度":"确定","判定理由":"画面上写着"}]}',
        _frame(),
    )
    assert picture == []

    on = speech_system_prompt(DetectionRules())
    assert "4. 夸大宣传" in on
    assert "不判断真假" in on
    assert "最后" in on
    off = speech_system_prompt(DetectionRules(categories=["价格", "规格", "品牌"]))
    assert "4. 夸大宣传" not in off
    assert "夸大宣传" not in visual_system_prompt(DetectionRules())

    stored = {"rules": {"categories": ["价格"], "extra_phrases": [], "ignore_phrases": [], "note": ""}}
    assert enable_exaggeration(stored) is True
    assert stored["rules"]["categories"] == ["价格", "夸大宣传"]
    stored["rules"]["categories"] = ["价格"]
    assert enable_exaggeration(stored) is False
    assert stored["rules"]["categories"] == ["价格"]


def _chat_response(content: str, status: int = 200) -> httpx.Response:
    if status != 200:
        return httpx.Response(status, json={"error": {"message": content}})
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


def test_speech_request_waits_longer_than_a_frame():
    assert speech_request_timeout(60).read == 120
    assert speech_request_timeout(150).read == 150
    assert speech_request_timeout(240).read == 180


async def test_speech_timeout_keeps_phrase_hits():
    rules = DetectionRules(
        categories=["价格", "夸大宣传"],
        extra_phrases=[PhraseRule(text="个位数", category="价格", source="口播")],
    )
    recognizer = DeepSeekRecognizer(RunConfig(api_key="sk-test", timeout_sec=60, rules=rules))
    recognizer._client = object()
    seen: dict = {}

    async def fail(client, body, *, timeout=None, attempts=3):
        seen["timeout"] = timeout
        seen["attempts"] = attempts
        raise RuntimeError("模型请求超时")

    recognizer._complete = fail
    sentences = [SpeechSentence("个位数到手赶紧去抢吧", 18.0, 20.0)]
    with pytest.raises(SpeechJudgeError) as caught:
        await recognizer.judge_speech(sentences)
    assert str(caught.value) == "模型请求超时，已按词表保留 1 条"
    assert [(item.category, item.text, item.source) for item in caught.value.hits] == [
        ("价格", "个位数", "口播")
    ]
    assert seen["attempts"] == 2
    assert seen["timeout"].read == 120

    recognizer.config = RunConfig(api_key="sk-test", timeout_sec=60, rules=DetectionRules(categories=["价格"]))
    with pytest.raises(RuntimeError, match="^模型请求超时$"):
        await recognizer.judge_speech(sentences)


async def test_frame_retries_once_when_the_model_returns_unparsed_json(tmp_path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8frame")
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return _chat_response("response_format is unsupported", status=400)
        if len(calls) == 2:
            return _chat_response("这一帧我用文字说明，不是 JSON")
        return _chat_response(
            '{"items":[{"类别":"价格","原文":"¥9.9","位置":"左下角","置信度":"确定","判定理由":"画面写着价格","备注":""}]}'
        )

    recognizer = DeepSeekRecognizer(RunConfig(api_key="sk-test", thinking=False, base_url="https://token4u.cc/v1"))
    recognizer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        hits = await recognizer.recognize(Frame(index=57, start_sec=56, end_sec=57, path=str(image)))
    finally:
        await recognizer.aclose()
    assert [item.text for item in hits] == ["¥9.9"]
    assert len(calls) == 3
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert "response_format" not in calls[2]
    assert calls[2]["messages"][-1] == {"role": "user", "content": _JSON_FIX}
    picture = calls[2]["messages"][1]["content"]
    assert picture[1]["type"] == "image_url"
    assert picture[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_frame_reports_the_parse_error_when_the_retry_is_also_invalid(tmp_path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8frame")
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return _chat_response("仍然不是 JSON")

    recognizer = DeepSeekRecognizer(RunConfig(api_key="sk-test", thinking=False, base_url="https://token4u.cc/v1"))
    recognizer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match="可解析的 JSON"):
            await recognizer.recognize(Frame(index=57, start_sec=56, end_sec=57, path=str(image)))
    finally:
        await recognizer.aclose()
    assert len(calls) == 2
    assert calls[1]["messages"][-1]["content"] == _JSON_FIX


def test_chat_url_accepts_official_base():
    assert chat_url("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
    assert chat_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1/chat/completions"
    assert chat_url("https://api.deepseek.com/chat/completions") == "https://api.deepseek.com/chat/completions"
    assert chat_url("https://token4u.cc/v1") == "https://token4u.cc/v1/chat/completions"


def test_glm_request_enables_thinking_and_drops_image_detail():
    from app.core.deepseek import DeepSeekRecognizer, model_controls
    from app.core.pipeline import recognition_max_edge
    from app.core.relay import DEEPSEEK_FLASH_MODEL, PICTURE_MODEL, configured_model
    from app.schemas import DEFAULT_SETTINGS, RunConfig

    assert configured_model("deepseek-flash") == DEEPSEEK_FLASH_MODEL
    assert configured_model("deepseek-v4.1-flash") == DEEPSEEK_FLASH_MODEL
    assert configured_model("deepseek-v4.1-flash-1017") == DEEPSEEK_FLASH_MODEL
    assert configured_model("DeepSeek-V4") == DEEPSEEK_FLASH_MODEL
    assert configured_model("  ") == PICTURE_MODEL
    assert configured_model("glm-5.3-flash") == PICTURE_MODEL
    assert configured_model("glm-4.5-air") == PICTURE_MODEL
    assert RunConfig().model == PICTURE_MODEL
    assert DEFAULT_SETTINGS["model"] == PICTURE_MODEL

    assert model_controls(False) == {
        "max_tokens": 2048,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert model_controls(True)["reasoning_effort"] == "high"
    assert model_controls(True)["max_tokens"] == 4096

    recognizer = DeepSeekRecognizer(RunConfig(thinking=False, detail="low"))
    body = recognizer._body(Frame(index=0, start_sec=0, end_sec=1, path="frame.jpg"), "abcd")
    assert body["model"] == PICTURE_MODEL
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 2048
    assert body["response_format"] == {"type": "json_object"}
    assert "temperature" not in body
    image = body["messages"][1]["content"][1]
    assert image == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcd"}}

    speech = recognizer._speech_body([SpeechSentence(text="九十九元", start_sec=1, end_sec=2)])
    assert speech["model"] == PICTURE_MODEL
    assert speech["response_format"] == {"type": "json_object"}
    assert speech["reasoning_effort"] == "low"
    assert "image_url" not in str(speech["messages"])

    thinking = DeepSeekRecognizer(RunConfig(thinking=True))
    assert thinking._speech_body([SpeechSentence(text="九十九元", start_sec=1, end_sec=2)])["reasoning_effort"] == "high"
    assert recognition_max_edge(1280, "original") == 1280
    assert recognition_max_edge(1280, "high") == 1280
    assert recognition_max_edge(1280, "low") == 640
    assert recognition_max_edge(480, "low") == 480


async def test_saved_model_collapses_to_the_two_choices(tmp_path):
    from app.core.relay import DEEPSEEK_FLASH_MODEL, PICTURE_MODEL
    from app.db import Database
    from app.schemas import DEFAULT_SETTINGS

    db = Database(tmp_path / "app.db")
    await db.connect()
    stored = dict(DEFAULT_SETTINGS)
    stored["model"] = "deepseek-v4.1-flash-1017"
    stored["base_url"] = "https://example.invalid"
    await db.save_settings(stored)
    loaded = await db.load_settings()
    assert loaded["model"] == DEEPSEEK_FLASH_MODEL
    assert loaded["base_url"] == "https://example.invalid"
    custom = dict(DEFAULT_SETTINGS)
    custom["model"] = "glm-4.5-air"
    await db.save_settings(custom)
    assert (await db.load_settings())["model"] == PICTURE_MODEL
    await db.close()
