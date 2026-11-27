"""桌面端把已经生成的图片保存到用户选择的文件。"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.console import create_console_app
from app.core.direct_image import store_result_images
from app.core.desktop_save import DesktopApi, create_desktop_window
from app.core.image_save import _write_bytes, save_generated_image

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JOB_ID = "a" * 32
# pywebview 5.4 只接受这种过滤器写法。写错时保存对话框会直接抛错。
_FILTER = r"^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*\.\w+)*)\)$"


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("app.core.direct_image.image_jobs_root", lambda: tmp_path / "jobs")
    images = store_result_images(JOB_ID, ["data:image/png;base64," + base64.b64encode(PNG).decode()])
    return images[0]["url"]


def test_save_dialog_writes_the_complete_png(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    destination = tmp_path / "Pictures"
    destination.mkdir()
    seen: dict[str, str] = {}

    def choose(suggested: str) -> str:
        seen["suggested"] = suggested
        return str(destination / "picked.webp")

    result = save_generated_image(url + "?access_token=sk-secret", "..\\礼盒/主图.png", choose_path=choose)

    assert result["ok"] is True
    assert result["cancelled"] is False
    assert result["bytes"] == len(PNG)
    assert result["filename"].endswith(".png")
    assert "/" not in result["filename"] and "\\" not in result["filename"]
    saved = destination / result["filename"]
    assert saved.read_bytes() == PNG
    assert saved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "sk-secret" not in str(result)
    assert str(tmp_path) not in str(result)
    assert seen["suggested"].endswith(".png")
    assert ".." not in seen["suggested"]


def test_cancel_does_not_write_or_report_success(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    pictures = tmp_path / "Pictures"
    pictures.mkdir()

    for choice in (None, "", (), []):
        result = save_generated_image(url, "礼盒-1.png", choose_path=lambda _name: choice)
        assert result["ok"] is False
        assert result["cancelled"] is True
        assert result["bytes"] == 0
        assert list(pictures.iterdir()) == []


def test_untrusted_targets_never_open_a_dialog(tmp_path):
    def choose(_name: str) -> str:
        raise AssertionError("dialog opened")

    for url in (
        "https://token4u.cc/image.png",
        "http://127.0.0.1/v1/image-workbench/jobs/" + JOB_ID + "/files/image-1.png",
        "data:image/png;base64,aaaa",
        "file:///etc/passwd",
        f"/v1/image-workbench/jobs/{JOB_ID}/files/../../image-1.png",
        f"/v1/image-workbench/jobs/{JOB_ID}/files/image-1.svg",
        f"/v1/image-workbench/jobs/{JOB_ID}/files/mock-1.svg",
        "blob:http://127.0.0.1/abcd",
    ):
        result = save_generated_image(url, "a.png", choose_path=choose)
        assert result == {
            "ok": False,
            "cancelled": False,
            "message": "只能保存本机已经生成的图片",
            "bytes": 0,
            "filename": "",
        }


def test_missing_file_is_an_error_before_the_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.direct_image.image_jobs_root", lambda: tmp_path / "jobs")
    url = f"/v1/image-workbench/jobs/{JOB_ID}/files/image-1.png"

    def choose(_name: str) -> str:
        raise AssertionError("dialog opened")

    result = save_generated_image(url, "礼盒-1.png", choose_path=choose)
    assert result["ok"] is False
    assert result["cancelled"] is False
    assert result["message"] == "图片不存在或已删除"


def test_dialog_failure_does_not_leak_the_exception(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)

    def choose(_name: str) -> str:
        raise RuntimeError("Bearer sk-secret-token")

    result = save_generated_image(url, "礼盒-1.png", choose_path=choose)
    assert result["ok"] is False
    assert result["cancelled"] is False
    assert result["message"] == "保存对话框没有打开"
    assert "sk-" not in str(result)


def test_refuses_symlink_relative_and_record_directory(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    link = tmp_path / "link.png"
    link.symlink_to(outside)
    linked_dir = tmp_path / "linked"
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    jobs = tmp_path / "jobs"

    cases = {
        str(link): "保存位置无效",
        "relative.png": "保存位置无效",
        str(tmp_path): "保存位置无效",
        str(linked_dir / "a.png"): "保存位置无效",
        str(jobs / JOB_ID / "image-1.png"): "不能保存到生图记录目录",
    }
    for choice, message in cases.items():
        result = save_generated_image(url, "礼盒-1.png", choose_path=lambda _name, choice=choice: choice)
        assert result["ok"] is False
        assert result["cancelled"] is False
        assert result["message"] == message
    assert outside.read_bytes() == b"keep"
    assert list(real_dir.iterdir()) == []
    assert (jobs / JOB_ID / "image-1.png").read_bytes() == PNG


def test_desktop_api_uses_the_save_dialog_and_not_a_browser_download(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    pictures = tmp_path / "Pictures"
    pictures.mkdir()
    seen: dict[str, object] = {}

    class FakeWindow:
        def create_file_dialog(self, dialog_type, directory="", allow_multiple=False, save_filename="", file_types=()):
            seen["dialog_type"] = dialog_type
            seen["directory"] = directory
            seen["allow_multiple"] = allow_multiple
            seen["save_filename"] = save_filename
            seen["file_types"] = file_types
            return str(pictures / "from-dialog.png")

    monkeypatch.setattr("app.core.desktop_save._save_window", lambda: FakeWindow())
    result = DesktopApi().save_generated_image(url, "夜景-1.png")

    assert result["ok"] is True
    assert result["bytes"] == len(PNG)
    assert (pictures / result["filename"]).read_bytes() == PNG
    assert seen["dialog_type"] == 30
    assert seen["directory"] == ""
    assert seen["allow_multiple"] is False
    assert str(seen["save_filename"]).endswith(".png")
    assert len(seen["file_types"]) == 1
    import re

    assert re.search(_FILTER, seen["file_types"][0])


def test_desktop_window_exposes_the_save_api():
    seen: dict[str, object] = {}

    class FakeWebview:
        def create_window(self, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return "window"

    window = create_desktop_window(FakeWebview(), "http://127.0.0.1:8780/?sso=1")
    assert window == "window"
    assert seen["args"][1] == "http://127.0.0.1:8780/?sso=1"
    api = seen["kwargs"]["js_api"]
    assert isinstance(api, DesktopApi)
    assert callable(api.save_generated_image)


def test_console_serves_the_saved_image_without_calling_the_cloud(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"cloud was called: {request.method} {request.url}")

    app = create_console_app(cloud="https://admin.example.test", dist=dist, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get(url, headers={"Authorization": "Bearer sk-should-not-leave"})
        attachment = client.get(url + "?download=1")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"].startswith("image/png")
    assert "sk-should-not-leave" not in response.text
    assert attachment.content == PNG
    assert attachment.headers["content-disposition"].startswith("attachment;")


def test_gtk_style_dialog_tuple_is_accepted(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    pictures = tmp_path / "Pictures"
    pictures.mkdir()
    target = pictures / "tuple.png"
    result = save_generated_image(url, "礼盒-1.png", choose_path=lambda _name: (str(target),))
    assert result["ok"] is True
    assert target.read_bytes() == PNG


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_destination_is_an_error(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        result = save_generated_image(url, "礼盒-1.png", choose_path=lambda _name: str(locked / "a.png"))
    finally:
        os.chmod(locked, 0o755)
    assert result["ok"] is False
    assert result["cancelled"] is False
    assert result["message"] == "没有写到所选位置"
    assert not (locked / "a.png").exists()


def test_open_flags_include_binary_mode_when_the_platform_has_it(tmp_path, monkeypatch):
    binary = 0x8000
    monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
    real_open = os.open
    opened: list[int] = []

    def spy_open(path, flags, mode=0o777, *, dir_fd=None):
        opened.append(flags)
        flags &= ~binary
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", spy_open)
    destination = tmp_path / "out.png"
    _write_bytes(destination, PNG)

    assert opened
    assert all(flags & binary for flags in opened)
    assert destination.read_bytes() == PNG


def test_mismatched_write_is_rejected_and_leaves_no_file(tmp_path, monkeypatch):
    url = _store(tmp_path, monkeypatch)
    pictures = tmp_path / "Pictures"
    pictures.mkdir()
    target = pictures / "picked.png"
    real_write = os.write

    def write_like_windows_text_mode(descriptor, data):
        raw = bytes(data)
        if not raw or raw not in PNG:
            return real_write(descriptor, data)
        translated = raw.replace(b"\n", b"\r\n")
        if translated == raw:
            translated = raw + b"\x00"
        pending = translated
        while pending:
            count = real_write(descriptor, pending)
            if count <= 0:
                raise OSError("short write")
            pending = pending[count:]
        return len(raw)

    monkeypatch.setattr(os, "write", write_like_windows_text_mode)

    with pytest.raises(ValueError, match="^保存后的文件和原图不一致$"):
        _write_bytes(target, PNG)
    assert not target.exists()
    assert list(pictures.iterdir()) == []

    result = save_generated_image(url, "礼盒-1.png", choose_path=lambda _name: str(target))

    assert result["ok"] is False
    assert result["cancelled"] is False
    assert result["message"] == "保存后的文件和原图不一致"
    assert result["bytes"] == 0
    assert result["filename"] == ""
    assert not target.exists()
    assert list(pictures.iterdir()) == []
