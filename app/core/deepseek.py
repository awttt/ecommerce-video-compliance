"""OpenAI 兼容的画面与口播文本调用。

glm-5.3-flash 拒绝 thinking.type=disabled，并且省略 reasoning_effort 时默认是 max。
思考开关因此改成推理强度：关闭用 low，打开用 high。图片只传 image_url.url。
qwen3.8-flash 默认把思考过程放在 reasoning_content，解析时只读 message.content。
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
from app.core.model_route import (
    MAX_STRIP_ROUNDS,
    TRANSPORT_ATTEMPTS,
    HttpBudget,
    ModelRoute,
    max_http_per_logical_dispatch,
    split_percent,
)
from app.core.relay import LUNA_MODEL, QWEN_FLASH_MODEL, USER_AGENT
from app.logging_setup import redact
from app.schemas import DetectionRules, Frame, RawHit, RunConfig

log = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
# 保留这个名字，是因为口播和画面现在共用同一轮传输次数。
_SPEECH_ATTEMPTS = TRANSPORT_ATTEMPTS
_MAX_TOKENS = {"low": 2048, "high": 4096}
# qwen3.8-flash 的 completion 含 reasoning_tokens，实测大约是 glm 的 2 倍。
# 上限加倍，给 content 留出和 glm 相近的空间，避免思考过程把 JSON 截断。
_QWEN_MAX_TOKENS = {"low": 4096, "high": 8192}
_REASONING_PARTS = {"reasoning", "thinking", "reasoning_content", "redacted_reasoning"}
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


def model_controls(thinking: bool, model: str | None = None) -> dict:
    """glm-5.3-flash 必须开启思考。强度用 low 代替以前的关闭，避免默认 max。

    qwen3.8-flash 默认也会思考，沿用同一组强度，只放宽 max_tokens。
    gpt-6-luna 不带 thinking / reasoning_effort。这只在 mock 里核对过请求形状，
    不表示线上已经接受这些字段或能够识图。
    """
    effort = "high" if thinking else "low"
    if model == LUNA_MODEL:
        return {"max_tokens": _MAX_TOKENS[effort]}
    limits = _QWEN_MAX_TOKENS if model == QWEN_FLASH_MODEL else _MAX_TOKENS
    return {
        "max_tokens": limits[effort],
        "thinking": {"type": "enabled"},
        "reasoning_effort": effort,
    }


def _encode_image(path: str) -> str:
    data = Path(path).read_bytes()
    if not data:
        raise RuntimeError("抽帧文件是空的")
    return base64.b64encode(data).decode("ascii")


class DeepSeekRecognizer:
    def __init__(
        self,
        config: RunConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        route: ModelRoute | None = None,
    ):
        self.config = config
        self.route = route or ModelRoute(
            selected_model=config.model,
            percent=split_percent(config.model),
        )
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> DeepSeekRecognizer:
        kwargs: dict = {
            "timeout": httpx.Timeout(self.config.timeout_sec, connect=10.0),
            "headers": {"User-Agent": USER_AGENT},
            "limits": httpx.Limits(
                max_connections=self.config.concurrency + 2,
                max_keepalive_connections=self.config.concurrency,
            ),
            "trust_env": self._transport is None,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
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
        model = await self.route.begin_picture(frame.index)
        budget = HttpBudget(max_http_per_logical_dispatch())
        try:
            encoded = await asyncio.to_thread(_encode_image, frame.path)
            body = self._body(frame, encoded, model)
            content = await self._complete_message(client, body, budget=budget)
            try:
                hits = parse_frame_result(content, frame)
            except ValueError:
                log.warning("frame %s returned unparsed JSON, retrying", frame.index)
                retry = self._body(frame, encoded, model)
                if "response_format" not in body:
                    retry.pop("response_format", None)
                retry["messages"].append({"role": "user", "content": _JSON_FIX})
                content = await self._complete_message(client, retry, budget=budget)
                try:
                    hits = parse_frame_result(content, frame)
                except ValueError:
                    log.warning("frame %s unparsed response: %s", frame.index, redact(content)[:300])
                    raise
            log.debug("frame %s response %s", frame.index, redact(content)[:300])
            hits = apply_rules(hits, self.config.rules)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.route.finish(model, exc)
            raise
        await self.route.finish(model, None)
        return hits

    async def judge_speech(self, sentences: list[SpeechSentence]) -> list[RawHit]:
        client = self._client
        if client is None:
            raise RuntimeError("识别已取消")
        hits: list[RawHit] = []
        errors: list[str] = []
        for batch_index, batch in enumerate(_speech_batches(sentences)):
            model = await self.route.begin_speech(batch_index)
            budget = HttpBudget(max_http_per_logical_dispatch())
            try:
                body = self._speech_body(batch, model)
                content = await self._complete_message(client, body, budget=budget)
                try:
                    batch_hits = parse_speech_result(content, batch, corpus=sentences)
                except ValueError:
                    retry = self._speech_body(batch, model)
                    if "response_format" not in body:
                        retry.pop("response_format", None)
                    retry["messages"].append({"role": "user", "content": _JSON_FIX})
                    content = await self._complete_message(client, retry, budget=budget)
                    batch_hits = parse_speech_result(content, batch, corpus=sentences)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.route.finish(model, exc)
                message = str(exc)
                if "JSON" in message or "json" in message or "Expecting" in message:
                    message = "口播判定结果不是合法 JSON"
                errors.append(message)
                continue
            await self.route.finish(model, None)
            hits.extend(batch_hits)
        if errors and not hits:
            local = apply_rules([], self.config.rules, sentences=sentences)
            if local:
                raise SpeechJudgeError(f"{errors[0]}，已按词表保留 {len(local)} 条", local)
            raise RuntimeError(errors[0])
        return apply_rules(hits, self.config.rules, sentences=sentences)

    async def _complete_message(self, client: httpx.AsyncClient, body: dict, *, budget: HttpBudget) -> str:
        timeout = speech_request_timeout(self.config.timeout_sec)
        strips = 0
        model = str(body.get("model") or self.config.model)
        while True:
            try:
                return await self._complete(client, body, timeout=timeout, attempts=_SPEECH_ATTEMPTS, budget=budget)
            except APIStatusError as exc:
                if (
                    exc.status == 400
                    and strips < MAX_STRIP_ROUNDS
                    and _drop_rejected_controls(body, exc.detail, model=model)
                ):
                    strips += 1
                    continue
                raise

    def _speech_body(self, sentences: list[SpeechSentence], model: str | None = None) -> dict:
        lines = [
            f"{index}. [{format_range(item.start_sec, item.end_sec)}] {item.text}"
            for index, item in enumerate(sentences, start=1)
        ]
        chosen = model or self.config.model
        body: dict = {
            "model": chosen,
            "messages": [
                {"role": "system", "content": speech_system_prompt(self.config.rules)},
                {"role": "user", "content": speech_user_prompt(lines, self.config.rules)},
            ],
            "response_format": {"type": "json_object"},
        }
        body.update(model_controls(self.config.thinking, chosen))
        return body

    def _body(self, frame: Frame, encoded: str, model: str | None = None) -> dict:
        chosen = model or self.config.model
        body: dict = {
            "model": chosen,
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
        body.update(model_controls(self.config.thinking, chosen))
        return body

    async def _complete(
        self,
        client: httpx.AsyncClient,
        body: dict,
        *,
        timeout: httpx.Timeout | None = None,
        attempts: int = TRANSPORT_ATTEMPTS,
        budget: HttpBudget | None = None,
    ) -> str:
        chosen = str(body.get("model") or self.config.model)
        last_error = "模型请求失败"
        for attempt in range(attempts):
            if budget is not None:
                budget.take()
            await self.route.note_http(chosen)
            try:
                response = await client.post(
                    chat_url(self.config.base_url),
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "User-Agent": USER_AGENT,
                    },
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


def _drop_rejected_controls(body: dict, detail: str, *, model: str) -> bool:
    """上游 400 点名不支持的字段时去掉。qwen 不接受 glm 的思考字段时只去掉那一对。"""
    dropped = False
    if "response_format" in body and "response_format" in detail:
        body.pop("response_format", None)
        dropped = True
    if model == QWEN_FLASH_MODEL and ("thinking" in detail or "reasoning_effort" in detail):
        if "thinking" in body or "reasoning_effort" in body:
            body.pop("thinking", None)
            body.pop("reasoning_effort", None)
            dropped = True
    return dropped


def _message_text(payload: dict) -> str:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型响应缺少内容") from exc
    if not isinstance(message, dict):
        raise RuntimeError("模型响应缺少内容")
    # qwen3.8-flash 把思考过程放在 reasoning_content。只读 content，避免思考文本混进 JSON。
    content = message.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "text").lower()
            if kind in _REASONING_PARTS:
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
        content = "".join(chunks)
    elif not isinstance(content, str):
        content = ""
    text = content.strip()
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


def build_recognizer(config: RunConfig, route: ModelRoute | None = None) -> Recognizer:
    if config.fault_bad_json or config.mock:
        inner: Recognizer = MockRecognizer(config.rules)
    else:
        if not config.api_key:
            raise RuntimeError("未配置画面识别 API Key。请登录后再开始检测。")
        inner = DeepSeekRecognizer(config, route=route)
    if config.fault_bad_json or config.fault_partial_frames:
        return FaultRecognizer(inner, config)
    return inner
