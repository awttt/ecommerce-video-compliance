"""Decide which NumPy files are test-only and should stay out of the installer.

NumPy ships its test suite inside the wheel (for example ``numpy/typing/tests``).
Those modules are not imported at runtime. ``numpy.testing`` is a different
package: application and library code can import it, so it stays.
"""

from __future__ import annotations


def is_numpy_tests_path(name: str) -> bool:
    """True when ``name`` is a NumPy test module or a file inside one.

    A path or dotted name matches only when a component is exactly ``numpy``
    and a later component is exactly ``tests``. ``numpy.testing`` does not match.
    """
    text = str(name).replace("\\", "/").rstrip("/")
    if text.endswith((".pyc", ".pyo")):
        text = text.rsplit(".", 1)[0]
    elif text.endswith(".py"):
        text = text[:-3]
    parts = [part for part in text.split("/") if part]
    if len(parts) == 1:
        parts = [part for part in parts[0].split(".") if part]
    for index, part in enumerate(parts):
        if part == "numpy" and "tests" in parts[index + 1 :]:
            return True
    return False


def drop_numpy_tests(entries):
    """Return TOC or data entries that are not NumPy test files."""
    kept = []
    for entry in entries:
        fields = entry[:2]
        if any(is_numpy_tests_path(str(field)) for field in fields):
            continue
        kept.append(entry)
    return kept
