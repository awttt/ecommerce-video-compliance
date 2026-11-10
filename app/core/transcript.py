"""口播句子。时间是视频内的秒，不是识别任务内部的相对时间。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SpeechWord:
    text: str
    start_sec: float
    end_sec: float


@dataclass
class SpeechSentence:
    text: str
    start_sec: float
    end_sec: float
    words: list[SpeechWord] = field(default_factory=list)
