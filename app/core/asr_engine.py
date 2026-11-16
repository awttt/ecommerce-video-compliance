"""录音文件识别引擎。大模型 2.0 才走预付费套餐，通用引擎 16k_zh 不走。"""

ASR_ENGINE = "16k_zh_en_2.0"


def configured_engine(value: object) -> str:
    """空值和旧的通用引擎都改成大模型 2.0。"""
    text = str(value or "").strip()
    if text in {"", "16k_zh"}:
        return ASR_ENGINE
    return text
