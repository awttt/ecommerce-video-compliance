"""把相邻帧上的同一处原文收成一个时间区间。中间空帧不合并。"""

from __future__ import annotations

import re
from collections import Counter

from app.core.timeutil import format_range
from app.schemas import Finding, RawHit

# 只吸收抽帧时间戳的浮点误差。缺了一帧就不并到同一个区间。
MERGE_EPSILON = 0.08
_CATEGORY_ORDER = {"价格": 0, "规格": 1, "品牌": 2}


def norm_text(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def aggregate(hits: list[RawHit]) -> list[Finding]:
    groups: dict[tuple[str, str, str], list[RawHit]] = {}
    for hit in hits:
        groups.setdefault((hit.category, norm_text(hit.text), hit.source or "画面"), []).append(hit)

    findings: list[Finding] = []
    for grouped in groups.values():
        grouped.sort(key=lambda item: (item.start_sec, item.end_sec))
        current = [grouped[0]]
        for hit in grouped[1:]:
            if hit.start_sec <= current[-1].end_sec + MERGE_EPSILON:
                current.append(hit)
            else:
                findings.append(_merge(current))
                current = [hit]
        findings.append(_merge(current))

    findings.sort(
        key=lambda item: (item.start_sec, _CATEGORY_ORDER.get(item.category, 9), item.source, item.text)
    )
    return findings


def _merge(items: list[RawHit]) -> Finding:
    start = min(item.start_sec for item in items)
    end = max(item.end_sec for item in items)
    confidence = "确定" if all(item.confidence == "确定" for item in items) else "疑似"
    text = next((item.text for item in items if item.confidence == "确定"), items[0].text)
    position = Counter(item.position for item in items).most_common(1)[0][0]
    reason = _join(item.reason for item in items)
    note = _join(item.note for item in items if item.note)
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
