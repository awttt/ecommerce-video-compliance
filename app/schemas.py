"""任务、命中和设置的数据结构。命中导出字段使用中文名。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Frame(BaseModel):
    index: int
    start_sec: float
    end_sec: float
    path: str


class RawHit(BaseModel):
    start_sec: float
    end_sec: float
    category: str
    text: str
    position: str
    confidence: str
    reason: str
    note: str = ""
    source: str = "画面"
    sentence_start: float | None = None
    sentence_end: float | None = None
    next_start: float | None = None


class Finding(BaseModel):
    """画面或口播命中。API 与导出都带中文别名；定位用 start_sec / end_sec。"""

    model_config = ConfigDict(populate_by_name=True)

    time_range: str = Field(alias="时间点")
    category: str = Field(alias="类别")
    source: str = Field(default="画面", alias="来源")
    text: str = Field(alias="原文")
    position: str = Field(alias="位置")
    confidence: str = Field(alias="置信度")
    reason: str = Field(alias="判定理由")
    note: str = Field(default="", alias="备注")
    start_sec: float = 0
    end_sec: float = 0
    sentence_start: float | None = None
    sentence_end: float | None = None
    next_start: float | None = None

    def export_dict(self) -> dict[str, str]:
        return {
            "时间点": self.time_range,
            "类别": self.category,
            "来源": self.source,
            "原文": self.text,
            "位置": self.position,
            "置信度": self.confidence,
            "判定理由": self.reason,
            "备注": self.note,
        }


_RULE_CATEGORIES = ("价格", "规格", "品牌")


class PhraseRule(BaseModel):
    text: str
    category: Literal["价格", "规格", "品牌"]

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", "", value.strip())
        if not cleaned or len(cleaned) > 40:
            raise ValueError("规则文字需要在 1 到 40 个字之间")
        return cleaned


class DetectionRules(BaseModel):
    """用户自定义的审核规则。不能放宽「禁止推测、原文必须原样出现」。"""

    categories: list[Literal["价格", "规格", "品牌"]] = Field(
        default_factory=lambda: ["价格", "规格", "品牌"]
    )
    extra_phrases: list[PhraseRule] = Field(default_factory=list, max_length=30)
    ignore_phrases: list[str] = Field(default_factory=list, max_length=30)
    note: str = ""

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: list[str]) -> list[str]:
        unique: list[str] = []
        for item in value:
            if item not in _RULE_CATEGORIES:
                raise ValueError("类别只能是价格、规格、品牌")
            if item not in unique:
                unique.append(item)
        return unique

    @field_validator("extra_phrases")
    @classmethod
    def _extra(cls, value: list[PhraseRule]) -> list[PhraseRule]:
        seen: set[tuple[str, str]] = set()
        unique: list[PhraseRule] = []
        for item in value:
            key = (item.category, item.text)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @field_validator("ignore_phrases")
    @classmethod
    def _ignore(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = re.sub(r"\s+", "", str(item).strip())
            if not text:
                continue
            if len(text) > 40:
                raise ValueError("忽略词需要在 1 到 40 个字之间")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        text = value.strip()
        if len(text) > 400:
            raise ValueError("补充说明不能超过 400 字")
        return text

    @model_validator(mode="after")
    def _has_rule(self) -> DetectionRules:
        if not self.categories and not self.extra_phrases:
            raise ValueError("至少启用一个类别，或添加一条额外命中词")
        return self


class RunConfig(BaseModel):
    fps: float = 1
    concurrency: int = 4
    max_width: int = 1280
    jpeg_q: int = 5
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    detail: str = "original"
    thinking: bool = False
    timeout_sec: float = 60
    mock: bool = False
    asr_enabled: bool = True
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_region: str = "ap-shanghai"
    asr_engine: str = "16k_zh"
    rules: DetectionRules = Field(default_factory=lambda: DetectionRules())


class CutSpan(BaseModel):
    start_sec: float = Field(ge=0)
    end_sec: float = Field(ge=0)


class JobPublic(BaseModel):
    id: str
    filename: str
    status: str
    stage: str
    message: str
    progress: float
    fps: float
    concurrency: int
    duration_sec: float | None = None
    frames_estimated: int = 0
    frames_extracted: int = 0
    frames_recognized: int = 0
    failed_frames: int = 0
    hit_count: int = 0
    error: str | None = None
    speech_stage: str = ""
    speech_message: str = ""
    asr_error: str | None = None
    mock: bool = False
    output_ready: bool = False
    preview_hint: str = ""
    cuts: list[CutSpan] = Field(default_factory=list)
    edit_rendered: bool = False
    has_edit_plan: bool = False
    created_at: str
    updated_at: str
    finished_at: str | None = None
    findings: list[Finding] = Field(default_factory=list)


class SettingUpdate(BaseModel):
    api_key: str | None = None
    clear_api_key: bool = False
    base_url: str | None = None
    model: str | None = None
    fps: float | None = Field(default=None, ge=0.5, le=2)
    concurrency: int | None = Field(default=None, ge=1, le=12)
    max_width: int | None = Field(default=None, ge=480, le=1920)
    jpeg_q: int | None = Field(default=None, ge=2, le=15)
    detail: Literal["original", "high", "low"] | None = None
    thinking: bool | None = None
    timeout_sec: float | None = Field(default=None, ge=15, le=180)
    mock_mode: bool | None = None
    asr_enabled: bool | None = None
    tencent_secret_id: str | None = None
    tencent_secret_key: str | None = None
    clear_tencent_secret: bool = False
    tencent_region: str | None = None
    asr_engine: str | None = None
    rules: DetectionRules | None = None

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("接口地址必须以 http:// 或 https:// 开头")
        return cleaned

    @field_validator("model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 80:
            raise ValueError("模型名无效")
        return cleaned

    @field_validator("api_key")
    @classmethod
    def _api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("tencent_secret_id", "tencent_secret_key")
    @classmethod
    def _tencent_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if any(char.isspace() for char in cleaned) or len(cleaned) > 128:
            raise ValueError("腾讯云密钥无效")
        return cleaned

    @field_validator("tencent_region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not re.fullmatch(r"[a-z0-9-]{3,32}", cleaned):
            raise ValueError("腾讯云地域格式无效")
        return cleaned

    @field_validator("asr_engine")
    @classmethod
    def _engine(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not re.fullmatch(r"[0-9A-Za-z_]{2,40}", cleaned):
            raise ValueError("识别引擎名称无效")
        return cleaned


class SettingPublic(BaseModel):
    has_api_key: bool
    api_key_masked: str
    api_key_source: str
    base_url: str
    model: str
    fps: float
    concurrency: int
    max_width: int
    jpeg_q: int
    detail: str
    thinking: bool
    timeout_sec: float
    mock_mode: bool
    asr_enabled: bool
    has_tencent_secret: bool
    tencent_secret_id_masked: str
    tencent_secret_key_masked: str
    tencent_secret_source: str
    tencent_region: str
    asr_engine: str
    rules: DetectionRules


class CutRequest(BaseModel):
    padding_sec: float = Field(default=0.2, ge=0, le=2)


class CutsUpdate(BaseModel):
    cuts: list[CutSpan] = Field(default_factory=list, max_length=200)


DEFAULT_SETTINGS: dict[str, object] = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "fps": 1.0,
    "concurrency": 4,
    "max_width": 1280,
    "jpeg_q": 5,
    "detail": "original",
    "thinking": False,
    "timeout_sec": 60.0,
    "mock_mode": False,
    "asr_enabled": True,
    "tencent_secret_id": "",
    "tencent_secret_key": "",
    "tencent_region": "ap-shanghai",
    "asr_engine": "16k_zh",
    "rules": {
        "categories": ["价格", "规格", "品牌"],
        "extra_phrases": [],
        "ignore_phrases": [],
        "note": "",
    },
}
