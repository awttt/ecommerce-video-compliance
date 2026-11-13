"""导出 JSON / Excel。结果表包含约定的中文字段，其中「来源」区分画面和口播。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.timeutil import format_clock
from app.paths import exports_dir, safe_filename
from app.schemas import Finding

_COLUMNS = ("时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注")
_BATCH_COLUMNS = ("视频",) + _COLUMNS
_STATUS_LABEL = {
    "queued": "排队中",
    "running": "识别中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


@dataclass(frozen=True)
class BatchExportJob:
    filename: str
    duration_sec: float | None
    fps: float
    status: str
    findings: list[Finding]


def export_file(job_id: str, filename: str, suffix: str) -> Path:
    stem = Path(safe_filename(filename)).stem or "video"
    return exports_dir() / f"{job_id}_{stem}_合规结果.{suffix}"


def batch_export_file(count: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return exports_dir() / f"批量{count}个视频_{stamp}_合规结果.xlsx"


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


def write_batch_xlsx(path: Path, jobs: list[BatchExportJob]) -> None:
    """多个视频合成一个工作簿。结果表在原有 8 列前加「视频」，未完成的任务只出现在任务信息里。"""
    book = Workbook()
    sheet = book.active
    sheet.title = "合规结果"
    _write_header(sheet, _BATCH_COLUMNS)
    for job in jobs:
        if job.status != "done":
            continue
        for item in job.findings:
            row = item.export_dict()
            sheet.append([job.filename, *[row[name] for name in _COLUMNS]])
    _finish_table(sheet, len(_BATCH_COLUMNS))
    _size_columns(sheet, (36, 18, 10, 10, 24, 28, 10, 42, 20))

    info = book.create_sheet("任务信息")
    _write_header(info, ("视频", "时长", "抽帧FPS", "命中条数", "状态"))
    for job in jobs:
        info.append([
            job.filename,
            format_clock(int(round(job.duration_sec or 0))),
            job.fps,
            len(job.findings) if job.status == "done" else 0,
            _STATUS_LABEL.get(job.status, job.status),
        ])
    _finish_table(info, 5)
    _size_columns(info, (36, 14, 12, 12, 12))
    book.save(path)


def _write_header(sheet: Worksheet, columns: tuple[str, ...]) -> None:
    sheet.append(list(columns))
    for cell in sheet[1]:
        cell.font = Font(bold=True)


def _finish_table(sheet: Worksheet, column_count: int) -> None:
    last = get_column_letter(column_count)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{last}{max(1, sheet.max_row)}"


def _size_columns(sheet: Worksheet, widths: tuple[int, ...]) -> None:
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
