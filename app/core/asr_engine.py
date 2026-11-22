"""录音文件识别引擎。只用大模型 2.0。"""

ASR_ENGINE = "16k_zh_en_2.0"


def configured_engine(value: object) -> str:
    """已不再提供通用引擎。不论设置里原来写的是什么，都用大模型 2.0。"""
    del value
    return ASR_ENGINE
