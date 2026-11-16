"""日志落到数据目录，避免把 API Key 打进日志。"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

from app.paths import logs_dir

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{4,}|Bearer\s+\S+|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}|AKID[0-9A-Za-z]{8,}|Credential=[^,\s]+|Signature=[0-9a-fA-F]+)",
    re.IGNORECASE,
)
_configured = False


def redact(text: str) -> str:
    return _SECRET.sub("***", text or "")


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def ensure_stdio() -> None:
    """打包成无控制台窗口时，sys.stdout 和 sys.stderr 是 None。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = open(logs_dir() / "startup.log", "a", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    level_name = os.environ.get("EVC_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact_filter = _RedactFilter()

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redact_filter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        logs_dir() / "app.log",
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact_filter)
    root.addHandler(file_handler)
    _configured = True


def reset_logging() -> None:
    global _configured
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    _configured = False
