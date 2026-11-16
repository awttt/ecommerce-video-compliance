"""腾讯云录音文件识别。本地音频用 SourceType=1 上传，超过 5MB 的由调用方切段。

签名使用 TC3-HMAC-SHA256，不依赖腾讯云 SDK。结果优先读 ResultDetail 的毫秒时间戳。
临时凭证在到期前约 5 分钟更换。网络、限流和鉴权失败会退避重试。密钥只留在内存里。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.asr_engine import ASR_ENGINE, configured_engine
from app.core.pipeline import JobCancelled, wait_while_paused
from app.core.transcript import SpeechSentence, SpeechWord
from app.logging_setup import redact

log = logging.getLogger(__name__)

ASR_HOST = "asr.tencentcloudapi.com"
ASR_SERVICE = "asr"
ASR_VERSION = "2019-06-14"
# 接口限制是 5MB（含）。留出余量，避免边界被拒。
MAX_AUDIO_BYTES = 4_500_000
_BRACKET = re.compile(r"\[(\d+):(\d+(?:\.\d+)?),(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")


# 首次请求之外再试这么多次。退避大约是 1s、2s、4s、8s、16s，封顶 30s。
ASR_MAX_RETRIES = 5
ASR_BACKOFF_BASE_SEC = 1.0
ASR_BACKOFF_CAP_SEC = 30.0
# 到期前这段时间就换新凭证，避免轮询到一半签名失效。
CREDENTIAL_REFRESH_SKEW_SEC = 5 * 60
_BACKOFF_SLICE_SEC = 0.25

_AUTH_MESSAGE_MARKERS = (
    "authfailure",
    "signature expire",
    "signatureexpire",
    "expired token",
    "token expire",
    "tokenfailure",
    "secretidnotfound",
    "invalidsecretid",
)
_BALANCE_MARKERS = (
    "userhasnofreeamount",
    "userhasnoamount",
    "nofreeamount",
    "serviceisolate",
    "usernotregistered",
    "insufficientbalance",
    "balancenotenough",
    "arrears",
    "resourcepackagerunout",
    "unpaid",
    "nopayment",
)
_BALANCE_TEXT = ("余额不足", "欠费", "未付费", "账号欠费", "资源包已用尽", "服务未开通")
_INVALID_PREFIXES = ("InvalidParameter", "MissingParameter", "UnknownParameter", "UnsupportedOperation")
_TRANSIENT_PREFIXES = ("RequestLimitExceeded", "RequestLimit", "InternalError", "ResourceUnavailable")
_TRANSIENT_CODES = {"Timeout", "Network", "BadResponse"}


class TencentAsrError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        http_status: int | None = None,
        permanent: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class AsrCredentials:
    """一次口播用的临时凭证。repr 不包含密钥。"""

    secret_id: str
    secret_key: str
    token: str
    region: str = ""
    expired_at: float | None = None

    def __repr__(self) -> str:
        return f"AsrCredentials(region={self.region!r}, expired_at={self.expired_at!r})"

    __str__ = __repr__


CredentialProvider = Callable[[], Awaitable[AsrCredentials]]


def coerce_expired_at(value: object) -> float | None:
    """中转站的 expired_at 是 unix 秒。毫秒值折成秒。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10**12:
        number /= 1000.0
    if number <= 0:
        return None
    return number


def parse_relay_credentials(data: object) -> AsrCredentials:
    """从中转站响应取出凭证。缺字段时抛 ValueError，消息里不带密钥。"""
    if not isinstance(data, dict):
        raise ValueError("中转站没有返回口播凭证")
    secret_id = str(data.get("tmp_secret_id") or "").strip()
    secret_key = str(data.get("tmp_secret_key") or "").strip()
    token = str(data.get("token") or "").strip()
    region = str(data.get("region") or "").strip()
    if not secret_id or not secret_key or not token:
        raise ValueError("中转站没有返回口播凭证")
    return AsrCredentials(
        secret_id=secret_id,
        secret_key=secret_key,
        token=token,
        region=region,
        expired_at=coerce_expired_at(data.get("expired_at")),
    )


def retry_backoff_seconds(retry_index: int, *, rng: random.Random | None = None) -> float:
    """第 retry_index 次重试的等待时间，从 1 开始。带抖动，不超过 30 秒。"""
    index = max(1, int(retry_index))
    base = min(ASR_BACKOFF_CAP_SEC, ASR_BACKOFF_BASE_SEC * (2 ** min(index - 1, 8)))
    roll = (rng or random).random()
    return min(ASR_BACKOFF_CAP_SEC, base * (0.8 + 0.4 * roll))


def _is_auth_error(exc: TencentAsrError) -> bool:
    code = exc.code or ""
    if code.startswith("AuthFailure"):
        return True
    lowered = code.lower()
    if any(marker in lowered for marker in ("signatureexpire", "tokenfailure", "secretidnotfound", "invalidsecretid")):
        return True
    text = str(exc).lower()
    if any(marker in text for marker in _AUTH_MESSAGE_MARKERS):
        return True
    raw = str(exc)
    if "签名" in raw and "过期" in raw:
        return True
    if "token" in text and "过期" in raw:
        return True
    return False


def _is_balance_error(exc: TencentAsrError) -> bool:
    lowered = (exc.code or "").lower()
    if any(marker in lowered for marker in _BALANCE_MARKERS):
        return True
    text = str(exc)
    return any(marker in text for marker in _BALANCE_TEXT)


def _is_invalid_request(exc: TencentAsrError) -> bool:
    return (exc.code or "").startswith(_INVALID_PREFIXES)


def _is_transient_error(exc: TencentAsrError) -> bool:
    status = exc.http_status
    if status is not None and (status >= 500 or status in {408, 429}):
        return True
    code = exc.code or ""
    if code in _TRANSIENT_CODES:
        return True
    return code.startswith(_TRANSIENT_PREFIXES)


def should_retry_asr(exc: TencentAsrError, *, has_provider: bool) -> bool:
    """临时故障和刷新后的鉴权失败可以重试。空音频、参数、欠费和坏音频不行。"""
    if exc.permanent or _is_balance_error(exc):
        return False
    status = exc.http_status
    if status is not None and (status >= 500 or status in {408, 429}):
        return True
    if _is_invalid_request(exc):
        return False
    if _is_auth_error(exc):
        return has_provider
    return _is_transient_error(exc)


async def wait_backoff(
    seconds: float,
    cancel: asyncio.Event | None,
    resume: asyncio.Event | None,
) -> None:
    """退避等待。取消立刻停下；暂停时把截止时间往后推。"""
    if cancel is not None and cancel.is_set():
        raise JobCancelled()
    if resume is not None and not resume.is_set():
        await wait_while_paused(cancel or asyncio.Event(), resume)
        if cancel is not None and cancel.is_set():
            raise JobCancelled()
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            raise JobCancelled()
        if resume is not None and not resume.is_set():
            held = time.monotonic()
            await wait_while_paused(cancel or asyncio.Event(), resume)
            deadline += time.monotonic() - held
            if cancel is not None and cancel.is_set():
                raise JobCancelled()
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        step = min(remaining, _BACKOFF_SLICE_SEC)
        if cancel is None:
            await asyncio.sleep(step)
            continue
        try:
            await asyncio.wait_for(cancel.wait(), timeout=step)
        except TimeoutError:
            continue
        raise JobCancelled()


def sign_tc3(
    *,
    secret_id: str,
    secret_key: str,
    region: str,
    action: str,
    payload: str,
    timestamp: int,
    host: str = ASR_HOST,
    token: str = "",
) -> dict[str, str]:
    algorithm = "TC3-HMAC-SHA256"
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            hashed_payload,
        ]
    )
    credential_scope = f"{date}/{ASR_SERVICE}/tc3_request"
    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "\n".join([algorithm, str(timestamp), credential_scope, hashed_canonical])

    def _hmac(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac(secret_date, ASR_SERVICE)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": ASR_VERSION,
        "X-TC-Region": region,
    }
    session = token.strip()
    if session:
        headers["X-TC-Token"] = session
    return headers


def chunk_spans(duration: float, byte_size: int, limit: int = MAX_AUDIO_BYTES) -> list[tuple[float, float]]:
    """按平均码率把整段切成不超过 limit 的时间片。"""
    if duration <= 0:
        return []
    if byte_size <= limit:
        return [(0.0, duration)]
    seconds = max(15.0, (limit * 0.85) / (byte_size / duration))
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration - 0.05:
        end = min(duration, cursor + seconds)
        spans.append((cursor, end))
        cursor = end
    return spans


def parse_asr_data(data: dict, offset_sec: float) -> list[SpeechSentence]:
    detail = data.get("ResultDetail")
    sentences = _from_detail(detail, offset_sec) if isinstance(detail, list) else []
    if sentences:
        return sentences
    return _from_brackets(str(data.get("Result") or ""), offset_sec)


def _from_detail(detail: list, offset_sec: float) -> list[SpeechSentence]:
    sentences: list[SpeechSentence] = []
    for item in detail:
        if not isinstance(item, dict):
            continue
        text = str(item.get("FinalSentence") or item.get("SliceSentence") or "").strip()
        if not text:
            continue
        start_ms = _number(item.get("StartMs"))
        end_ms = _number(item.get("EndMs"))
        start = offset_sec + start_ms / 1000
        end = offset_sec + end_ms / 1000
        if end <= start:
            end = start + 0.2
        words: list[SpeechWord] = []
        raw_words = item.get("Words") or []
        if isinstance(raw_words, list):
            for word in raw_words:
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("Word") or "").strip()
                if not word_text:
                    continue
                word_start = offset_sec + (start_ms + _number(word.get("OffsetStartMs"))) / 1000
                word_end = offset_sec + (start_ms + _number(word.get("OffsetEndMs"))) / 1000
                if word_end <= word_start:
                    word_end = word_start + 0.05
                words.append(SpeechWord(word_text, word_start, word_end))
        sentences.append(SpeechSentence(text, start, end, words))
    return sentences


def _from_brackets(result: str, offset_sec: float) -> list[SpeechSentence]:
    sentences: list[SpeechSentence] = []
    for line in result.splitlines():
        matched = _BRACKET.search(line.strip())
        if not matched:
            continue
        text = matched.group(5).strip()
        if not text:
            continue
        start = offset_sec + _clock(matched.group(1), matched.group(2))
        end = offset_sec + _clock(matched.group(3), matched.group(4))
        if end <= start:
            end = start + 0.2
        sentences.append(SpeechSentence(text, start, end))
    return sentences


def _clock(minutes: str, seconds: str) -> float:
    return int(minutes) * 60 + float(seconds)


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class TencentAsrClient:
    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region: str,
        engine: str = ASR_ENGINE,
        *,
        token: str = "",
        host: str | None = None,
        timeout: float = 60,
        poll_interval: float = 1.5,
        transport: httpx.AsyncBaseTransport | None = None,
        credential_provider: CredentialProvider | None = None,
        expired_at: float | None = None,
    ):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.token = token.strip()
        self.region = region or "ap-shanghai"
        self.expired_at = coerce_expired_at(expired_at)
        self.engine = configured_engine(engine)
        self.host = host or os.environ.get("TENCENT_ASR_HOST", "").strip() or ASR_HOST
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._transport = transport
        self._provider = credential_provider
        self._credential_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def backoff_delay(self, retry_index: int) -> float:
        return retry_backoff_seconds(retry_index)

    def apply_credentials(
        self,
        secret_id: str,
        secret_key: str,
        region: str,
        token: str,
        expired_at: float | None = None,
    ) -> None:
        """用这一次任务的临时凭证替换签名材料。不写入磁盘，也不打日志。"""
        self.secret_id = secret_id.strip()
        self.secret_key = secret_key.strip()
        self.token = token.strip()
        if region.strip():
            self.region = region.strip()
        self.expired_at = coerce_expired_at(expired_at)

    def _credential_due(self, now: float | None = None) -> bool:
        if not self.secret_id or not self.secret_key or not self.token:
            return True
        if self.expired_at is None:
            return False
        current = time.time() if now is None else now
        return self.expired_at - current <= CREDENTIAL_REFRESH_SKEW_SEC

    async def _ensure_credentials(self, *, force: bool) -> None:
        provider = self._provider
        if provider is None:
            if not self.secret_id or not self.secret_key:
                raise TencentAsrError("没有口播凭证", permanent=True)
            return
        if not force and not self._credential_due():
            return
        async with self._credential_lock:
            if not force and not self._credential_due():
                return
            fresh = await provider()
            if not isinstance(fresh, AsrCredentials):
                raise TencentAsrError("没有口播凭证", permanent=True)
            secret_id = fresh.secret_id.strip()
            secret_key = fresh.secret_key.strip()
            token = fresh.token.strip()
            if not secret_id or not secret_key or not token:
                raise TencentAsrError("没有口播凭证", permanent=True)
            self.apply_credentials(secret_id, secret_key, fresh.region, token, expired_at=fresh.expired_at)

    async def __aenter__(self) -> TencentAsrClient:
        self._client = httpx.AsyncClient(
            base_url=f"https://{self.host}",
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            transport=self._transport,
            trust_env=self._transport is None,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def transcribe(
        self,
        pieces: list[tuple[float, Path]],
        *,
        cancel: asyncio.Event,
        on_status: Callable[[str], Awaitable[None]],
        resume: asyncio.Event | None = None,
    ) -> list[SpeechSentence]:
        client = self._client
        if client is None:
            raise RuntimeError("口播识别已取消")
        if self._provider is None and (not self.secret_id or not self.secret_key):
            raise TencentAsrError("没有口播凭证", permanent=True)
        sentences: list[SpeechSentence] = []
        total = max(len(pieces), 1)
        for index, (offset, path) in enumerate(pieces, start=1):
            await wait_while_paused(cancel, resume)
            if cancel.is_set():
                raise JobCancelled()
            await on_status(f"正在提交腾讯云识别（{index}/{total}）")
            task_id = await self._create(client, path, cancel, resume, on_status)
            await on_status(f"口播识别中（{index}/{total}）")
            data = await self._poll(client, task_id, cancel, resume, on_status)
            sentences.extend(parse_asr_data(data, offset))
        return sentences

    async def _create(
        self,
        client: httpx.AsyncClient,
        path: Path,
        cancel: asyncio.Event,
        resume: asyncio.Event | None,
        on_status: Callable[[str], Awaitable[None]],
    ) -> int:
        audio = path.read_bytes()
        if not audio:
            raise TencentAsrError("音频文件是空的", permanent=True)
        if len(audio) > 5 * 1024 * 1024:
            raise TencentAsrError("音频片段超过腾讯云 5MB 限制", permanent=True)
        body = {
            "EngineModelType": self.engine,
            "ChannelNum": 1,
            "ResTextFormat": 2,
            "SourceType": 1,
            "Data": base64.b64encode(audio).decode("ascii"),
            "DataLen": len(audio),
            "ConvertNumMode": 0,
        }
        payload = await self._call(client, "CreateRecTask", body, cancel, resume, on_status)
        task_id = (payload.get("Data") or {}).get("TaskId")
        if task_id is None:
            raise TencentAsrError("腾讯云没有返回识别任务编号", permanent=True)
        return int(task_id)

    async def _poll(
        self,
        client: httpx.AsyncClient,
        task_id: int,
        cancel: asyncio.Event,
        resume: asyncio.Event | None,
        on_status: Callable[[str], Awaitable[None]],
    ) -> dict:
        deadline = time.monotonic() + 30 * 60
        while True:
            held = time.monotonic()
            await wait_while_paused(cancel, resume)
            deadline += time.monotonic() - held
            if cancel.is_set():
                raise JobCancelled()
            if time.monotonic() > deadline:
                raise TencentAsrError("腾讯云识别超时", permanent=True)
            payload = await self._call(client, "DescribeTaskStatus", {"TaskId": task_id}, cancel, resume, on_status)
            data = payload.get("Data") or {}
            status = data.get("Status")
            status_text = str(data.get("StatusStr") or "")
            if status == 2 or status_text == "success":
                return data
            if status == 3 or status_text == "failed":
                detail = redact(str(data.get("ErrorMsg") or "识别失败"))
                raise TencentAsrError(f"腾讯云识别失败：{detail[:200]}", permanent=True)
            try:
                await asyncio.wait_for(cancel.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
            raise JobCancelled()

    async def _call(
        self,
        client: httpx.AsyncClient,
        action: str,
        body: dict,
        cancel: asyncio.Event,
        resume: asyncio.Event | None,
        on_status: Callable[[str], Awaitable[None]],
    ) -> dict:
        last: TencentAsrError | None = None
        force_refresh = False
        for attempt in range(ASR_MAX_RETRIES + 1):
            if attempt:
                await on_status(f"腾讯云识别重试中（{attempt}/{ASR_MAX_RETRIES}）")
                await wait_backoff(self.backoff_delay(attempt), cancel, resume)
            await self._ensure_credentials(force=force_refresh)
            force_refresh = False
            try:
                return await self._invoke(client, action, body)
            except asyncio.CancelledError:
                raise
            except TencentAsrError as exc:
                last = exc
                if not should_retry_asr(exc, has_provider=self._provider is not None):
                    raise
                if _is_auth_error(exc):
                    force_refresh = True
                if attempt >= ASR_MAX_RETRIES:
                    raise
                log.warning(
                    "asr %s will retry %s/%s code=%s http=%s",
                    action,
                    attempt + 1,
                    ASR_MAX_RETRIES,
                    exc.code,
                    exc.http_status,
                )
        assert last is not None
        raise last

    async def _invoke(self, client: httpx.AsyncClient, action: str, body: dict) -> dict:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())
        headers = sign_tc3(
            secret_id=self.secret_id,
            secret_key=self.secret_key,
            region=self.region,
            action=action,
            payload=payload,
            timestamp=timestamp,
            host=self.host,
            token=self.token,
        )
        try:
            response = await client.post("/", content=payload.encode("utf-8"), headers=headers)
        except httpx.TimeoutException as exc:
            raise TencentAsrError("腾讯云识别请求超时", code="Timeout") from exc
        except httpx.HTTPError as exc:
            raise TencentAsrError("无法连接腾讯云语音识别", code="Network") from exc
        if response.status_code != 200:
            code, message = _error_parts(response)
            message = self._scrub(message)
            detail = f"：{code} {message}".rstrip() if code or message else ""
            raise TencentAsrError(
                f"腾讯云返回 HTTP {response.status_code}{detail}"[:300],
                code=code,
                http_status=response.status_code,
            )
        try:
            parsed = response.json()
        except json.JSONDecodeError as exc:
            raise TencentAsrError("腾讯云响应无法解析", code="BadResponse") from exc
        result = parsed.get("Response") if isinstance(parsed, dict) else None
        if not isinstance(result, dict):
            raise TencentAsrError("腾讯云响应格式无法识别", code="BadResponse")
        error = result.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "")
            message = self._scrub(str(error.get("Message") or "请求失败")).replace("\n", " ")
            raise TencentAsrError(f"腾讯云识别失败：{code} {message}".strip()[:300], code=code)
        return result

    def _scrub(self, text: str) -> str:
        cleaned = redact(text)
        for secret in (self.secret_key, self.token, self.secret_id):
            if secret and len(secret) >= 8:
                cleaned = cleaned.replace(secret, "***")
        return cleaned


def _error_parts(response: httpx.Response) -> tuple[str, str]:
    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    error = parsed.get("Response", {}).get("Error") if isinstance(parsed.get("Response"), dict) else None
    if not isinstance(error, dict):
        error = parsed.get("Error")
    if not isinstance(error, dict):
        return "", ""
    code = str(error.get("Code") or "")
    message = redact(str(error.get("Message") or "")).replace("\n", " ")[:180]
    return code, message


class FaultAsrClient:
    """测试用。不连接腾讯云，直接按接口调用失败返回。"""

    async def __aenter__(self) -> FaultAsrClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False

    async def aclose(self) -> None:
        return None

    async def transcribe(
        self,
        pieces: list[tuple[float, Path]],
        *,
        cancel: asyncio.Event,
        on_status: Callable[[str], Awaitable[None]],
        resume: asyncio.Event | None = None,
    ) -> list[SpeechSentence]:
        del pieces, on_status, resume
        if cancel.is_set():
            raise JobCancelled()
        raise TencentAsrError("无法连接腾讯云语音识别", code="Network")


class MockAsrClient:
    """不访问腾讯云。返回一句固定口播，用来打通并行流程。"""

    def __init__(self, sentences: list[SpeechSentence] | None = None):
        self.sentences = sentences

    async def __aenter__(self) -> MockAsrClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False

    async def aclose(self) -> None:
        return None

    async def transcribe(
        self,
        pieces: list[tuple[float, Path]],
        *,
        cancel: asyncio.Event,
        on_status: Callable[[str], Awaitable[None]],
        resume: asyncio.Event | None = None,
    ) -> list[SpeechSentence]:
        await wait_while_paused(cancel, resume)
        if cancel.is_set():
            raise JobCancelled()
        await on_status("口播识别中（模拟）")
        if self.sentences is not None:
            return list(self.sentences)
        return [SpeechSentence("这款只要九十九元", 0.0, 0.5)]
