"""OpenAI 兼容的画面与口播文本调用。模型名 glm-5.3-flash。

glm-5.3-flash 拒绝 thinking.type=disabled，并且省略 reasoning_effort 时默认是 max。
思考开关因此改成推理强度：关闭用 low，打开用 high。图片只传 image_url.url。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Protocol

import httpx

from app.core.parsing import extract_json, parse_frame_result, parse_speech_result
from app.core.prompts import speech_system_prompt, speech_user_prompt, user_prompt, visual_system_prompt
from app.core.rules import apply_rules
from app.core.timeutil import format_range
from app.core.transcript import SpeechSentence
from app.logging_setup import redact
from app.schemas import DetectionRules, Frame, RawHit, RunConfig

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
_MAX_TOKENS = {"low": 2048, "high": 4096}
_JSON_FIX = "上一次不是合法 JSON。请只输出一个 JSON 对象，键名和文字都用英文双引号，} 和 ] 前面不要加逗号。"
# 口播判定带整份词表和思考，比单帧画面更容易超过「单帧超时」。
_SPEECH_TIMEOUT_FLOOR = 120.0
_SPEECH_TIMEOUT_CAP = 180.0
_SPEECH_ATTEMPTS = 2


class SpeechJudgeError(RuntimeError):
    """模型没判完，但词表已经在转写里对上了。hits 要保留。"""

    def __init__(self, message: str, hits: list[RawHit]):
        super().__init__(message)
        self.hits = hits


def speech_request_timeout(timeout_sec: float) -> httpx.Timeout:
    seconds = min(_SPEECH_TIMEOUT_CAP, max(float(timeout_sec), _SPEECH_TIMEOUT_FLOOR))
    return httpx.Timeout(seconds, connect=10.0)


class Recognizer(Protocol):
    async def recognize(self, frame: Frame) -> list[RawHit]: ...

    async def judge_speech(self, sentences: list[SpeechSentence]) -> list[RawHit]: ...

    async def aclose(self) -> None: ...


class APIStatusError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def model_controls(thinking: bool) -> dict:
    """glm-5.3-flash 必须开启思考。强度用 low 代替以前的关闭，避免默认 max。"""
    effort = "high" if thinking else "low"
    return {
        "max_tokens": _MAX_TOKENS[effort],
        "thinking": {"type": "enabled"},
        "reasoning_effort": effort,
    }


def _encode_image(path: str) -> str:
    data = Path(path).read_bytes()
    if not data:
        raise RuntimeError("抽帧文件是空的")
    return base64.b64encode(data).decode("ascii")


class DeepSeekRecognizer:
    def __init__(self, config: RunConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> DeepSeekRecognizer:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_sec, connect=10.0),
            limits=httpx.Limits(
                max_connections=self.config.concurrency + 2,
                max_keepalive_connections=self.config.concurrency,
            ),
            trust_env=True,
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

    async def recognize(self, frame: Frame) -> list[RawHit]:
        client = self._client
        if client is None:
            raise RuntimeError("识别已取消")
        encoded = await asyncio.to_thread(_encode_image, frame.path)
        body = self._body(frame, encoded)
        content = await self._complete_message(client, body)
        try:
            hits = parse_frame_result(content, frame)
        except ValueError:
            log.warning("frame %s returned unparsed JSON, retrying", frame.index)
            retry = self._body(frame, encoded)
            if "response_format" not in body:
                retry.pop("response_format", None)
            retry["messages"].append({"role": "user", "content": _JSON_FIX})
            content = await self._complete_message(client, retry)
            try:
                hits = parse_frame_result(content, frame)
            except ValueError:
                log.warning("frame %s unparsed response: %s", frame.index, redact(content)[:300])
                raise
        log.debug("frame %s response %s", frame.index, redact(content)[:300])
        return apply_rules(hits, self.config.rules)

    async def judge_speech(self, sentences: list[SpeechSentence]) -> list[RawHit]:
        client = self._client
        if client is None:
            raise RuntimeError("识别已取消")
        hits: list[RawHit] = []
        errors: list[str] = []
        for batch in _speech_batches(sentences):
            body = self._speech_body(batch)
            try:
                content = await self._complete_message(client, body)
                try:
                    batch_hits = parse_speech_result(content, batch, corpus=sentences)
                except ValueError:
                    retry = self._speech_body(batch)
                    if "response_format" not in body:
                        retry.pop("response_format", None)
                    retry["messages"].append({"role": "user", "content": _JSON_FIX})
                    content = await self._complete_message(client, retry)
                    batch_hits = parse_speech_result(content, batch, corpus=sentences)
            except Exception as exc:
                message = str(exc)
                if "JSON" in message or "json" in message or "Expecting" in message:
                    message = "口播判定结果不是合法 JSON"
                errors.append(message)
                continue
            hits.extend(batch_hits)
        if errors and not hits:
            local = apply_rules([], self.config.rules, sentences=sentences)
            if local:
                raise SpeechJudgeError(f"{errors[0]}，已按词表保留 {len(local)} 条", local)
            raise RuntimeError(errors[0])
        return apply_rules(hits, self.config.rules, sentences=sentences)

    async def _complete_message(self, client: httpx.AsyncClient, body: dict) -> str:
        timeout = speech_request_timeout(self.config.timeout_sec)
        try:
            return await self._complete(client, body, timeout=timeout, attempts=_SPEECH_ATTEMPTS)
        except APIStatusError as exc:
            if exc.status == 400 and "response_format" in exc.detail:
                body.pop("response_format", None)
                return await self._complete(client, body, timeout=timeout, attempts=_SPEECH_ATTEMPTS)
            raise

    def _speech_body(self, sentences: list[SpeechSentence]) -> dict:
        lines = [
            f"{index}. [{format_range(item.start_sec, item.end_sec)}] {item.text}"
            for index, item in enumerate(sentences, start=1)
        ]
        body: dict = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": speech_system_prompt(self.config.rules)},
                {"role": "user", "content": speech_user_prompt(lines, self.config.rules)},
            ],
            "response_format": {"type": "json_object"},
        }
        body.update(model_controls(self.config.thinking))
        return body

    def _body(self, frame: Frame, encoded: str) -> dict:
        body: dict = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": visual_system_prompt(self.config.rules)},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt(frame, self.config.rules)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
        }
        body.update(model_controls(self.config.thinking))
        return body

    async def _complete(
        self,
        client: httpx.AsyncClient,
        body: dict,
        *,
        timeout: httpx.Timeout | None = None,
        attempts: int = 3,
    ) -> str:
        last_error = "模型请求失败"
        for attempt in range(attempts):
            try:
                response = await client.post(
                    chat_url(self.config.base_url),
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json=body,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = "模型请求超时"
                log.warning("chat timeout attempt %s: %s", attempt + 1, exc)
            except httpx.HTTPError as exc:
                last_error = "无法连接模型服务，请检查网络"
                log.warning("chat network attempt %s: %s", attempt + 1, exc)
            else:
                if response.status_code == 200:
                    return _message_text(response.json())
                detail = _error_detail(response)
                if response.status_code not in _RETRY_STATUS:
                    raise APIStatusError(response.status_code, detail)
                last_error = detail
                log.warning("chat status %s attempt %s", response.status_code, attempt + 1)
            await asyncio.sleep(0.4 * (2**attempt))
        raise RuntimeError(last_error)


def _message_text(payload: dict) -> str:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型响应缺少内容") from exc
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    text = (content or "").strip()
    if not text:
        raise RuntimeError("模型返回空内容")
    return text


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message") or payload.get("message")
    except Exception:
        message = response.text
    message = redact(str(message or "")).strip().replace("\n", " ")
    if response.status_code in {401, 403}:
        return "模型 API Key 无效或没有权限"
    if not message:
        return f"模型返回 HTTP {response.status_code}"
    return f"模型返回 HTTP {response.status_code}：{message[:240]}"


class MockRecognizer:
    """不访问网络。奇数帧返回一条固定价格，用来打通流水线。"""

    def __init__(self, rules: DetectionRules | None = None):
        self.rules = rules or DetectionRules()

    async def __aenter__(self) -> MockRecognizer:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.aclose()
        return False

    async def aclose(self) -> None:
        return None

    async def recognize(self, frame: Frame) -> list[RawHit]:
        if frame.index % 2 == 0:
            return []
        hits = [
            RawHit(
                start_sec=frame.start_sec,
                end_sec=frame.end_sec,
                category="价格",
                text="¥199",
                position="画面右下角价格贴纸",
                confidence="确定",
                reason="带货币符号且位于商品旁价格标签",
                note="模拟识别，不是模型结果",
                source="画面",
            )
        ]
        return apply_rules(hits, self.rules)

    async def judge_speech(self, sentences: list[SpeechSentence]) -> list[RawHit]:
        hits: list[RawHit] = []
        ordered = list(sentences)
        for index, sentence in enumerate(ordered):
            next_start = None
            if index + 1 < len(ordered) and ordered[index + 1].start_sec + 1e-3 >= sentence.end_sec:
                next_start = ordered[index + 1].start_sec
            for category, quote, reason in _mock_speech_hits(sentence.text):
                located = quote if quote in sentence.text else sentence.text
                hits.append(
                    RawHit(
                        start_sec=sentence.start_sec,
                        end_sec=sentence.end_sec,
                        category=category,
                        text=located,
                        position="口播内容",
                        confidence="确定",
                        reason=reason,
                        note="模拟识别，不是模型结果",
                        source="口播",
                        sentence_start=sentence.start_sec,
                        sentence_end=sentence.end_sec,
                        next_start=next_start,
                    )
                )
        return apply_rules(hits, self.rules, sentences=ordered)


_PRICE_SPOKEN = re.compile(
    r"(?:[¥￥$]\s*)?\d+(?:\.\d+)?\s*(?:元|块|块钱)?|[零一二三四五六七八九十百千万两]+\s*(?:元|块|块钱)"
)
_SPEC_SPOKEN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ml|mL|ML|毫升|升|kg|g|克|千克|斤|件装|片|粒|cm|厘米|mm)"
)


def _speech_batches(sentences: list[SpeechSentence], size: int = 8) -> list[list[SpeechSentence]]:
    return [sentences[index : index + size] for index in range(0, len(sentences), size)]


def _mock_speech_hits(text: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    price = _PRICE_SPOKEN.search(text)
    if price:
        found.append(("价格", _mock_span(text, price), "口播中明确提及价格"))
    spec = _SPEC_SPOKEN.search(text)
    if spec:
        found.append(("规格", _mock_span(text, spec), "口播中明确提及规格"))
    return found


def _mock_span(text: str, matched) -> str:
    if matched.start() <= 12:
        return text[: matched.end()].strip("，。！？、 ")
    return matched.group(0).strip()


def frame_call_should_fail(index: int) -> bool:
    """第 1、4、7… 帧失败，其余帧继续。至少两帧时才能同时看到失败和成功。"""
    return index % 3 == 1


class FaultRecognizer:
    """测试账号注入的失败。不发起画面或口播模型请求。"""

    def __init__(self, inner: Recognizer, config: RunConfig):
        self.inner = inner
        self.config = config

    async def __aenter__(self) -> FaultRecognizer:
        enter = getattr(self.inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        exit_inner = getattr(self.inner, "__aexit__", None)
        if exit_inner is not None:
            return bool(await exit_inner(exc_type, exc, tb))
        await self.inner.aclose()
        return False

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def recognize(self, frame: Frame) -> list[RawHit]:
        if self.config.fault_bad_json:
            extract_json("{")
        if self.config.fault_partial_frames and frame_call_should_fail(frame.index):
            raise RuntimeError("模型返回 HTTP 503：Service temporarily unavailable")
        return await self.inner.recognize(frame)

    async def judge_speech(self, sentences: list[SpeechSentence]) -> list[RawHit]:
        if self.config.fault_bad_json:
            extract_json("{")
        return await self.inner.judge_speech(sentences)


def build_recognizer(config: RunConfig) -> Recognizer:
    if config.fault_bad_json or config.mock:
        inner: Recognizer = MockRecognizer(config.rules)
    else:
        if not config.api_key:
            raise RuntimeError("未配置画面识别 API Key。请登录后再开始检测。")
        inner = DeepSeekRecognizer(config)
    if config.fault_bad_json or config.fault_partial_frames:
        return FaultRecognizer(inner, config)
    return inner
