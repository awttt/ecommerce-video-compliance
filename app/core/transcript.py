"""口播句子。时间是视频内的秒，不是识别任务内部的相对时间。"""

from __future__ import annotations

import json
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


def dump_sentences(sentences: list[SpeechSentence]) -> list[dict]:
    """转写成可落库的结构。词级时间要留下，文字判定重试还要用来收紧切点。"""
    payload: list[dict] = []
    for item in sentences:
        text = str(item.text or "")
        if not text.strip():
            continue
        words: list[dict] = []
        for word in item.words:
            word_text = str(word.text or "")
            if not word_text:
                continue
            try:
                words.append(
                    {
                        "text": word_text,
                        "start_sec": float(word.start_sec),
                        "end_sec": float(word.end_sec),
                    }
                )
            except (TypeError, ValueError):
                continue
        try:
            start_sec = float(item.start_sec)
            end_sec = float(item.end_sec)
        except (TypeError, ValueError):
            continue
        payload.append({"text": text, "start_sec": start_sec, "end_sec": end_sec, "words": words})
    return payload


def load_sentences(raw) -> list[SpeechSentence]:
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    sentences: list[SpeechSentence] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        words: list[SpeechWord] = []
        listed = item.get("words")
        if isinstance(listed, list):
            for word in listed:
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("text") or "")
                if not word_text:
                    continue
                try:
                    words.append(
                        SpeechWord(
                            text=word_text,
                            start_sec=float(word.get("start_sec") or 0),
                            end_sec=float(word.get("end_sec") or 0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        try:
            start_sec = float(item.get("start_sec") or 0)
            end_sec = float(item.get("end_sec") or 0)
        except (TypeError, ValueError):
            continue
        sentences.append(SpeechSentence(text=text, start_sec=start_sec, end_sec=end_sec, words=words))
    return sentences
