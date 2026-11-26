"""检查 token4u.cc/download 上的 Windows 安装包，并在本机覆盖安装。

下载站在每次发布后写一份固定的 latest.manifest。客户端只认这个文件里的
版本号、文件名和 sha256，地址始终拼在 https://token4u.cc/download/ 后面。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from app import __version__

logger = logging.getLogger("app.updates")

DOWNLOAD_ORIGIN = "https://token4u.cc"
DOWNLOAD_BASE = f"{DOWNLOAD_ORIGIN}/download"
LATEST_URL = f"{DOWNLOAD_BASE}/latest.manifest"
USER_AGENT = f"ecommerce-video-compliance/{__version__}"
MAX_INSTALLER_BYTES = 800 * 1024 * 1024
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_FILE = re.compile(r"^VideoCompliance-(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-Setup\.exe$")


class UpdateError(Exception):
    pass


class Release:
    def __init__(self, version: str, sha256: str, filename: str) -> None:
        self.version = version
        self.sha256 = sha256
        self.filename = filename

    @property
    def url(self) -> str:
        return f"{DOWNLOAD_BASE}/{self.filename}"


def version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value.strip())
    if match is None:
        raise UpdateError("版本号不正确")
    return tuple(int(part) for part in match.groups())


def parse_manifest(text: str) -> Release:
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip().lower()] = value.strip()
    version = fields.get("version", "")
    digest = fields.get("sha256", "").lower()
    filename = fields.get("file") or f"VideoCompliance-{version}-Setup.exe"
    if not _VERSION.fullmatch(version) or not _SHA.fullmatch(digest) or not _FILE.fullmatch(filename):
        raise UpdateError("更新信息无法识别")
    if filename != f"VideoCompliance-{version}-Setup.exe":
        raise UpdateError("更新信息无法识别")
    return Release(version, digest, filename)


def packaged_windows() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def verify_file(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise UpdateError("安装包校验失败，已取消更新")


def _safe_path(path: Path) -> str:
    text = str(path)
    if any(char in text for char in '"\r\n&|><^'):
        raise UpdateError("安装路径无法用于更新")
    return text


def write_helper(installer: Path, exe: Path) -> Path:
    """安装器会关掉正在运行的程序，所以先退出，再静默覆盖，然后重新打开。"""
    setup = _safe_path(installer)
    program = _safe_path(exe)
    script = installer.with_name("apply-update.cmd")
    script.write_text(
        "\r\n".join(
            [
                "@echo off",
                "ping 127.0.0.1 -n 4 >nul",
                f'"{setup}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
                f'if exist "{program}" start "" "{program}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _allowed_final_url(url: str) -> bool:
    if not url.startswith(f"{DOWNLOAD_BASE}/"):
        return False
    name = url.removeprefix(f"{DOWNLOAD_BASE}/").split("?", 1)[0]
    return _FILE.fullmatch(name) is not None


class UpdateCenter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._release: Release | None = None
        self._available = False
        self._phase = "idle"
        self._received = 0
        self._total = 0
        self._message = ""
        self._check_state = "idle"
        self._check_message = ""

    def snapshot(self) -> dict:
        with self._lock:
            latest = self._release.version if self._release else ""
            return {
                "current": __version__,
                "latest": latest,
                "available": self._available,
                "phase": self._phase,
                "received": self._received,
                "total": self._total,
                "message": self._message,
                "can_apply": packaged_windows(),
                "check_state": self._check_state,
                "check_message": self._check_message,
            }

    def check(self) -> dict:
        """只读取固定的更新清单。正在下载或安装时不再另开一次，也不在这里启动安装。"""
        with self._lock:
            if self._phase in {"downloading", "installing"}:
                self._check_state = "busy"
                self._check_message = "已有更新正在进行"
                return self.snapshot()
        try:
            release = self._fetch_manifest()
        except Exception:
            # 带上异常细节，否则日志里只剩一句「检查更新失败」，分不清是 404 还是网络不通。
            logger.info("检查更新失败", exc_info=True)
            with self._lock:
                if self._phase in {"downloading", "installing"}:
                    self._check_state = "busy"
                    self._check_message = "已有更新正在进行"
                else:
                    self._check_state = "failed"
                    self._check_message = "暂时无法检查更新"
                return self.snapshot()
        try:
            newer = version_key(release.version) > version_key(__version__)
        except UpdateError:
            logger.info("下载站版本号无法比较")
            with self._lock:
                self._check_state = "failed"
                self._check_message = "暂时无法检查更新"
                return self.snapshot()
        with self._lock:
            self._release = release
            self._available = newer
            if self._phase in {"downloading", "installing"}:
                self._check_state = "busy"
                self._check_message = "已有更新正在进行"
            else:
                self._phase = "idle"
                self._message = ""
                self._check_state = "available" if newer else "current"
                self._check_message = ""
            return self.snapshot()

    def start(self, *, busy: bool) -> dict:
        if busy:
            raise UpdateError("还有视频在识别，完成或取消后再更新")
        if not packaged_windows():
            raise UpdateError("只有 Windows 安装版可以在这里更新")
        with self._lock:
            if self._phase in {"downloading", "installing"}:
                return self.snapshot()
            release = self._release
            available = self._available
        if release is None or not available:
            self.check()
            with self._lock:
                release = self._release
                available = self._available
                failed = self._check_state == "failed"
            if failed:
                raise UpdateError("暂时无法检查更新")
        if release is None or not available:
            raise UpdateError("已经是最新版本")
        with self._lock:
            self._phase = "downloading"
            self._received = 0
            self._total = 0
            self._message = "正在下载安装包"
        threading.Thread(target=self._install, args=(release,), name="app-update", daemon=True).start()
        return self.snapshot()

    def _fetch_manifest(self) -> Release:
        import httpx

        try:
            response = httpx.get(
                LATEST_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=15.0,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise UpdateError("暂时无法检查更新") from exc
        if response.status_code == 404:
            raise UpdateError("还没有发布更新")
        if response.status_code != 200:
            raise UpdateError("暂时无法检查更新")
        if len(response.content) > 4096:
            raise UpdateError("更新信息无法识别")
        final = str(response.url)
        if final.split("?", 1)[0] != LATEST_URL:
            raise UpdateError("更新信息无法识别")
        return parse_manifest(response.text)

    def _download(self, release: Release, dest: Path) -> None:
        import httpx

        digest = hashlib.sha256()
        received = 0
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
                with client.stream("GET", release.url) as response:
                    response.raise_for_status()
                    if not _allowed_final_url(str(response.url)):
                        raise UpdateError("下载地址不正确")
                    total = int(response.headers.get("content-length") or 0)
                    if total > MAX_INSTALLER_BYTES:
                        raise UpdateError("安装包过大")
                    with self._lock:
                        self._total = total
                    with dest.open("wb") as handle:
                        for chunk in response.iter_bytes(256 * 1024):
                            received += len(chunk)
                            if received > MAX_INSTALLER_BYTES:
                                raise UpdateError("安装包过大")
                            digest.update(chunk)
                            handle.write(chunk)
                            with self._lock:
                                self._received = received
                                if total <= 0:
                                    self._total = received
        except UpdateError:
            dest.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            dest.unlink(missing_ok=True)
            raise UpdateError("下载安装包失败") from exc
        if digest.hexdigest() != release.sha256:
            dest.unlink(missing_ok=True)
            raise UpdateError("安装包校验失败，已取消更新")

    def _install(self, release: Release) -> None:
        from app.paths import data_dir

        folder = data_dir() / "updates"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / release.filename
            if dest.exists():
                dest.unlink()
            self._download(release, dest)
            verify_file(dest, release.sha256)
            with self._lock:
                self._phase = "installing"
                self._message = "正在安装，程序会关闭并在完成后重新打开"
            helper = write_helper(dest, Path(sys.executable))
            self._hand_off(helper)
        except Exception as exc:
            logger.warning("安装更新失败", exc_info=True)
            message = str(exc) if isinstance(exc, UpdateError) else "更新失败，请稍后再试"
            with self._lock:
                self._phase = "error"
                self._message = message

    def _hand_off(self, helper: Path) -> None:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            ["cmd", "/c", str(helper)],
            cwd=str(helper.parent),
            close_fds=True,
            creationflags=flags,
        )
        os._exit(0)


_center: UpdateCenter | None = None


def get_updates() -> UpdateCenter:
    global _center
    if _center is None:
        _center = UpdateCenter()
    return _center
