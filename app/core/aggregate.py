"""把画面上一直挂着的同一处文字收成一个时间区间。"""

from __future__ import annotations

import re
from collections import Counter

from app.core.timeutil import format_range
from app.schemas import MANUAL_REVIEW_NOTE, Finding, RawHit, is_manual_review

# 口播只吸收时间戳的浮点误差。
MERGE_EPSILON = 0.08
# 画面抽帧最慢 0.5 FPS，空了大约一帧仍视为同一处文字还在。再久就断开。
PICTURE_GAP = 2.0
_CATEGORY_ORDER = {"价格": 0, "规格": 1, "品牌": 2}
_PRICE_HEAD = re.compile(r"^(?P<mark>[¥￥$])?(?P<number>\d+(?:\.\d+)?)(?P<unit>元|块钱|块)?")
_COLON_DECIMAL = re.compile(r"\d[:：]\d")


def norm_text(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _unify_decimal(text: str) -> str:
    """把「5:55元」这种把小数点看成冒号的读法收成「5.55元」。"""
    return _COLON_DECIMAL.sub(lambda match: match.group(0).replace(":", ".").replace("：", "."), text.replace("．", "."))


def price_group_key(text: str) -> str:
    """同一处价格的不同读法用同一档。「5.55元」和「5.55元3把」是一档，「4.9元」不是。"""
    compact = _unify_decimal(norm_text(text))
    match = _PRICE_HEAD.match(compact)
    if match is None or (not match.group("mark") and not match.group("unit")):
        return compact
    return f"{match.group('mark') or ''}{match.group('number')}{match.group('unit') or ''}"


def _group_key(hit: RawHit) -> tuple[str, str, str]:
    source = hit.source or "画面"
    if is_manual_review(hit.category):
        return hit.category, f"{hit.start_sec:.3f}-{hit.end_sec:.3f}", source
    if source == "画面" and hit.category == "价格":
        text_key = price_group_key(hit.text)
    else:
        text_key = norm_text(hit.text)
    return hit.category, text_key, source


def _gap_limit(source: str) -> float:
    if source == "画面":
        return PICTURE_GAP
    return MERGE_EPSILON


def aggregate(hits: list[RawHit]) -> list[Finding]:
    groups: dict[tuple[str, str, str], list[RawHit]] = {}
    for hit in hits:
        groups.setdefault(_group_key(hit), []).append(hit)

    findings: list[Finding] = []
    for grouped in groups.values():
        grouped.sort(key=lambda item: (item.start_sec, item.end_sec))
        limit = _gap_limit(grouped[0].source or "画面")
        current = [grouped[0]]
        for hit in grouped[1:]:
            if hit.start_sec <= current[-1].end_sec + limit:
                current.append(hit)
            else:
                findings.append(_merge(current))
                current = [hit]
        findings.append(_merge(current))

    findings.sort(
        key=lambda item: (item.start_sec, _CATEGORY_ORDER.get(item.category, 9), item.source, item.text)
    )
    return findings


def refold_findings(findings: list[Finding]) -> list[Finding]:
    """已保存的旧结果再收一次。已经收干净的列表再收一次，内容不变。"""
    if len(findings) < 2:
        return list(findings)
    hits = [
        RawHit(
            start_sec=item.start_sec,
            end_sec=item.end_sec,
            category=item.category,
            text=item.text,
            position=item.position,
            confidence=item.confidence,
            reason=item.reason,
            note=item.note,
            source=item.source,
            sentence_start=item.sentence_start,
            sentence_end=item.sentence_end,
            next_start=item.next_start,
        )
        for item in findings
    ]
    return aggregate(hits)


def _choose_text(items: list[RawHit]) -> str:
    pool = [item for item in items if item.confidence == "确定"] or list(items)
    counts: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for item in pool:
        key = _unify_decimal(norm_text(item.text))
        counts[key] += 1
        previous = sample.get(key)
        if previous is None or (_COLON_DECIMAL.search(previous) and not _COLON_DECIMAL.search(item.text)):
            sample[key] = item.text
    best = max(sample, key=lambda key: (1 if counts[key] >= 2 else 0, len(key), counts[key]))
    return sample[best]


def _merge(items: list[RawHit]) -> Finding:
    if is_manual_review(items[0].category):
        item = items[0]
        start = item.start_sec
        end = item.end_sec
        return Finding(
            time_range=format_range(start, end),
            category=item.category,
            source=item.source or "画面",
            text=item.text,
            position=item.position,
            confidence=item.confidence,
            reason=item.reason,
            note=item.note or MANUAL_REVIEW_NOTE,
            start_sec=round(start, 3),
            end_sec=round(end, 3),
        )
    start = min(item.start_sec for item in items)
    end = max(item.end_sec for item in items)
    confidence = "确定" if all(item.confidence == "确定" for item in items) else "疑似"
    text = _choose_text(items)
    position = Counter(item.position for item in items).most_common(1)[0][0]
    reason = _join(item.reason for item in items)
    note = _join(item.note for item in items if item.note)
    if len({_unify_decimal(norm_text(item.text)) for item in items}) > 1:
        extra = "同一处文字有多种读法，已按最完整的一条保留"
        note = f"{note}；{extra}" if note else extra
    if len({item.position for item in items}) > 1:
        extra = "相邻帧位置描述不完全一致"
        note = f"{note}；{extra}" if note else extra
    return Finding(
        time_range=format_range(start, end),
        category=items[0].category,
        source=items[0].source or "画面",
        text=text,
        position=position,
        confidence=confidence,
        reason=reason,
        note=note,
        start_sec=round(start, 3),
        end_sec=round(end, 3),
        sentence_start=_optional_min(item.sentence_start for item in items),
        sentence_end=_optional_max(item.sentence_end for item in items),
        next_start=_next_after(items, end),
    )


def _optional_min(values) -> float | None:
    nums = [value for value in values if value is not None]
    if not nums:
        return None
    return round(min(nums), 3)


def _optional_max(values) -> float | None:
    nums = [value for value in values if value is not None]
    if not nums:
        return None
    return round(max(nums), 3)


def _next_after(items: list[RawHit], end: float) -> float | None:
    candidates = [
        item.next_start
        for item in items
        if item.next_start is not None and item.next_start + 1e-3 >= end
    ]
    if not candidates:
        return None
    return round(min(candidates), 3)


def _join(values, limit: int = 2) -> str:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
        if len(unique) >= limit:
            break
    return "；".join(unique)
