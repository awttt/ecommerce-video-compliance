"""数据目录与路径。开发时用项目 data/，打包后用系统用户目录。"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_DATA_DIR: Path | None = None

_RESERVED_WINDOWS = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_root() -> Path:
    """打包后的只读资源目录。开发时就是项目根目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return project_root()


def platform_user_data() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "EcommerceVideoCompliance"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EcommerceVideoCompliance"
    return Path.home() / ".local" / "share" / "ecommerce-video-compliance"


def data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        override = os.environ.get("EVC_DATA_DIR", "").strip()
        if override:
            path = Path(override).expanduser()
        elif is_frozen():
            path = platform_user_data()
        else:
            path = project_root() / "data"
        path.mkdir(parents=True, exist_ok=True)
        _DATA_DIR = path
    return _DATA_DIR


def reset_paths_cache() -> None:
    global _DATA_DIR
    _DATA_DIR = None


def video_dir(job_id: str) -> Path:
    return data_dir() / "videos" / job_id


def frames_dir(job_id: str) -> Path:
    return data_dir() / "frames" / job_id


def audio_dir(job_id: str) -> Path:
    return data_dir() / "audio" / job_id


def exports_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def safe_filename(name: str) -> str:
    """保留中文，去掉路径和 Windows 非法字符。"""
    cleaned = (name or "").replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace("\x00", "").strip().strip(".")
    cleaned = re.sub(r'[<>:"|?*]', "_", cleaned)
    if cleaned in {"", ".", ".."}:
        return "video.mp4"
    stem = Path(cleaned).stem.upper()
    if stem in _RESERVED_WINDOWS:
        cleaned = f"_{cleaned}"
    return cleaned


def load_dotenv(path: Path) -> None:
    """最小 .env 读取。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_project_dotenv() -> None:
    load_dotenv(project_root() / ".env")
    load_dotenv(Path.cwd() / ".env")
