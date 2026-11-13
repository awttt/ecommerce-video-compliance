import json
from io import BytesIO
from urllib.parse import unquote

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.exporting import BatchExportJob, write_batch_xlsx, write_xlsx
from app.factory import create_app
from app.schemas import Finding
from app.services.manager import JobState, get_manager
from tests.test_api import sign_in

DONE = "a" * 32
RUNNING = "b" * 32
EMPTY = "c" * 32
_COLUMNS = ["时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注"]


def _finding(text: str, source: str = "画面") -> Finding:
    return Finding(
        time_range="00:01-00:02",
        category="价格",
        source=source,
        text=text,
        position="画面左下" if source == "画面" else "口播内容",
        confidence="确定",
        reason="画面上写着价格" if source == "画面" else "口播里说出了价格",
        note="",
    )


def _job(job_id: str, filename: str, status: str, findings: list[Finding], duration: float, fps: float) -> JobState:
    return JobState(
        id=job_id,
        filename=filename,
        source_path="",
        status=status,
        stage="done" if status == "done" else "recognize",
        message="",
        progress=100 if status == "done" else 40,
        fps=fps,
        concurrency=1,
        duration_sec=duration,
        frames_estimated=0,
        frames_extracted=0,
        frames_recognized=0,
        failed_frames=0,
        hit_count=len(findings) if status == "done" else 0,
        error=None,
        speech_stage="",
        speech_message="",
        asr_error=None,
        options={},
        output_path=None,
        created_at="2026-11-13T00:00:00+00:00",
        updated_at="2026-11-13T00:00:00+00:00",
        finished_at="2026-11-13T00:00:00+00:00" if status == "done" else None,
        findings=findings,
    )


def _rows(sheet):
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def test_batch_workbook_keeps_the_original_columns_and_skips_unfinished(tmp_path):
    path = tmp_path / "批量.xlsx"
    write_batch_xlsx(path, [
        BatchExportJob("甲.mp4", 65, 1, "done", [_finding("¥199"), _finding("九十九元", "口播")]),
        BatchExportJob("乙.mp4", 10, 2, "running", [_finding("不该出现")]),
        BatchExportJob("丙.mp4", 3, 0.5, "failed", [_finding("也不该出现")]),
    ])
    book = load_workbook(path)
    assert book.sheetnames == ["合规结果", "任务信息"]
    result = _rows(book["合规结果"])
    assert result[0] == ["视频", *_COLUMNS]
    assert result[1][0] == "甲.mp4"
    assert [cell or "" for cell in result[1][1:]] == ["00:01-00:02", "价格", "画面", "¥199", "画面左下", "确定", "画面上写着价格", ""]
    assert result[2][2:5] == ["价格", "口播", "九十九元"]
    assert len(result) == 3
    assert book["合规结果"].freeze_panes == "A2"
    assert book["合规结果"].auto_filter.ref == "A1:I3"
    assert book["合规结果"]["A1"].font.bold is True

    info = _rows(book["任务信息"])
    assert info[0] == ["视频", "时长", "抽帧FPS", "命中条数", "状态"]
    assert info[1:] == [
        ["甲.mp4", "01:05", 1, 2, "已完成"],
        ["乙.mp4", "00:10", 2, 0, "识别中"],
        ["丙.mp4", "00:03", 0.5, 0, "失败"],
    ]
    assert book["任务信息"].auto_filter.ref == "A1:E4"

    single = tmp_path / "单个.xlsx"
    write_xlsx(single, "甲.mp4", 65, 1, [_finding("¥199")])
    single_book = load_workbook(single)
    assert [cell.value for cell in single_book.active[1]] == _COLUMNS


def test_batch_export_api_merges_finished_jobs_only():
    with TestClient(create_app()) as client:
        denied = client.post("/api/jobs/export", json={"ids": [DONE, RUNNING]})
        assert denied.status_code == 401

        sign_in()
        manager = get_manager()
        manager._jobs[DONE] = _job(DONE, "甲.mp4", "done", [_finding("¥199")], 65, 1)
        manager._jobs[RUNNING] = _job(RUNNING, "乙.mp4", "running", [_finding("不该出现")], 10, 2)
        manager._jobs[EMPTY] = _job(EMPTY, "丙.mp4", "done", [], 3, 0.5)

        one = client.post("/api/jobs/export", json={"ids": [DONE]})
        assert one.status_code == 400
        assert one.json()["detail"] == "批量导出至少需要 2 个视频"
        same = client.post("/api/jobs/export", json={"ids": [DONE, DONE]})
        assert same.status_code == 400

        pending = client.post("/api/jobs/export", json={"ids": [RUNNING, "d" * 32]})
        assert pending.status_code == 404

        manager._jobs["e" * 32] = _job("e" * 32, "丁.mp4", "queued", [], 1, 1)
        unfinished = client.post("/api/jobs/export", json={"ids": [RUNNING, "e" * 32]})
        assert unfinished.status_code == 409
        assert unfinished.json()["detail"] == "这批视频还没有检测完成，不能导出"

        too_many = client.post("/api/jobs/export", json={"ids": [f"{index:032x}" for index in range(21)]})
        assert too_many.status_code == 422

        exported = client.post("/api/jobs/export", json={"ids": [DONE, RUNNING, DONE, EMPTY]})
        assert exported.status_code == 200, exported.text
        assert exported.content[:2] == b"PK"
        disposition = unquote(exported.headers["content-disposition"])
        assert "批量2个视频_" in disposition
        assert disposition.endswith("合规结果.xlsx") or "合规结果.xlsx" in disposition
        book = load_workbook(BytesIO(exported.content))
        result = _rows(book["合规结果"])
        assert result[0] == ["视频", *_COLUMNS]
        assert [row[0] for row in result[1:]] == ["甲.mp4"]
        assert "不该出现" not in json.dumps(result, ensure_ascii=False)
        info = _rows(book["任务信息"])
        assert [row[0] for row in info[1:]] == ["甲.mp4", "乙.mp4", "丙.mp4"]
        assert [row[3] for row in info[1:]] == [1, 0, 0]
        assert [row[4] for row in info[1:]] == ["已完成", "识别中", "已完成"]

        single = client.get(f"/api/jobs/{DONE}/export", params={"fmt": "xlsx"})
        assert single.status_code == 200
        single_book = load_workbook(BytesIO(single.content))
        assert [cell.value for cell in single_book.active[1]] == _COLUMNS
