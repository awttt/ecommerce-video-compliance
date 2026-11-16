"""录音文件识别引擎。免费资源包只抵扣通用引擎 16k_zh，大模型 2.0 另计。"""

ASR_ENGINE = "16k_zh"


def configured_engine(value: object) -> str:
    """空值用免费的通用引擎。已经选过的引擎保持原样。"""
    text = str(value or "").strip()
    return text or ASR_ENGINE
