import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "evc_numpy_bundle",
    ROOT / "packaging" / "evc_numpy_bundle.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
drop_numpy_tests = _MODULE.drop_numpy_tests
is_numpy_tests_path = _MODULE.is_numpy_tests_path


def test_numpy_test_packages_match_and_runtime_modules_do_not():
    assert is_numpy_tests_path("numpy.typing.tests")
    assert is_numpy_tests_path("numpy.typing.tests.test_typing")
    assert is_numpy_tests_path("numpy.core.tests")
    assert is_numpy_tests_path("numpy._core.tests.test_multiarray")
    assert is_numpy_tests_path("numpy.testing.tests")
    assert is_numpy_tests_path("numpy/typing/tests/test_typing.py")
    assert is_numpy_tests_path("numpy\\typing\\tests\\data\\pass\\example.py")
    assert is_numpy_tests_path(
        "C:/Python312/Lib/site-packages/numpy/typing/tests/test_runtime.py"
    )
    assert is_numpy_tests_path("numpy/typing/tests")

    assert not is_numpy_tests_path("numpy")
    assert not is_numpy_tests_path("numpy.testing")
    assert not is_numpy_tests_path("numpy.testing._private.utils")
    assert not is_numpy_tests_path("numpy.core._multiarray_umath")
    assert not is_numpy_tests_path("numpy/testing/__init__.py")
    assert not is_numpy_tests_path("numpy/_core/_multiarray_umath.pyd")
    assert not is_numpy_tests_path("numpy.libs/libopenblas.dll")
    assert not is_numpy_tests_path("uvicorn.loops.auto")
    assert not is_numpy_tests_path("app.core.pipeline")


def test_drop_numpy_tests_keeps_runtime_entries():
    entries = [
        ("numpy/typing/tests/test_typing.py", "/src/numpy/typing/tests/test_typing.py", "DATA"),
        ("numpy/testing/__init__.py", "/src/numpy/testing/__init__.py", "PYMODULE"),
        ("numpy/_core/_multiarray_umath.pyd", "/src/numpy/_core/_multiarray_umath.pyd", "BINARY"),
        ("/src/numpy/random/tests/data/data.npz", "numpy/random/tests/data", "DATA"),
    ]
    kept = drop_numpy_tests(entries)
    assert kept == [entries[1], entries[2]]


def test_packaging_filters_numpy_tests_before_collect():
    spec = (ROOT / "packaging" / "evc.spec").read_text(encoding="utf-8")
    hook = (ROOT / "packaging" / "hooks" / "hook-numpy.py").read_text(encoding="utf-8")
    assert "drop_numpy_tests" in spec
    assert "is_numpy_tests_path" in hook
    assert "evc_numpy_bundle.py" in spec
    assert "evc_numpy_bundle.py" in hook
    assert "numpy.testing" in spec
    assert "numpy.testing.tests" in spec


def test_windows_workflow_uploads_only_the_setup_artifact():
    text = (ROOT / ".github/workflows/windows-package.yml").read_text(encoding="utf-8")
    assert "name: Upload package" not in text
    assert "name: VideoCompliance-windows\n" not in text
    assert text.count("name: VideoCompliance-windows-setup") == 3
    assert text.count("actions/upload-artifact@v4") == 1
    assert text.count("actions/download-artifact@v4") == 2
    assert text.count("retention-days:") == 1
    assert "retention-days: 3" in text


def test_windows_workflow_caches_downloads_not_pyinstaller_output():
    text = (ROOT / ".github/workflows/windows-package.yml").read_text(encoding="utf-8")
    assert "cache: pip" in text
    assert "cache-dependency-path: requirements.txt" in text
    assert "cache: npm" in text
    assert "frontend/package-lock.json" in text
    assert "admin/web/package-lock.json" in text
    assert text.count("uses: actions/cache@v4") == 2
    assert "key: ffmpeg-${{ runner.os }}-${{ env.FFMPEG_VERSION }}" in text
    assert "key: innosetup-${{ runner.os }}-${{ env.INNO_SETUP_VERSION }}" in text
    assert "path: .cache/ffmpeg" in text
    assert "path: .cache/innosetup" in text
    cache_paths = [
        line.strip().split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("path: .cache/")
    ]
    assert cache_paths == [".cache/ffmpeg", ".cache/innosetup"]
    assert "path: build" not in text
    assert "path: dist/VideoCompliance\n" not in text
    assert "FFMPEG_RELEASE: \"autobuild-2026-11-22-12-36\"" in text
    assert "INNO_SETUP_VERSION: \"6.7.1\"" in text
    build = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "--clean" in build
