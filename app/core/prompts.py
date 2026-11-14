"""发给 glm-5.3-flash 的画面审核提示。只允许报告画面里看得见的内容。"""

from __future__ import annotations

from app.core.exaggeration import exaggeration_instructions
from app.core.timeutil import format_range
from app.schemas import DetectionRules, Frame

SYSTEM_PROMPT = """你是电商短视频画面合规审核员。你只能报告这一帧画面里用眼睛能直接读到的信息。
禁止根据商品外观、常识、上下文或相邻帧推测。看不清的内容不要补全，也不要脑补货币符号、单位或品牌名。
只报告三类，且画面中必须有对应的可见文字，或清晰可读的 Logo 文字：
1. 价格：货币符号、价格数字、划线价、到手价、券后价等价格文本。
2. 规格：容量、重量、尺寸、数量、包装规格、型号规格等文本，例如 500ml、XL、2件装。
3. 品牌：画面中可辨认的品牌名，或 Logo 上能读出的品牌文字。看不到品牌文字就不要报告品牌。
规则：
- 原文必须是画面中的原文，不要改写、不要翻译、不要加上画面里没有的符号。
- 位置用简短中文描述文字在画面中的位置。
- 清晰可读时，置信度填「确定」。有遮挡、模糊、反光但能辨认一部分时填「疑似」，原文只保留辨认出来的部分。
- 没有命中时返回空数组。不要为了完成任务而编造。
只输出 JSON 对象，不要 Markdown，不要解释。格式：
{"items":[{"类别":"价格","原文":"¥199","位置":"画面右下角价格贴纸","置信度":"确定","判定理由":"带货币符号且位于商品旁价格标签","备注":""}]}
类别只能是「价格」「规格」「品牌」。置信度只能是「确定」或「疑似」。"""


def user_prompt(frame: Frame, rules: DetectionRules | None = None) -> str:
    span = format_range(frame.start_sec, frame.end_sec)
    names = _category_names(rules, "画面")
    extra = (
        "自定义规则里点名的画面文字，只要原样出现也要列出。"
        if rules and any(item.source == "画面" for item in rules.extra_phrases)
        else ""
    )
    return (
        f"这是视频 {span} 的一帧画面。"
        f"请只根据这一张图里实际可见的文字和标识，列出{names}。"
        f"{extra}"
        '没有就返回 {"items":[]}。'
    )


SPEECH_SYSTEM_PROMPT = """你是电商短视频口播合规审核员。你只能根据给出的口播原文判断，不能补充说话人没说的内容。
禁止根据常识、商品外观或上下文推测价格、规格、品牌。口播里没出现的数字、单位、品牌名不要补上。
只报告三类，且必须能在某一句口播里找到连续原文：
1. 价格：明确说出的价格，例如「九十九元」「¥199」。
2. 规格：明确说出的容量、重量、尺寸、数量、包装规格，例如「500毫升」。
3. 品牌：明确说出的品牌名。没说出来就不要报告品牌。
规则：
- 原文必须是该句中连续出现的原话，可以是整句或其中连续一段。不要改写、不要翻译、不要把中文数字换成阿拉伯数字。
- 句子字段填写列表中的句子序号。
- 清晰说出时置信度填「确定」。含糊、只听到一部分时填「疑似」，原文只保留听到的部分。
- 没有命中时返回空数组。不要为了完成任务而编造。
只输出 JSON 对象，不要 Markdown，不要解释。格式：
{"items":[{"句子":1,"类别":"价格","原文":"这款只要九十九元","置信度":"确定","判定理由":"口播中明确提及价格","备注":""}]}
类别只能是「价格」「规格」「品牌」。置信度只能是「确定」或「疑似」。"""


def speech_user_prompt(lines: list[str], rules: DetectionRules | None = None) -> str:
    body = "\n".join(lines)
    names = _category_names(rules, "口播")
    extra = (
        "自定义规则里点名的口播文字，只要原话里连续出现也要列出。X 表示数字。\n"
        if rules and any(item.source == "口播" for item in rules.extra_phrases)
        else ""
    )
    return (
        f"下面是带序号和时间的口播句子。请只根据这些原话列出{names}。\n"
        f"{extra}"
        f"{body}\n"
        '没有就返回 {"items":[]}。'
    )


def visual_system_prompt(rules: DetectionRules | None = None) -> str:
    return SYSTEM_PROMPT + "\n" + rules_appendix(rules, "画面")


def speech_system_prompt(rules: DetectionRules | None = None) -> str:
    current = rules or DetectionRules()
    text = SPEECH_SYSTEM_PROMPT
    if "夸大宣传" in current.categories:
        text = text.replace("只报告三类，", "只报告下面几类，", 1)
        text = text.replace(
            "3. 品牌：明确说出的品牌名。没说出来就不要报告品牌。\n规则：",
            "3. 品牌：明确说出的品牌名。没说出来就不要报告品牌。\n" + exaggeration_instructions() + "\n规则：",
            1,
        )
        text = text.replace(
            "类别只能是「价格」「规格」「品牌」。",
            "类别只能是「价格」「规格」「品牌」「夸大宣传」。",
            1,
        )
    return text + "\n" + rules_appendix(current, "口播")


def rules_appendix(rules: DetectionRules | None = None, source: str = "画面") -> str:
    current = rules or DetectionRules()
    enabled = _category_names(current, source) or "（未启用预设类别）"
    lines = [
        "下面是本次任务的自定义规则。这些规则不能放宽前面的限制：仍然禁止推测，原文必须能在画面或口播里原样找到。",
        f"只报告这些预设类别：{enabled}。不在列表里的预设类别不要返回。",
    ]
    phrases = [item for item in current.extra_phrases if item.source == source]
    if phrases:
        lines.append(_phrase_block(phrases, source))
    if current.ignore_phrases:
        body = "、".join(f"「{item}」" for item in current.ignore_phrases)
        lines.append(f"下列文字不要报告：{body}。")
    if current.note:
        lines.append(f"用户补充说明：{current.note}")
    lines.append("如果补充说明和「禁止推测、原文必须原样出现」冲突，以禁止推测为准。没有命中就返回 {\"items\":[]}。")
    return "\n".join(lines)


def _phrase_block(phrases: list, source: str) -> str:
    grouped: dict[str, list[str]] = {}
    for item in phrases:
        grouped.setdefault(item.category, []).append(item.text)
    if source == "口播":
        lines = [
            "下列口播文字只要在某一句里连续出现，也要报告。词中的 X 表示数字，例如「X折」对应「5折」或「五折」，「X.X贵」对应「3.5贵」。报告时原文写实际说出来的那一段，不要写 X。"
        ]
    else:
        lines = ["下列文字只要在这一帧画面里看得见，也要报告。不要根据外观、包装形状或常识推测。"]
    shown = ("价格", "规格", "品牌", "夸大宣传") if source == "口播" else ("价格", "规格", "品牌")
    for category in shown:
        texts = grouped.get(category) or []
        if texts:
            lines.append(category + "：" + "、".join(f"「{text}」" for text in texts))
    return "\n".join(lines)


def _category_names(rules: DetectionRules | None, source: str = "口播") -> str:
    if rules is None or not rules.categories:
        names = ["价格", "规格", "品牌"]
    else:
        names = list(rules.categories)
    if source == "画面":
        names = [item for item in names if item != "夸大宣传"]
    return "、".join(names)
