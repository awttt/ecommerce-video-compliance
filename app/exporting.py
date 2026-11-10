"""导出 JSON / Excel。结果表包含约定的中文字段，其中「来源」区分画面和口播。"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.core.timeutil import format_clock
from app.paths import exports_dir, safe_filename
from app.schemas import Finding

_COLUMNS = ("时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注")


def export_file(job_id: str, filename: str, suffix: str) -> Path:
    stem = Path(safe_filename(filename)).stem or "video"
    return exports_dir() / f"{job_id}_{stem}_合规结果.{suffix}"


def write_json(path: Path, filename: str, duration_sec: float | None, fps: float, findings: list[Finding]) -> None:
    payload = {
        "视频": filename,
        "时长": format_clock(int(round(duration_sec or 0))),
        "抽帧FPS": fps,
        "结果": [item.export_dict() for item in findings],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_xlsx(path: Path, filename: str, duration_sec: float | None, fps: float, findings: list[Finding]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "合规结果"
    sheet.append(list(_COLUMNS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for item in findings:
        row = item.export_dict()
        sheet.append([row[name] for name in _COLUMNS])
    for index, width in enumerate((18, 10, 10, 24, 28, 10, 42, 20), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:H{max(1, sheet.max_row)}"

    info = book.create_sheet("任务信息")
    info.append(["视频", filename])
    info.append(["时长", format_clock(int(round(duration_sec or 0)))])
    info.append(["抽帧FPS", fps])
    info.append(["命中条数", len(findings)])
    info.column_dimensions["A"].width = 16
    info.column_dimensions["B"].width = 48
    book.save(path)
