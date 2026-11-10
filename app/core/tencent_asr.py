"""腾讯云录音文件识别。本地音频用 SourceType=1 上传，超过 5MB 的由调用方切段。

签名使用 TC3-HMAC-SHA256，不依赖腾讯云 SDK。结果优先读 ResultDetail 的毫秒时间戳。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.pipeline import JobCancelled
from app.core.transcript import SpeechSentence, SpeechWord

log = logging.getLogger(__name__)

ASR_HOST = "asr.tencentcloudapi.com"
ASR_SERVICE = "asr"
ASR_VERSION = "2019-06-14"
# 接口限制是 5MB（含）。留出余量，避免边界被拒。
MAX_AUDIO_BYTES = 4_500_000
_BRACKET = re.compile(r"\[(\d+):(\d+(?:\.\d+)?),(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")


class TencentAsrError(RuntimeError):
    pass


def sign_tc3(
    *,
    secret_id: str,
    secret_key: str,
    region: str,
    action: str,
    payload: str,
    timestamp: int,
    host: str = ASR_HOST,
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
    return {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": ASR_VERSION,
        "X-TC-Region": region,
    }


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
        engine: str = "16k_zh",
        *,
        host: str | None = None,
        timeout: float = 60,
        poll_interval: float = 1.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region or "ap-shanghai"
        self.engine = engine or "16k_zh"
        self.host = host or os.environ.get("TENCENT_ASR_HOST", "").strip() or ASR_HOST
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

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
    ) -> list[SpeechSentence]:
        client = self._client
        if client is None:
            raise RuntimeError("口播识别已取消")
        sentences: list[SpeechSentence] = []
        total = max(len(pieces), 1)
        for index, (offset, path) in enumerate(pieces, start=1):
            if cancel.is_set():
                raise JobCancelled()
            await on_status(f"正在提交腾讯云识别（{index}/{total}）")
            task_id = await self._create(client, path)
            await on_status(f"口播识别中（{index}/{total}）")
            data = await self._poll(client, task_id, cancel)
            sentences.extend(parse_asr_data(data, offset))
        return sentences

    async def _create(self, client: httpx.AsyncClient, path: Path) -> int:
        audio = path.read_bytes()
        if not audio:
            raise TencentAsrError("音频文件是空的")
        if len(audio) > 5 * 1024 * 1024:
            raise TencentAsrError("音频片段超过腾讯云 5MB 限制")
        body = {
            "EngineModelType": self.engine,
            "ChannelNum": 1,
            "ResTextFormat": 2,
            "SourceType": 1,
            "Data": base64.b64encode(audio).decode("ascii"),
            "DataLen": len(audio),
            "ConvertNumMode": 0,
        }
        payload = await self._call(client, "CreateRecTask", body)
        task_id = (payload.get("Data") or {}).get("TaskId")
        if task_id is None:
            raise TencentAsrError("腾讯云没有返回识别任务编号")
        return int(task_id)

    async def _poll(self, client: httpx.AsyncClient, task_id: int, cancel: asyncio.Event) -> dict:
        deadline = time.monotonic() + 30 * 60
        while True:
            if cancel.is_set():
                raise JobCancelled()
            if time.monotonic() > deadline:
                raise TencentAsrError("腾讯云识别超时")
            payload = await self._call(client, "DescribeTaskStatus", {"TaskId": task_id})
            data = payload.get("Data") or {}
            status = data.get("Status")
            status_text = str(data.get("StatusStr") or "")
            if status == 2 or status_text == "success":
                return data
            if status == 3 or status_text == "failed":
                detail = str(data.get("ErrorMsg") or "识别失败")
                raise TencentAsrError(f"腾讯云识别失败：{detail[:200]}")
            try:
                await asyncio.wait_for(cancel.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
            raise JobCancelled()

    async def _call(self, client: httpx.AsyncClient, action: str, body: dict) -> dict:
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
        )
        try:
            response = await client.post("/", content=payload.encode("utf-8"), headers=headers)
        except httpx.TimeoutException as exc:
            raise TencentAsrError("腾讯云识别请求超时") from exc
        except httpx.HTTPError as exc:
            raise TencentAsrError("无法连接腾讯云语音识别") from exc
        if response.status_code != 200:
            raise TencentAsrError(f"腾讯云返回 HTTP {response.status_code}")
        try:
            parsed = response.json()
        except json.JSONDecodeError as exc:
            raise TencentAsrError("腾讯云响应无法解析") from exc
        result = parsed.get("Response") if isinstance(parsed, dict) else None
        if not isinstance(result, dict):
            raise TencentAsrError("腾讯云响应格式无法识别")
        error = result.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or "请求失败")
            raise TencentAsrError(f"腾讯云识别失败：{code} {message}"[:300])
        return result


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
    ) -> list[SpeechSentence]:
        if cancel.is_set():
            raise JobCancelled()
        await on_status("口播识别中（模拟）")
        if self.sentences is not None:
            return list(self.sentences)
        return [SpeechSentence("这款只要九十九元", 0.0, 0.5)]
