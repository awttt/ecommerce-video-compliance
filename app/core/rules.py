"""把用户自定义规则用到已经解析好的命中上。

画面命中只做过滤：模型必须自己看见文字。口播可以按原句做匹配，
额外命中词不在口播里连续出现就不会被加上。词里的 X 表示数字。
"""

from __future__ import annotations

import re

from app.core.parsing import locate_quote, tighten_span
from app.core.transcript import SpeechSentence
from app.schemas import DetectionRules, PhraseRule, RawHit

_CATEGORIES = {"价格", "规格", "品牌", "夸大宣传"}
_PLAIN_NUMBER = r"(?:\d+|[零〇一二三四五六七八九十百千万两]+)"
_NUMBER = rf"(?:{_PLAIN_NUMBER}(?:(?:\.|点){_PLAIN_NUMBER})?)"


def apply_rules(
    hits: list[RawHit],
    rules: DetectionRules,
    sentences: list[SpeechSentence] | None = None,
) -> list[RawHit]:
    kept = [hit for hit in hits if _keep(hit, rules)]
    if not sentences:
        return kept
    kept.extend(_extra_speech(sentences, rules, kept))
    return kept


def _keep(hit: RawHit, rules: DetectionRules) -> bool:
    if _ignored(hit.text, rules):
        return False
    if hit.category in rules.categories:
        return True
    source = hit.source if hit.source in {"口播", "画面"} else "画面"
    return any(
        item.source == source and hit.category == item.category and _covers(hit.text, item.text)
        for item in rules.extra_phrases
    )


def _ignored(text: str, rules: DetectionRules) -> bool:
    hay = _norm(text)
    if not hay:
        return False
    for phrase in rules.ignore_phrases:
        needle = _norm(phrase)
        if needle and (hay == needle or hay in needle):
            return True
    return False


def _extra_speech(
    sentences: list[SpeechSentence],
    rules: DetectionRules,
    existing: list[RawHit],
) -> list[RawHit]:
    found: list[RawHit] = []
    seen = {(hit.category, _norm(hit.text), round(hit.start_sec, 2)) for hit in existing}
    ordered = list(sentences)
    for index, sentence in enumerate(ordered):
        next_start = _following(ordered, index)
        for phrase in rules.extra_phrases:
            for hit in _phrase_hits(sentence, phrase, next_start, rules):
                key = (hit.category, _norm(hit.text), round(hit.start_sec, 2))
                if key in seen:
                    continue
                seen.add(key)
                found.append(hit)
    return found


def _phrase_hits(
    sentence: SpeechSentence,
    phrase: PhraseRule,
    next_start: float | None,
    rules: DetectionRules,
) -> list[RawHit]:
    if phrase.source != "口播" or phrase.category not in _CATEGORIES:
        return []
    found: list[RawHit] = []
    for located in locate_rule_quotes(sentence.text, phrase.text):
        if _ignored(located, rules):
            continue
        start, end = tighten_span(sentence, located)
        found.append(
            RawHit(
                start_sec=start,
                end_sec=end,
                category=phrase.category,
                text=located,
                position="口播内容",
                confidence="确定",
                reason=f"命中自定义规则「{phrase.text}」",
                note="",
                source="口播",
                sentence_start=sentence.start_sec,
                sentence_end=sentence.end_sec,
                next_start=next_start,
            )
        )
    return found


def locate_rule_quotes(sentence: str, phrase: str) -> list[str]:
    """返回原句里命中的连续片段。没有 X 时必须原样出现；X 表示一段数字。"""
    phrase = re.sub(r"\s+", "", phrase.strip())
    if not phrase or not sentence:
        return []
    pattern = _wildcard_pattern(phrase)
    if pattern is None:
        located = locate_quote(sentence, phrase)
        if located:
            return [located]
        if re.search(r"[A-Za-z]", phrase):
            found = _search_all(sentence, re.escape(phrase))
            return found[:1]
        return []
    return _search_all(sentence, pattern)


def _wildcard_pattern(phrase: str) -> str | None:
    if "X" not in phrase and "x" not in phrase:
        return None
    chunks: list[str] = []
    index = 0
    while index < len(phrase):
        char = phrase[index]
        if char in "Xx":
            if index + 2 < len(phrase) and phrase[index + 1] == "." and phrase[index + 2] in "Xx":
                chunks.append(_PLAIN_NUMBER)
                chunks.append(r"(?:\.|点)")
                chunks.append(_PLAIN_NUMBER)
                index += 3
                continue
            chunks.append(_NUMBER)
            index += 1
            continue
        end = index + 1
        while end < len(phrase) and phrase[end] not in "Xx":
            end += 1
        chunks.append(re.escape(phrase[index:end]))
        index = end
    if not chunks:
        return None
    return "".join(chunks)


def _search_all(sentence: str, pattern: str) -> list[str]:
    hay, indexes = _compact(sentence)
    if not hay:
        return []
    found: list[str] = []
    for match in re.finditer(pattern, hay, flags=re.IGNORECASE):
        if match.end() <= match.start():
            continue
        start = indexes[match.start()]
        end = indexes[match.end() - 1] + 1
        found.append(sentence[start:end])
    return found


def _compact(sentence: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    indexes: list[int] = []
    for index, char in enumerate(sentence):
        if char.isspace():
            continue
        chars.append(char)
        indexes.append(index)
    return "".join(chars), indexes


def _following(ordered: list[SpeechSentence], index: int) -> float | None:
    if index + 1 >= len(ordered):
        return None
    nxt = ordered[index + 1].start_sec
    if nxt + 1e-3 < ordered[index].end_sec:
        return None
    return nxt


def _covers(hit_text: str, phrase: str) -> bool:
    return any(_norm(quote) == _norm(hit_text) for quote in locate_rule_quotes(hit_text, phrase))


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()
