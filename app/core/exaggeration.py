"""口播夸大宣传的字面边界。

只认转写原话里连续出现的绝对化用语和疗效承诺，不判断这句话是不是真的。
「最后」「最近」这类顺序或时间，以及「最大号」这类尺码，不算。
"""

from __future__ import annotations

import re

# 越长的词放前面，截取原文时优先用更完整的说法。
TERMS: tuple[str, ...] = (
    "百分之百",
    "百分百",
    "100%",
    "100％",
    "全国第一",
    "全网第一",
    "世界第一",
    "销量第一",
    "排名第一",
    "行业第一",
    "品质第一",
    "第一名",
    "独一无二",
    "仅此一家",
    "绝无仅有",
    "史无前例",
    "最先进",
    "最便宜",
    "最划算",
    "最安全",
    "最高级",
    "绝对有效",
    "药到病除",
    "彻底治愈",
    "永不复发",
    "立竿见影",
    "无副作用",
    "一用就灵",
    "无效退款",
    "最好",
    "最佳",
    "最优",
    "最高",
    "最低",
    "最强",
    "最大",
    "最小",
    "最新",
    "顶级",
    "极品",
    "万能",
    "完美",
    "永久",
    "国家级",
    "世界级",
    "根治",
    "包治",
    "治愈",
    "绝对",
    "no.1",
)

# 从待匹配文字里拿掉，避免「最」和「治愈」误伤。
_SKIP: tuple[str, ...] = (
    "最开始",
    "最后",
    "最近",
    "最终",
    "治愈系",
    "最大号",
    "最小号",
    "最大码",
    "最小码",
)

_PREFIXES: tuple[str, ...] = ("全网", "全国", "世界", "史上", "行业", "销量", "排名", "品质")


def exaggeration_instructions() -> str:
    """写进口播提示词。和 quote_is_exaggeration 用同一份说法。"""
    shown = [item for item in TERMS if item not in {"100％", "no.1"}]
    words = "、".join(f"「{item}」" for item in shown)
    return (
        "4. 夸大宣传：只看这句转写里有没有越界说法。不判断真假，也不根据常识推测效果。\n"
        "算夸大宣传：同一句里连续出现下列说法。原文只写这一小段；前面紧挨着「全网」「全国」「世界」「史上」「行业」「销量」时，把这几个字一并写入原文。\n"
        f"{words}。100% 和 NO.1 同样算。\n"
        "不算夸大宣传：\n"
        "- 「最后」「最近」「最终」「最开始」只是在说顺序或时间。\n"
        "- 「最大号」「最小号」「最大码」「最小码」只是在说尺码。「治愈系」只是在说风格。\n"
        "- 「好用」「不错」「便宜」「推荐」「实惠」「喜欢」这类评价，原话里没有上面的说法。\n"
        "- 只是价格、规格、品牌，没有上面的说法。那些交给原来的三类。\n"
        "判定理由写「口播中出现夸大宣传用语」，不要写推测。"
    )


def quote_is_exaggeration(text: str) -> bool:
    hay = _norm(text)
    if not hay:
        return False
    for word in sorted(_SKIP, key=len, reverse=True):
        hay = hay.replace(_norm(word), "")
    return any(_norm(term) in hay for term in TERMS)


def clip_exaggeration(quote: str) -> str | None:
    """把模型返回的整句收成越界的那一小段。对不上边界就返回空。"""
    if not quote_is_exaggeration(quote):
        return None
    hay = quote.casefold().replace("％", "%")
    best: tuple[int, int] | None = None
    for term in sorted(TERMS, key=len, reverse=True):
        needle = term.casefold().replace("％", "%")
        start = hay.find(needle)
        if start < 0:
            continue
        end = start + len(needle)
        for prefix in _PREFIXES:
            plen = len(prefix)
            if start >= plen and quote[start - plen : start] == prefix:
                start -= plen
                break
        if best is None or end - start > best[1] - best[0]:
            best = (start, end)
    if best is None:
        return None
    return quote[best[0] : best[1]]


def _norm(text: str) -> str:
    text = text.replace("％", "%").casefold()
    return re.sub(r"\s+", "", text)
