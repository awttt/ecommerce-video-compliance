"""任务、命中和设置的数据结构。命中导出字段使用中文名。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.asr_engine import configured_engine
from app.core.relay import configured_model
from app.core.rule_catalog import default_extra_phrases


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


# 识别失败的帧仍占一行，方便导出后人工看。它不是价格、规格或品牌命中。
MANUAL_REVIEW_CATEGORY = "解析失败"
MANUAL_REVIEW_NOTE = "解析失败，需要人工审核"


def is_manual_review(category: str) -> bool:
    return category == MANUAL_REVIEW_CATEGORY


_RULE_CATEGORIES = ("价格", "规格", "品牌", "夸大宣传")


class PhraseRule(BaseModel):
    text: str
    category: Literal["价格", "规格", "品牌", "夸大宣传"]
    source: Literal["口播", "画面"] = "口播"

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", "", value.strip())
        if not cleaned or len(cleaned) > 40:
            raise ValueError("规则文字需要在 1 到 40 个字之间")
        return cleaned

    @field_validator("source", mode="before")
    @classmethod
    def _source(cls, value: object) -> object:
        if value is None or value == "":
            return "口播"
        return value


class DetectionRules(BaseModel):
    """用户自定义的审核规则。不能放宽「禁止推测、原文必须原样出现」。"""

    categories: list[Literal["价格", "规格", "品牌", "夸大宣传"]] = Field(
        default_factory=lambda: ["价格", "规格", "品牌", "夸大宣传"]
    )
    extra_phrases: list[PhraseRule] = Field(default_factory=list, max_length=300)
    ignore_phrases: list[str] = Field(default_factory=list, max_length=30)
    note: str = ""

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: list[str]) -> list[str]:
        unique: list[str] = []
        for item in value:
            if item not in _RULE_CATEGORIES:
                raise ValueError("类别只能是价格、规格、品牌、夸大宣传")
            if item not in unique:
                unique.append(item)
        return unique

    @field_validator("extra_phrases")
    @classmethod
    def _extra(cls, value: list[PhraseRule]) -> list[PhraseRule]:
        seen: set[tuple[str, str]] = set()
        unique: list[PhraseRule] = []
        for item in value:
            key = (item.source, item.category, item.text)
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


MAX_CONCURRENCY = 20
DEFAULT_CONCURRENCY = 10


def clamp_concurrency(value: object, default: int = DEFAULT_CONCURRENCY) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(MAX_CONCURRENCY, max(1, number))


class RunConfig(BaseModel):
    fps: float = 1
    concurrency: int = DEFAULT_CONCURRENCY
    max_width: int = 1280
    jpeg_q: int = 5
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "glm-5.3-flash"
    detail: str = "original"
    thinking: bool = False
    motion_enabled: bool = False
    timeout_sec: float = 120
    mock: bool = False
    fault_bad_json: bool = False
    fault_tencent: bool = False
    fault_partial_frames: bool = False
    asr_enabled: bool = True
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_region: str = "ap-shanghai"
    asr_engine: str = "16k_zh"
    rules: DetectionRules = Field(default_factory=lambda: DetectionRules())


class CutSpan(BaseModel):
    start_sec: float = Field(ge=0)
    end_sec: float = Field(ge=0)


class ModelStat(BaseModel):
    """本机请求计数。logical 按逻辑单元去重；timeout 含在 failed 里。"""

    logical: int = 0
    runs: int = 0
    http_attempts: int = 0
    ok: int = 0
    failed: int = 0
    timeout: int = 0


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
    speech_retry: Literal["", "transcribe", "judge"] = ""
    mock: bool = False
    output_ready: bool = False
    preview_status: str = ""
    preview_hint: str = ""
    cuts: list[CutSpan] = Field(default_factory=list)
    edit_rendered: bool = False
    has_edit_plan: bool = False
    created_at: str
    updated_at: str
    finished_at: str | None = None
    started_at: str | None = None
    paused_total_sec: float = 0
    hold_sec: float = 0
    paused_at: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    model_stats: dict[str, ModelStat] = Field(default_factory=dict)


class SettingUpdate(BaseModel):
    api_key: str | None = None
    clear_api_key: bool = False
    fps: float | None = Field(default=None, ge=0.5, le=2)
    concurrency: int | None = None
    max_width: int | None = Field(default=None, ge=480, le=1920)
    jpeg_q: int | None = Field(default=None, ge=2, le=15)
    detail: Literal["original", "high", "low"] | None = None
    thinking: bool | None = None
    motion_enabled: bool | None = None
    timeout_sec: float | None = Field(default=None, ge=15, le=180)
    mock_mode: bool | None = None
    fault_bad_json: bool | None = None
    fault_tencent: bool | None = None
    fault_partial_frames: bool | None = None
    mcp_enabled: bool | None = None
    model: str | None = None
    asr_enabled: bool | None = None
    tencent_secret_id: str | None = None
    tencent_secret_key: str | None = None
    clear_tencent_secret: bool = False
    tencent_region: str | None = None
    asr_engine: str | None = None
    rules: DetectionRules | None = None

    @field_validator("concurrency")
    @classmethod
    def _concurrency(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 1 <= value <= MAX_CONCURRENCY:
            raise ValueError(f"并发数需要在 1 到 {MAX_CONCURRENCY} 之间")
        return value

    @field_validator("model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return configured_model(value)

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
        cleaned = configured_engine(value)
        if not re.fullmatch(r"[0-9A-Za-z_.]{2,40}", cleaned):
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
    motion_enabled: bool = False
    timeout_sec: float
    mock_mode: bool
    fault_bad_json: bool = False
    fault_tencent: bool = False
    fault_partial_frames: bool = False
    mcp_enabled: bool = False
    asr_enabled: bool
    has_tencent_secret: bool
    tencent_secret_id_masked: str
    tencent_secret_key_masked: str
    tencent_secret_source: str
    tencent_region: str
    asr_engine: str
    rules: DetectionRules
    account_logged_in: bool = False
    account_email: str = ""
    account_balance: float | None = None
    account_frozen_balance: float | None = None
    account_today_cost: float | None = None
    account_total_cost: float | None = None


class AccountCredentials(BaseModel):
    email: str = Field(max_length=120)
    password: str = Field(max_length=200)
    verify_code: str = Field(default="", max_length=20)


class AccountCodeRequest(BaseModel):
    email: str = Field(max_length=120)


class AccountTotpRequest(BaseModel):
    totp_code: str = Field(max_length=12)


class AccountOptions(BaseModel):
    reachable: bool = True
    registration_enabled: bool = True
    email_verify_enabled: bool = False
    captcha: str = "none"
    message: str = ""


class AccountAction(BaseModel):
    settings: SettingPublic
    requires_2fa: bool = False
    email_masked: str = ""
    message: str = ""


class RechargeMethod(BaseModel):
    payment_type: str
    display_name: str
    currency: str = ""
    fee_rate: float = 0
    daily_limit: float = 0
    single_min: float = 0
    single_max: float = 0


class RechargeBonusTier(BaseModel):
    min_amount: float
    bonus_percent: float


class RechargeInfo(BaseModel):
    balance_disabled: bool = False
    global_min: float = 0
    global_max: float = 0
    recharge_fee_rate: float = 0
    balance_recharge_multiplier: float = 1
    bonus_mode: str = "bonus"
    bonus_notice: str = ""
    bonus_tiers: list[RechargeBonusTier] = Field(default_factory=list)
    help_text: str = ""
    methods: list[RechargeMethod] = Field(default_factory=list)
    message: str = ""


class RechargeOrderRequest(BaseModel):
    amount: float = Field(gt=0, le=1_000_000)
    payment_type: str = Field(min_length=1, max_length=40)


class RechargeOrder(BaseModel):
    order_id: int
    amount: float = 0
    pay_amount: float = 0
    fee_rate: float = 0
    bonus_amount: float = 0
    status: str = ""
    result_type: str = ""
    payment_type: str = ""
    pay_url: str = ""
    qr_code: str = ""
    currency: str = ""
    expires_at: str = ""
    created_at: str = ""
    message: str = ""
    settings: SettingPublic | None = None


class RechargeOrders(BaseModel):
    orders: list[RechargeOrder] = Field(default_factory=list)


class CutRequest(BaseModel):
    padding_sec: float = Field(default=0.2, ge=0, le=2)


class CutsUpdate(BaseModel):
    cuts: list[CutSpan] = Field(default_factory=list, max_length=200)


FIXED_TENCENT_REGION = "ap-shanghai"
TEST_ACCOUNT_EMAIL = "350944364@qq.com"
FAULT_FIELDS = ("fault_bad_json", "fault_tencent", "fault_partial_frames")

DEFAULT_SETTINGS: dict[str, object] = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "glm-5.3-flash",
    "fps": 1.0,
    "concurrency": DEFAULT_CONCURRENCY,
    "max_width": 1280,
    "jpeg_q": 5,
    "detail": "original",
    "thinking": False,
    "motion_enabled": False,
    "timeout_sec": 120.0,
    "mock_mode": False,
    "fault_bad_json": False,
    "fault_tencent": False,
    "fault_partial_frames": False,
    "mcp_enabled": False,
    "asr_enabled": True,
    "tencent_secret_id": "",
    "tencent_secret_key": "",
    "tencent_region": FIXED_TENCENT_REGION,
    "asr_engine": "16k_zh",
    "rules": {
        "categories": ["价格", "规格", "品牌", "夸大宣传"],
        "extra_phrases": default_extra_phrases(),
        "ignore_phrases": [],
        "note": "",
    },
}
