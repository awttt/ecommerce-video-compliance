"""把用户自定义规则用到已经解析好的命中上。

画面命中只做过滤：模型必须自己看见文字。口播可以按原句做精确匹配，
额外命中词不在口播里连续出现就不会被加上。
"""

from __future__ import annotations

import re

from app.core.parsing import locate_quote, tighten_span
from app.core.transcript import SpeechSentence
from app.schemas import DetectionRules, PhraseRule, RawHit

_CATEGORIES = {"价格", "规格", "品牌"}


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
    return any(
        hit.category == item.category and _same(hit.text, item.text) for item in rules.extra_phrases
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
            hit = _phrase_hit(sentence, phrase, next_start, rules)
            if hit is None:
                continue
            key = (hit.category, _norm(hit.text), round(hit.start_sec, 2))
            if key in seen:
                continue
            seen.add(key)
            found.append(hit)
    return found


def _phrase_hit(
    sentence: SpeechSentence,
    phrase: PhraseRule,
    next_start: float | None,
    rules: DetectionRules,
) -> RawHit | None:
    if phrase.category not in _CATEGORIES:
        return None
    located = locate_quote(sentence.text, phrase.text)
    if not located or _ignored(located, rules):
        return None
    start, end = tighten_span(sentence, located)
    return RawHit(
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


def _following(ordered: list[SpeechSentence], index: int) -> float | None:
    if index + 1 >= len(ordered):
        return None
    nxt = ordered[index + 1].start_sec
    if nxt + 1e-3 < ordered[index].end_sec:
        return None
    return nxt


def _same(left: str, right: str) -> bool:
    return _norm(left) == _norm(right)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()
