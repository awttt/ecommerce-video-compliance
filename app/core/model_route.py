"""画面和口播都只请求当前选中的模型。

不再把 glm-5.3-flash 分到 gpt-6-luna。旧任务快照里的 Luna 分配在执行、
失败帧重试和口播重判时改回当前模型，不会再发出 Luna 请求。
DeepSeek 和 Qwen 仍用各自的模型名。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.flash import FLASH_INDEX_BASE
from app.core.relay import LUNA_MODEL, PICTURE_MODEL

log = logging.getLogger(__name__)

# 不再分流。保留函数名，调用点统一得到 0。

# 一轮传输里，超时、断线和可重试状态码最多 POST 这么多次。这不是整次逻辑请求的上限。
TRANSPORT_ATTEMPTS = 2
# 400 且错误点名可去掉的字段时，最多再进入这么多轮。response_format 一轮；只有 qwen 再剥思考字段一轮。
MAX_STRIP_ROUNDS = 2
# 解析失败后，最多再发一次完整请求，模型不变。
MAX_JSON_REPAIRS = 1
STAT_FIELDS = ("logical", "runs", "http_attempts", "ok", "failed", "timeout")
# 当前代码走完传输、400 剥字段和 JSON 补救后实际打得到的次数。硬停止比这更高，用来拦住写坏的循环。
REACHABLE_HTTP_GLM_LUNA = 5
REACHABLE_HTTP_QWEN = 7


def max_http_per_logical_dispatch() -> int:
    """一次逻辑请求的硬停止。

    每一段（最初的请求，或一次 JSON 补救）最多有 1 轮传输，外加最多 2 轮 400 剥字段。
    每一轮最多 TRANSPORT_ATTEMPTS 次 POST。两段乘起来是 12。
    400 会立刻结束当前这一轮，所以实际打不到 12。测试钉住的可达次数是 GLM/Luna 5、Qwen 7。
    """
    return TRANSPORT_ATTEMPTS * (1 + MAX_STRIP_ROUNDS) * (1 + MAX_JSON_REPAIRS)


class HttpBudget:
    """同一次逻辑请求共用的 POST 计数。超限就停止，不再发下一次。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def take(self) -> None:
        if self.used >= self.limit:
            raise RuntimeError(f"已达到单次审核请求的调用上限（{self.limit} 次），已停止重试")
        self.used += 1


def split_percent(selected_model: str) -> int:
    del selected_model
    return 0


def uses_luna(ordinal: int, percent: int) -> bool:
    """ordinal 从 1 起。50% 时第 1 个是 GLM，第 2 个是 Luna。"""
    if percent <= 0 or ordinal <= 0:
        return False
    if percent >= 100:
        return True
    return (ordinal * percent) // 100 != ((ordinal - 1) * percent) // 100


def empty_assignments() -> dict[str, Any]:
    return {"picture": {}, "speech": {}, "flash_order": []}


def empty_stat() -> dict[str, int]:
    return {name: 0 for name in STAT_FIELDS}


def is_timeout_failure(exc: BaseException) -> bool:
    text = str(exc).strip()
    return text == "模型请求超时" or text.startswith("模型请求超时，")


def _text_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, str) and item.strip():
            cleaned[str(key)] = item.strip()
    return cleaned


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (int, str)) and str(item).strip()]


def normalize_assignments(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "picture": _text_map(raw.get("picture")),
        "speech": _text_map(raw.get("speech")),
        "flash_order": _text_list(raw.get("flash_order")),
    }


def normalize_stats(value: object) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}
    stats: dict[str, dict[str, int]] = {}
    for key, item in value.items():
        if not isinstance(item, dict):
            continue
        bucket = empty_stat()
        for name in STAT_FIELDS:
            raw = item.get(name, 0)
            if isinstance(raw, bool):
                number = 0
            else:
                try:
                    number = int(raw)
                except (TypeError, ValueError):
                    number = 0
            bucket[name] = max(0, number)
        stats[str(key)] = bucket
    return stats


def public_stats(value: object) -> dict[str, dict[str, int]]:
    """含尚未发生的 0。分流打开时 Luna 逻辑数为 0 也要能看见。"""
    return normalize_stats(value)


class ModelRoute:
    """任务快照里的分配表和计数。同一个 dict 会被写回 options。"""

    def __init__(
        self,
        *,
        selected_model: str,
        percent: int,
        assignments: dict[str, Any] | None = None,
        stats: dict[str, dict[str, int]] | None = None,
        allow_new_flash: bool = True,
        flush: Callable[[], Awaitable[None]] | None = None,
    ):
        self.selected_model = selected_model or PICTURE_MODEL
        if self.selected_model == LUNA_MODEL:
            self.selected_model = PICTURE_MODEL
        # 传入的旧比例一律不用，避免快照或调用方把请求再分到 Luna。
        self.percent = 0
        del percent
        self.assignments = assignments if assignments is not None else empty_assignments()
        self.stats = stats if stats is not None else {}
        self.allow_new_flash = allow_new_flash
        self._flush = flush
        self._lock = asyncio.Lock()
        if self.percent > 0:
            self._bucket(PICTURE_MODEL)
            self._bucket(LUNA_MODEL)

    @classmethod
    def bind(
        cls,
        options: dict,
        *,
        selected_model: str,
        allow_new_flash: bool = True,
        flush: Callable[[], Awaitable[None]] | None = None,
    ) -> ModelRoute:
        configured = split_percent(selected_model)
        options["luna_request_percent"] = configured
        assignments = normalize_assignments(options.get("model_assignments"))
        options["model_assignments"] = assignments
        stats = normalize_stats(options.get("model_stats"))
        options["model_stats"] = stats
        return cls(
            selected_model=selected_model,
            percent=configured,
            assignments=assignments,
            stats=stats,
            allow_new_flash=allow_new_flash,
            flush=flush,
        )

    def clear_speech(self) -> None:
        speech = self.assignments.get("speech")
        if isinstance(speech, dict):
            speech.clear()

    async def begin_picture(self, index: int) -> str:
        async with self._lock:
            model, fresh, kind = self._assign_picture(index)
        if fresh and kind == "flash" and self._flush is not None:
            await safe_flush(self._flush)
        return model

    async def begin_speech(self, batch_index: int) -> str:
        async with self._lock:
            model, fresh = self._assign_speech(batch_index)
        if fresh and self._flush is not None:
            await safe_flush(self._flush)
        return model

    async def note_http(self, model: str) -> None:
        async with self._lock:
            self._bucket(model)["http_attempts"] += 1

    async def finish(self, model: str, error: BaseException | None) -> None:
        async with self._lock:
            bucket = self._bucket(model)
            bucket["runs"] += 1
            if error is None:
                bucket["ok"] += 1
                return
            bucket["failed"] += 1
            if is_timeout_failure(error):
                bucket["timeout"] += 1

    def _bucket(self, model: str) -> dict[str, int]:
        bucket = self.stats.get(model)
        if bucket is None:
            bucket = empty_stat()
            self.stats[model] = bucket
        return bucket

    def _bump_logical(self, model: str) -> None:
        self._bucket(model)["logical"] += 1

    def _kept_assignment(self, stored: object) -> str | None:
        if not isinstance(stored, str) or not stored.strip():
            return None
        return stored.strip()

    def _by_ordinal(self, ordinal: int) -> str:
        if uses_luna(ordinal, self.percent):
            return LUNA_MODEL
        return PICTURE_MODEL

    def _assign_picture(self, index: int) -> tuple[str, bool, str]:
        picture: dict[str, str] = self.assignments["picture"]
        key = str(int(index))
        stored = self._kept_assignment(picture.get(key))
        if stored == LUNA_MODEL:
            picture[key] = self.selected_model
            return self.selected_model, False, "picture"
        if stored:
            return stored, False, "picture"
        model, kind = self._choose_picture(index)
        picture[key] = model
        self._bump_logical(model)
        return model, True, kind

    def _choose_picture(self, index: int) -> tuple[str, str]:
        if self.percent <= 0:
            if index >= FLASH_INDEX_BASE:
                order: list[str] = self.assignments["flash_order"]
                key = str(int(index))
                if key not in order and self.allow_new_flash:
                    order.append(key)
                return self.selected_model, "flash"
            return self.selected_model, "picture"
        if index >= FLASH_INDEX_BASE:
            order: list[str] = self.assignments["flash_order"]
            key = str(int(index))
            if key in order:
                return self._by_ordinal(order.index(key) + 1), "flash"
            if not self.allow_new_flash:
                # 没有记录的闪帧不再按剩余帧重排，固定退回 GLM。
                return PICTURE_MODEL, "flash"
            order.append(key)
            return self._by_ordinal(len(order)), "flash"
        if index < 1:
            return self.selected_model, "picture"
        return self._by_ordinal(index), "picture"

    def _assign_speech(self, batch_index: int) -> tuple[str, bool]:
        speech: dict[str, str] = self.assignments["speech"]
        key = str(int(batch_index))
        stored = self._kept_assignment(speech.get(key))
        if stored == LUNA_MODEL:
            speech[key] = self.selected_model
            return self.selected_model, False
        if stored:
            return stored, False
        model = self.selected_model if self.percent <= 0 else self._by_ordinal(int(batch_index) + 1)
        speech[key] = model
        self._bump_logical(model)
        return model, True


def initial_route_fields(model: str = PICTURE_MODEL) -> dict[str, Any]:
    return {
        "luna_request_percent": split_percent(model),
        "model_assignments": empty_assignments(),
        "model_stats": {},
    }


async def safe_flush(flush: Callable[[], Awaitable[None]]) -> None:
    try:
        await flush()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("model route flush failed")
