"""生图契约。

公开 API 只确认万川有 ``POST /v1/images/generations``，认证为 Bearer，
模型 id 以目录为准。请求体有哪些字段、尺寸、张数、画质、参考图和异步开关，
公开文档都没有给出；参考页当时也没有可用模型，生成、上传和下载都没有跑通。

因此默认契约是「未确认」：不组包、不发请求、不创建生图密钥。
测试可以注入白名单，用来证明没被点名的参数进不了上游请求。
白名单只接受 model 和 prompt 这两个占位字段，尺寸等可见控件仍然只留在本机。

分组名称和渠道备注按运维 bot 创建的命名来填（例如「生图专属-bot创建」）。
名称、编号都只读环境变量。两项都空着则禁用，不会回落到审核分组，也不会直连 chuanapi。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from app.core.relay import RelayError, relay_group_name, resolve_relay_group_id

IMAGE_KEY_NAME = "御镜生图"
IMAGE_SESSION_FIELDS = (
    "image_relay_api_key",
    "image_relay_api_key_id",
    "image_relay_group_id",
)
PREVIEW_MODEL = "preview-mock"
PREVIEW_MODEL_LABEL = "预览模拟（不是上游模型）"
PREVIEW_TOKEN = "preview-local-session"
TOKEN4U_GATEWAY = "https://token4u.cc/v1"
UPSTREAM_FIELDS = frozenset({"model", "prompt"})
FORBIDDEN_CLIENT_FIELDS = frozenset(
    {
        "group",
        "group_id",
        "group_name",
        "base_url",
        "baseurl",
        "api_key",
        "apikey",
        "key",
        "authorization",
        "token",
        "reference_url",
        "image_url",
        "url",
    }
)
SIZES = (
    ("auto", "自动", "auto"),
    ("1024x1024", "1024×1024", "square"),
    ("1536x1024", "1536×1024", "landscape"),
    ("1024x1536", "1024×1536", "portrait"),
)
SIZE_IDS = {item[0]: item for item in SIZES}
COUNTS = (1, 2, 3, 4)
MAX_PROMPT = 2000
MAX_REFERENCES = 4
MAX_REFERENCE_BYTES = 4 * 1024 * 1024
MAX_BATCHES = 120
NOTICE = "尺寸、张数和参考图只记在本机。这些参数还没有公开契约，未确认前不会发给上游。"


class ImageError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class LocalRequest:
    prompt: str
    model: str
    size: str
    count: int
    quality: str


@dataclass(frozen=True)
class PreparedUpstream:
    url: str
    body: dict
    api_key: str


@dataclass
class ImageContract:
    """测试或以后确认的契约。生产默认 confirmed 为 False，parser 为空。"""

    confirmed: bool = False
    fields: frozenset[str] = field(default_factory=frozenset)
    models: tuple[str, ...] = ()
    parser: Callable[[bytes], list[bytes]] | None = None


_contract: ImageContract | None = None
_transport = None


def current_contract() -> ImageContract:
    return _contract if _contract is not None else ImageContract()


def install_contract(contract: ImageContract | None) -> None:
    global _contract
    _contract = contract


def install_transport(transport) -> None:
    global _transport
    _transport = transport


def current_transport():
    return _transport


def reset_image_contract() -> None:
    install_contract(None)
    install_transport(None)


def provider_mode() -> str:
    raw = os.environ.get("EVC_IMAGE_PROVIDER", "").strip().lower()
    if raw == "mock":
        return "mock"
    if raw == "token4u":
        return "token4u"
    return "unconfigured"


def image_group_config() -> tuple[str, int | None]:
    """生图专用分组。空名称和空编号表示未配置。不读取审核分组的环境变量。"""
    name = os.environ.get("EVC_IMAGE_RELAY_GROUP_NAME", "").strip()
    raw = os.environ.get("EVC_IMAGE_RELAY_GROUP_ID", "").strip()
    if not raw:
        return name, None
    if not raw.isdigit() or int(raw) <= 0:
        raise ImageError("生图分组编号配置无效", 400)
    return name, int(raw)


def group_label() -> str:
    mode = provider_mode()
    if mode == "mock":
        return "预览不绑定分组"
    if mode != "token4u":
        return "未配置"
    name, group_id = image_group_config()
    if name:
        return name
    if group_id is not None:
        return "专用分组已配置"
    return "未配置"


def mock_delay() -> float:
    raw = os.environ.get("EVC_IMAGE_MOCK_DELAY", "").strip()
    if not raw:
        return 0.8
    try:
        value = float(raw)
    except ValueError:
        return 0.8
    if value < 0:
        return 0.0
    return min(value, 5.0)


def apply_preview_login(settings: dict) -> bool:
    """只给本机预览打开界面。不替换已有登录，也不在真实模式下生效。"""
    if provider_mode() != "mock":
        return False
    if os.environ.get("EVC_IMAGE_PREVIEW_LOGIN", "").strip() != "1":
        return False
    if str(settings.get("relay_access_token") or "").strip():
        return False
    settings["relay_email"] = "preview@local"
    settings["last_login_email"] = "preview@local"
    settings["relay_access_token"] = PREVIEW_TOKEN
    settings["relay_login_at"] = time.time()
    return True


def clear_image_session(settings: dict) -> None:
    for name in IMAGE_SESSION_FIELDS:
        settings.pop(name, None)


def reject_client_overrides(payload: dict) -> None:
    for key in payload:
        if str(key).strip().lower() in FORBIDDEN_CLIENT_FIELDS:
            raise ImageError("不能指定分组、网关或密钥", 400)


def parse_request(payload: dict, *, mode: str) -> LocalRequest:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ImageError("请先填写提示词", 400)
    if len(prompt) > MAX_PROMPT:
        raise ImageError(f"提示词不能超过 {MAX_PROMPT} 字", 400)
    size = str(payload.get("size") or "auto").strip().lower().replace("×", "x").replace("*", "x")
    if size not in SIZE_IDS:
        raise ImageError("不支持这个尺寸", 400)
    try:
        count = int(payload.get("count") if payload.get("count") is not None else 1)
    except (TypeError, ValueError) as exc:
        raise ImageError("张数必须是 1 到 4", 400) from exc
    if count not in COUNTS:
        raise ImageError("张数必须是 1 到 4", 400)
    quality = str(payload.get("quality") or "auto").strip().lower()
    if quality != "auto":
        raise ImageError("画质只有自动，且不会发给上游", 400)
    if _truthy(payload.get("async_generation")):
        raise ImageError("异步开关尚未列入契约，已拒绝", 400)
    model = str(payload.get("model") or "").strip()
    if mode == "mock":
        if model not in {"", PREVIEW_MODEL}:
            raise ImageError("预览模式只有预览模型", 400)
        model = PREVIEW_MODEL
    elif mode == "token4u":
        contract = current_contract()
        if not contract.confirmed or contract.parser is None:
            raise ImageError("生图上游契约尚未确认，已拒绝调用。", 409)
        if not model or model not in contract.models:
            raise ImageError("这个模型不在已确认清单里", 400)
    else:
        raise ImageError("生图尚未配置，功能已禁用。不会改用审核分组，也不会直连上游。", 409)
    return LocalRequest(prompt=prompt, model=model, size=size, count=count, quality="auto")


def orientation_of(size: str) -> str:
    return SIZE_IDS[size][2]


def size_label(size: str) -> str:
    return SIZE_IDS[size][1]


def resolve_image_group(groups: list[dict]) -> int:
    """只在当前用户可见分组里解析生图分组。审核分组一律拒绝。"""
    name, fallback = image_group_config()
    if not name and fallback is None:
        raise ImageError("生图专用分组尚未配置，功能已禁用。不会改用审核分组，也不会直连上游。", 409)
    detection = relay_group_name()
    if name and name == detection:
        raise ImageError("生图分组不能与审核分组同名，已拒绝。", 409)
    detection_ids: set[int] = set()
    for item in groups:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() != detection:
            continue
        gid = item.get("id")
        if isinstance(gid, int) and gid > 0:
            detection_ids.add(gid)
        elif isinstance(gid, str) and gid.strip().isdigit():
            detection_ids.add(int(gid.strip()))
    try:
        group_id = resolve_relay_group_id(groups, name=name or "生图专用分组未命名", fallback_id=fallback)
    except RelayError as exc:
        raise ImageError("生图专用分组不在当前账号的可用分组中，已拒绝。不会改用审核分组。", 409) from exc
    if group_id in detection_ids:
        raise ImageError("解析到的是审核分组，生图已拒绝。", 409)
    return group_id


def gateway_base() -> str:
    """只允许 token4u 的 /v1。拒绝 chuanapi、其他主机、代理式用户名和非常规路径。"""
    raw = os.environ.get("EVC_IMAGE_GATEWAY_URL", "").strip() or TOKEN4U_GATEWAY
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    if host == "chuanapi.com" or host.endswith(".chuanapi.com"):
        raise ImageError("生图不能直连上游站点，已拒绝。", 409)
    if parts.scheme != "https" or host != "token4u.cc":
        raise ImageError("生图网关只能是 token4u，已拒绝其他地址。", 409)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ImageError("生图网关地址无效。", 409)
    path = parts.path.rstrip("/")
    if path != "/v1":
        raise ImageError("生图网关路径无效。", 409)
    return TOKEN4U_GATEWAY


def prepare_upstream(local: LocalRequest, api_key: str, contract: ImageContract | None = None) -> PreparedUpstream:
    chosen = current_contract() if contract is None else contract
    if not chosen.confirmed or chosen.parser is None:
        raise ImageError("生图上游契约尚未确认，已拒绝调用。", 409)
    if not chosen.fields or not chosen.fields <= UPSTREAM_FIELDS:
        raise ImageError("契约包含尚未确认的参数，已拒绝调用。", 409)
    if not api_key or api_key == PREVIEW_TOKEN:
        raise ImageError("没有可用的用户生图密钥。", 409)
    body: dict[str, str] = {}
    if "model" in chosen.fields:
        body["model"] = local.model
    if "prompt" in chosen.fields:
        body["prompt"] = local.prompt
    return PreparedUpstream(url=f"{gateway_base()}/images/generations", body=body, api_key=api_key)


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


_GLYPHS = {
    " ": ["00000"] * 7,
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
}


def preview_pixel_size(size: str) -> tuple[int, int]:
    return {
        "auto": (480, 480),
        "1024x1024": (480, 480),
        "1536x1024": (576, 384),
        "1024x1536": (384, 576),
    }[size]


def preview_png(size: str, index: int) -> bytes:
    """本地标识图。画面上写明 MOCK PREVIEW，避免被当成上游成片。"""
    import struct
    import zlib

    width, height = preview_pixel_size(size)
    scale = 4 if width >= 480 else 3
    lines = ("MOCK PREVIEW", "NOT UPSTREAM")
    text_height = len(lines) * (7 * scale + scale)
    text_width = max(len(line) for line in lines) * (6 * scale)
    origin_x = max(8, (width - text_width) // 2)
    origin_y = max(8, (height - text_height) // 2)
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            band = (x * 3 + y + index * 40) % 96 < 8
            if band:
                pixels += b"\xb9\xd4\xff"
            else:
                shade = 236 - (y * 18 // height)
                pixels += bytes((shade, shade + 4, 246))
    for line_index, line in enumerate(lines):
        for glyph_index, char in enumerate(line):
            glyph = _GLYPHS.get(char, _GLYPHS[" "])
            left = origin_x + glyph_index * 6 * scale
            top = origin_y + line_index * (8 * scale)
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit != "1":
                        continue
                    for py in range(scale):
                        for px in range(scale):
                            xx = left + gx * scale + px
                            yy = top + gy * scale + py
                            if 0 <= xx < width and 0 <= yy < height:
                                offset = yy * (width * 3 + 1) + 1 + xx * 3
                                pixels[offset : offset + 3] = b"\x1f\x3b\x73"
    raw = zlib.compress(bytes(pixels), 9)
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
