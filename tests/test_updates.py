import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.updates import Release, UpdateCenter, UpdateError, parse_manifest, verify_file, version_key, write_helper
from app.factory import create_app


def _release(version: str = "0.2.0") -> Release:
    return Release(version, "a" * 64, f"VideoCompliance-{version}-Setup.exe")


def test_version_order_and_manifest_shape():
    assert version_key("0.1.10") > version_key("0.1.2")
    assert version_key("1.0.0") > version_key("0.9.9")
    with pytest.raises(UpdateError):
        version_key("01.2.3")
    release = parse_manifest("version=0.2.0\nsha256=" + "ab" * 32 + "\nfile=VideoCompliance-0.2.0-Setup.exe\n")
    assert release.url == "https://token4u.cc/download/VideoCompliance-0.2.0-Setup.exe"
    with pytest.raises(UpdateError):
        parse_manifest("version=0.2.0\nsha256=" + "ab" * 32 + "\nfile=../VideoCompliance-0.2.0-Setup.exe\n")
    with pytest.raises(UpdateError):
        parse_manifest("version=0.2.0\nsha256=" + "ab" * 32 + "\nfile=VideoCompliance-0.3.0-Setup.exe\n")


def test_checksum_mismatch_removes_the_file(tmp_path: Path):
    path = tmp_path / "VideoCompliance-0.2.0-Setup.exe"
    path.write_bytes(b"not-the-installer")
    with pytest.raises(UpdateError, match="校验失败"):
        verify_file(path, "a" * 64)
    assert not path.exists()


def test_helper_installs_silently_then_reopens(tmp_path: Path):
    installer = tmp_path / "VideoCompliance-0.2.0-Setup.exe"
    installer.write_bytes(b"setup")
    program = tmp_path / "VideoCompliance.exe"
    script = write_helper(installer, program)
    text = script.read_text(encoding="utf-8")
    assert "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" in text
    assert str(program) in text
    assert script.name == "apply-update.cmd"


def test_dev_build_refuses_to_install_and_reports_status(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EVC_DATA_DIR", str(tmp_path))
    from app.core import updates
    from app.paths import reset_paths_cache

    reset_paths_cache()
    center = UpdateCenter()
    center._release = _release()
    center._available = True
    monkeypatch.setattr(updates, "_center", center)
    with pytest.raises(UpdateError, match="Windows"):
        center.start(busy=False)
    with pytest.raises(UpdateError, match="识别"):
        center.start(busy=True)
    with TestClient(create_app()) as client:
        status = client.get("/api/update")
        assert status.status_code == 200
        body = status.json()
        assert body["current"]
        assert body["can_apply"] is False
        denied = client.post("/api/update")
        assert denied.status_code == 409
        assert "Windows" in denied.json()["detail"]


def test_matching_checksum_keeps_the_file(tmp_path: Path):
    path = tmp_path / "ok.exe"
    payload = b"installer-bytes"
    path.write_bytes(payload)
    verify_file(path, hashlib.sha256(payload).hexdigest())
    assert path.read_bytes() == payload
