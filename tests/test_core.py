from app.core.aggregate import aggregate
from app.core.deepseek import chat_url
import pytest

from app.core.editor import EditorError, _parse_silences, draft_cuts, normalize_cuts, plan_cuts
from app.core.parsing import parse_frame_result
from app.core.prompts import SPEECH_SYSTEM_PROMPT, SYSTEM_PROMPT, visual_system_prompt
from app.core.rules import apply_rules
from app.core.timeutil import format_clock, format_range
from app.core.transcript import SpeechSentence
from app.paths import safe_filename
from app.schemas import DetectionRules, Finding, Frame, PhraseRule, RawHit


def _frame() -> Frame:
    return Frame(index=1, start_sec=12, end_sec=13, path="frame.jpg")


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
    prompt = visual_system_prompt(rules)
    assert "只报告这些预设类别：规格" in prompt
    assert "全网最低" in prompt
    assert prompt.rfind("禁止推测") > prompt.rfind("可以推测")


def test_chat_url_accepts_official_base():
    assert chat_url("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
    assert chat_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1/chat/completions"
    assert chat_url("https://api.deepseek.com/chat/completions") == "https://api.deepseek.com/chat/completions"
    assert chat_url("https://token4u.cc/v1") == "https://token4u.cc/v1/chat/completions"


def test_glm_request_enables_thinking_and_drops_image_detail():
    from app.core.deepseek import DeepSeekRecognizer, model_controls
    from app.core.pipeline import recognition_max_edge
    from app.core.relay import PICTURE_MODEL, configured_model
    from app.schemas import DEFAULT_SETTINGS, RunConfig

    assert configured_model("deepseek-flash") == PICTURE_MODEL
    assert configured_model("deepseek-v4.1-flash") == PICTURE_MODEL
    assert configured_model("deepseek-v4.1-flash-1017") == PICTURE_MODEL
    assert configured_model("DeepSeek-V4") == PICTURE_MODEL
    assert configured_model("  ") == PICTURE_MODEL
    assert configured_model("glm-4.5-air") == "glm-4.5-air"
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


async def test_saved_deepseek_model_loads_as_glm(tmp_path):
    from app.core.relay import PICTURE_MODEL
    from app.db import Database
    from app.schemas import DEFAULT_SETTINGS

    db = Database(tmp_path / "app.db")
    await db.connect()
    stored = dict(DEFAULT_SETTINGS)
    stored["model"] = "deepseek-v4.1-flash-1017"
    stored["base_url"] = "https://example.invalid"
    await db.save_settings(stored)
    loaded = await db.load_settings()
    assert loaded["model"] == PICTURE_MODEL
    assert loaded["base_url"] == "https://example.invalid"
    custom = dict(DEFAULT_SETTINGS)
    custom["model"] = "glm-4.5-air"
    await db.save_settings(custom)
    assert (await db.load_settings())["model"] == "glm-4.5-air"
    await db.close()
