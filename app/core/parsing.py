"""解析模型输出。丢弃类别不合法、原文为空或理由里带推测的条目。"""

from __future__ import annotations

import json
import re

from app.core.transcript import SpeechSentence
from app.schemas import Frame, RawHit

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_CATEGORY = {
    "价格": "价格",
    "price": "价格",
    "规格": "规格",
    "spec": "规格",
    "specification": "规格",
    "品牌": "品牌",
    "brand": "品牌",
}
_CONFIDENCE = {
    "确定": "确定",
    "确认": "确定",
    "疑似": "疑似",
    "可能": "疑似",
    "不确定": "疑似",
}
_EMPTY_TEXT = {
    "无",
    "没有",
    "无内容",
    "未显示",
    "未识别",
    "看不清",
    "n/a",
    "na",
    "none",
    "null",
    "-",
    "—",
    "无价格",
    "无规格",
    "无品牌",
}
_SPECULATIVE = ("推测", "猜测", "常识", "应该是", "看起来像", "估计是", "可能是")
_MAX_ITEMS = 12
_MAX_TEXT = 80


def extract_json(content: str) -> object:
    text = _FENCE.sub("", (content or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("模型没有返回可解析的 JSON") from None


def parse_frame_result(content: str, frame: Frame) -> list[RawHit]:
    data = extract_json(content)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        raw_items = data.get("items", data.get("结果", []))
        if not isinstance(raw_items, list):
            raise ValueError("模型 JSON 中的 items 不是数组")
        items = raw_items
    else:
        raise ValueError("模型 JSON 格式无法识别")

    hits: list[RawHit] = []
    seen: set[tuple[str, str]] = set()
    for item in items[:_MAX_ITEMS]:
        hit = _one(item, start_sec=frame.start_sec, end_sec=frame.end_sec, source="画面")
        if hit is None:
            continue
        key = (hit.category, _norm(hit.text))
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
    return hits


def parse_speech_result(
    content: str,
    sentences: list[SpeechSentence],
    corpus: list[SpeechSentence] | None = None,
) -> list[RawHit]:
    """口播判定。原文必须是某一句里连续出现的原话，位置固定为口播内容。"""
    corpus = corpus or sentences
    data = extract_json(content)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        raw_items = data.get("items", data.get("结果", []))
        if not isinstance(raw_items, list):
            raise ValueError("模型 JSON 中的 items 不是数组")
        items = raw_items
    else:
        raise ValueError("模型 JSON 格式无法识别")

    hits: list[RawHit] = []
    seen: set[tuple[str, str, float]] = set()
    for item in items[:24]:
        hit = _speech_one(item, sentences, corpus)
        if hit is None:
            continue
        key = (hit.category, _norm(hit.text), round(hit.start_sec, 2))
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
    return hits


def _speech_one(
    item: object,
    sentences: list[SpeechSentence],
    corpus: list[SpeechSentence],
) -> RawHit | None:
    if not isinstance(item, dict) or not sentences:
        return None
    quote = _clean_text(_pick(item, "原文", "text", "原文内容"))
    if not quote:
        return None
    sentence = _match_sentence(item, quote, sentences)
    if sentence is None:
        return None
    located = locate_quote(sentence.text, quote)
    if not located:
        return None
    start, end = tighten_span(sentence, located)
    hit = _one(
        item,
        start_sec=start,
        end_sec=end,
        source="口播",
        text_override=located,
        force_position="口播内容",
    )
    if hit is None:
        return None
    hit.sentence_start = sentence.start_sec
    hit.sentence_end = sentence.end_sec
    hit.next_start = _next_sentence_start(sentence, corpus)
    return hit


def _next_sentence_start(sentence: SpeechSentence, corpus: list[SpeechSentence]) -> float | None:
    ordered = sorted(corpus, key=lambda item: (item.start_sec, item.end_sec, item.text))
    index = _sentence_index(sentence, ordered)
    if index is None or index + 1 >= len(ordered):
        return None
    nxt = ordered[index + 1].start_sec
    if nxt + 1e-3 < sentence.end_sec:
        return None
    return nxt


def _sentence_index(sentence: SpeechSentence, ordered: list[SpeechSentence]) -> int | None:
    for index, item in enumerate(ordered):
        if item is sentence:
            return index
    for index, item in enumerate(ordered):
        if item.start_sec == sentence.start_sec and item.text == sentence.text:
            return index
    return None


def _match_sentence(item: dict, quote: str, sentences: list[SpeechSentence]) -> SpeechSentence | None:
    raw = item.get("句子", item.get("sentence", item.get("id")))
    index = _index(raw)
    if index is not None and 0 <= index < len(sentences):
        if locate_quote(sentences[index].text, quote):
            return sentences[index]
    for sentence in sentences:
        if locate_quote(sentence.text, quote):
            return sentence
    return None


def _index(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw - 1
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip()) - 1
    return None


def locate_quote(sentence: str, quote: str) -> str | None:
    """返回句子中与原文对应的连续片段。允许两边空白不同，不允许改字。"""
    quote = quote.strip()
    if not quote or not sentence:
        return None
    if quote in sentence:
        return quote
    compact: list[str] = []
    indexes: list[int] = []
    for index, char in enumerate(sentence):
        if char.isspace():
            continue
        compact.append(char)
        indexes.append(index)
    needle = "".join(char for char in quote if not char.isspace())
    if not needle:
        return None
    found = "".join(compact).find(needle)
    if found < 0:
        return None
    start = indexes[found]
    end = indexes[found + len(needle) - 1] + 1
    return sentence[start:end]


def tighten_span(sentence: SpeechSentence, quote: str) -> tuple[float, float]:
    if not sentence.words:
        return sentence.start_sec, sentence.end_sec
    target = _spoken_norm(quote)
    if not target:
        return sentence.start_sec, sentence.end_sec
    chars: list[tuple[str, int]] = []
    for index, word in enumerate(sentence.words):
        for char in _spoken_norm(word.text):
            chars.append((char, index))
    hay = "".join(char for char, _index in chars)
    found = hay.find(target)
    if found < 0:
        return sentence.start_sec, sentence.end_sec
    first = chars[found][1]
    last = chars[found + len(target) - 1][1]
    start = sentence.words[first].start_sec
    end = sentence.words[last].end_sec
    if end <= start:
        return sentence.start_sec, sentence.end_sec
    return start, end


def _spoken_norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]…·]", "", text)


def _one(
    item: object,
    *,
    start_sec: float,
    end_sec: float,
    source: str,
    text_override: str | None = None,
    force_position: str | None = None,
) -> RawHit | None:
    if not isinstance(item, dict):
        return None
    category = _CATEGORY.get(_pick(item, "类别", "category").strip().casefold(), "")
    # casefold would break Chinese keys if we casefold before lookup. Handle both.
    if not category:
        category = _CATEGORY.get(_pick(item, "类别", "category").strip(), "")
    confidence = _CONFIDENCE.get(_pick(item, "置信度", "confidence").strip(), "")
    text = text_override if text_override is not None else _clean_text(_pick(item, "原文", "text", "原文内容"))
    if not category or not confidence or not text:
        return None
    if text.casefold() in _EMPTY_TEXT or len(text) > _MAX_TEXT:
        return None
    if not re.search(r"[\w\u4e00-\u9fff¥$€£]", text):
        return None
    raw_reason = _pick(item, "判定理由", "reason")
    if not raw_reason:
        raw_reason = f"口播中明确提及{category}" if source == "口播" else "画面中可见对应文字"
    reason = _clip(raw_reason, 120)
    if any(word in reason for word in _SPECULATIVE):
        return None
    if force_position:
        position = force_position
    else:
        position = _clip(_pick(item, "位置", "position"), 40)
        if not position:
            position = "画面中可见"
            confidence = "疑似"
    note = _clip(_pick(item, "备注", "note"), 80)
    return RawHit(
        start_sec=start_sec,
        end_sec=end_sec,
        category=category,
        text=text,
        position=position,
        confidence=confidence,
        reason=reason,
        note=note,
        source=source,
    )


def _pick(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clean_text(value: str) -> str:
    text = value.strip().strip("\"'“”‘’")
    text = text.replace("￥", "¥").replace("＄", "$")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _clip(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()
