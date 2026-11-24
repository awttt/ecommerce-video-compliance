"""把本机已经生成的图片写到用户在保存对话框里选择的文件。

桌面窗口是 pywebview。页面对 blob 地址调用浏览器下载不会弹出系统保存框，
也不能表示用户取消了。这里只读取本机生成记录里的那一张图，不跟随页面传来的其他地址。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from app.core import direct_image

_LOCAL_FILE = re.compile(
    r"^/v1/image-workbench/jobs/(?P<job>[0-9a-f]{32})/files/(?P<name>image-[1-4]\.(?:png|jpe?g|webp))$"
)
_SUFFIX = {".png": ".png", ".jpg": ".jpg", ".jpeg": ".jpg", ".webp": ".webp"}
_FILTERS = {
    ".png": "PNG (*.png)",
    ".jpg": "JPEG (*.jpg;*.jpeg)",
    ".webp": "WEBP (*.webp)",
}


def parse_local_image_url(url: str) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text or "\\" in text or "\x00" in text:
        return None
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "data:", "file:", "blob:", "//")):
        return None
    path = text.split("#", 1)[0].split("?", 1)[0]
    if not path.startswith("/") or ".." in path.split("/"):
        return None
    match = _LOCAL_FILE.fullmatch(path)
    if match is None:
        return None
    job_id = match.group("job")
    name = match.group("name")
    if direct_image._JOB_ID.fullmatch(job_id) is None or direct_image._FILE_NAME.fullmatch(name) is None:
        return None
    return job_id, name


def safe_download_name(suggested: str, stored_name: str) -> str:
    suffix = _SUFFIX[Path(stored_name).suffix.lower()]
    raw = suggested if isinstance(suggested, str) else ""
    leaf = raw.replace("\\", "/").split("/")[-1]
    leaf = re.sub(r"[\x00-\x1f<>:\"|?*]+", " ", leaf)
    leaf = re.sub(r"\s+", " ", leaf).strip().strip(".")
    stem = Path(leaf).stem.strip().strip(".")[:40] if leaf else ""
    if not stem:
        stem = Path(stored_name).stem
    return f"{stem}{suffix}"


def file_filter_for(filename: str) -> str:
    return _FILTERS[Path(filename).suffix.lower()]


def normalize_dialog_choice(choice: object) -> str | None:
    if choice is None or choice in ("", (), []):
        return None
    if isinstance(choice, str):
        text = choice.strip()
        return text or None
    if isinstance(choice, (list, tuple)) and len(choice) == 1 and isinstance(choice[0], str):
        text = choice[0].strip()
        return text or None
    raise ValueError("保存位置无效")


def _result(ok: bool, message: str, *, cancelled: bool = False, filename: str = "", count: int = 0) -> dict:
    return {
        "ok": ok,
        "cancelled": cancelled,
        "message": message,
        "bytes": count,
        "filename": filename,
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _destination(selected: str, stored_suffix: str) -> Path:
    suffix = _SUFFIX.get(stored_suffix)
    if suffix is None:
        raise ValueError("图片格式不受支持")
    raw = Path(selected)
    if not raw.is_absolute():
        raise ValueError("保存位置无效")
    if _not_a_regular_file(raw):
        raise ValueError("保存位置无效")
    if raw.suffix.lower() not in {suffix, stored_suffix}:
        raw = raw.with_suffix(suffix)
    elif raw.suffix.lower() == ".jpeg":
        raw = raw.with_suffix(".jpg")
    parent = raw.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("保存位置无效")
    if _not_a_regular_file(raw):
        raise ValueError("保存位置无效")
    return raw


def _not_a_regular_file(path: Path) -> bool:
    if path.is_symlink():
        return True
    return path.exists() and not path.is_file()


def _write_bytes(path: Path, payload: bytes) -> None:
    parent = path.parent
    temporary = parent / f".{path.name}.{os.getpid()}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        written = 0
        view = memoryview(payload)
        while written < len(payload):
            chunk = os.write(descriptor, view[written:])
            if chunk <= 0:
                raise OSError("short write")
            written += chunk
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_generated_image(
    url: str,
    filename: str,
    *,
    choose_path: Callable[[str], object],
) -> dict:
    parsed = parse_local_image_url(url)
    if parsed is None:
        return _result(False, "只能保存本机已经生成的图片")
    job_id, stored_name = parsed
    found = direct_image.read_saved_file(job_id, stored_name)
    if found is None:
        return _result(False, "图片不存在或已删除")
    payload, _media = found
    if not payload:
        return _result(False, "图片内容是空的")
    suggested = safe_download_name(filename, stored_name)
    try:
        choice = choose_path(suggested)
    except Exception:
        return _result(False, "保存对话框没有打开")
    try:
        selected = normalize_dialog_choice(choice)
        if selected is None:
            return _result(False, "", cancelled=True)
        destination = _destination(selected, Path(stored_name).suffix.lower())
    except ValueError as exc:
        return _result(False, str(exc))
    root = direct_image.image_jobs_root().resolve()
    if _inside(destination, root):
        return _result(False, "不能保存到生图记录目录")
    try:
        _write_bytes(destination, payload)
        written = destination.read_bytes()
    except OSError:
        return _result(False, "没有写到所选位置")
    if written != payload:
        return _result(False, "保存后的文件和原图不一致")
    return _result(True, "", filename=destination.name, count=len(written))
