"""SQLite 历史。连接只在 asyncio 循环里使用。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite

from app.core.asr_engine import configured_engine
from app.core.relay import configured_model
from app.core.rule_catalog import apply_builtin_rules, enable_exaggeration
from app.schemas import DEFAULT_SETTINGS, DetectionRules, Finding, clamp_concurrency

log = logging.getLogger(__name__)


def _optional_float(row, name: str) -> float | None:
    if name not in row.keys():
        return None
    value = row[name]
    if value is None:
        return None
    return float(value)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    fps REAL NOT NULL,
    concurrency INTEGER NOT NULL,
    duration_sec REAL,
    frames_estimated INTEGER NOT NULL DEFAULT 0,
    frames_extracted INTEGER NOT NULL DEFAULT 0,
    frames_recognized INTEGER NOT NULL DEFAULT 0,
    failed_frames INTEGER NOT NULL DEFAULT 0,
    failed_frames_json TEXT NOT NULL DEFAULT '[]',
    hit_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    speech_stage TEXT NOT NULL DEFAULT '',
    speech_message TEXT NOT NULL DEFAULT '',
    asr_error TEXT,
    options_json TEXT NOT NULL DEFAULT '{}',
    output_path TEXT,
    edit_plan TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    started_at TEXT,
    paused_total_sec REAL NOT NULL DEFAULT 0,
    hold_sec REAL NOT NULL DEFAULT 0,
    paused_at TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    time_range TEXT NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '画面',
    text TEXT NOT NULL,
    position TEXT NOT NULL,
    confidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id, start_sec);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_JOB_FIELDS = (
    "id",
    "filename",
    "source_path",
    "status",
    "stage",
    "message",
    "progress",
    "fps",
    "concurrency",
    "duration_sec",
    "frames_estimated",
    "frames_extracted",
    "frames_recognized",
    "failed_frames",
    "failed_frames_json",
    "hit_count",
    "error",
    "speech_stage",
    "speech_message",
    "asr_error",
    "options_json",
    "output_path",
    "edit_plan",
    "created_at",
    "updated_at",
    "finished_at",
    "started_at",
    "paused_total_sec",
    "hold_sec",
    "paused_at",
)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._ensure_columns()
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _ensure_columns(self) -> None:
        await self._add_column("jobs", "speech_stage", "TEXT NOT NULL DEFAULT ''")
        await self._add_column("jobs", "speech_message", "TEXT NOT NULL DEFAULT ''")
        await self._add_column("jobs", "asr_error", "TEXT")
        await self._add_column("findings", "source", "TEXT NOT NULL DEFAULT '画面'")
        await self._add_column("findings", "sentence_start", "REAL")
        await self._add_column("findings", "sentence_end", "REAL")
        await self._add_column("findings", "next_start", "REAL")
        await self._add_column("jobs", "edit_plan", "TEXT")
        await self._add_column("jobs", "started_at", "TEXT")
        await self._add_column("jobs", "paused_total_sec", "REAL NOT NULL DEFAULT 0")
        await self._add_column("jobs", "hold_sec", "REAL NOT NULL DEFAULT 0")
        await self._add_column("jobs", "paused_at", "TEXT")
        await self._add_column("jobs", "failed_frames_json", "TEXT NOT NULL DEFAULT '[]'")

    async def _add_column(self, table: str, name: str, declaration: str) -> None:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        if any(row["name"] == name for row in rows):
            return
        await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库还没有打开")
        return self._conn

    async def load_settings(self) -> dict:
        async with self._lock:
            cursor = await self.conn.execute("SELECT value FROM settings WHERE key='config'")
            row = await cursor.fetchone()
        merged = dict(DEFAULT_SETTINGS)
        if row is not None:
            try:
                stored = json.loads(row["value"])
            except json.JSONDecodeError:
                stored = {}
            if isinstance(stored, dict):
                merged.update(stored)
        merged["model"] = configured_model(merged.get("model"))
        engine = configured_engine(merged.get("asr_engine"))
        engine_changed = merged.get("asr_engine") != engine
        merged["asr_engine"] = engine
        bounded = clamp_concurrency(merged.get("concurrency"))
        concurrency_changed = merged.get("concurrency") != bounded
        merged["concurrency"] = bounded
        seeded = apply_builtin_rules(merged)
        exaggeration_changed = enable_exaggeration(merged)
        try:
            merged["rules"] = DetectionRules.model_validate(merged.get("rules") or {}).model_dump()
        except Exception:
            merged["rules"] = DetectionRules().model_dump()
            seeded = False
        if seeded or concurrency_changed or exaggeration_changed or engine_changed:
            await self.save_settings(merged)
        return merged

    async def save_settings(self, settings: dict) -> None:
        payload = json.dumps(settings, ensure_ascii=False)
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO settings(key, value) VALUES('config', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (payload,),
            )
            await self.conn.commit()

    async def fail_interrupted(self, updated_at: str) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                UPDATE jobs
                   SET status='failed', stage='failed',
                       message='程序退出时任务未完成', error='interrupted',
                       updated_at=?, finished_at=COALESCE(finished_at, ?)
                 WHERE status IN ('running', 'paused')
                """,
                (updated_at, updated_at),
            )
            await self.conn.commit()

    async def upsert_job(self, row: dict) -> None:
        columns = ", ".join(_JOB_FIELDS)
        placeholders = ", ".join(f":{name}" for name in _JOB_FIELDS)
        updates = ", ".join(
            f"{name}=excluded.{name}" for name in _JOB_FIELDS if name not in {"id", "created_at"}
        )
        async with self._lock:
            await self.conn.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                row,
            )
            await self.conn.commit()

    async def replace_findings(self, job_id: str, findings: list[Finding]) -> None:
        rows = [
            (
                job_id,
                item.time_range,
                item.start_sec,
                item.end_sec,
                item.category,
                item.source,
                item.text,
                item.position,
                item.confidence,
                item.reason,
                item.note,
                item.sentence_start,
                item.sentence_end,
                item.next_start,
            )
            for item in findings
        ]
        async with self._lock:
            await self.conn.execute("DELETE FROM findings WHERE job_id=?", (job_id,))
            if rows:
                await self.conn.executemany(
                    """
                    INSERT INTO findings (
                        job_id, time_range, start_sec, end_sec, category, source, text,
                        position, confidence, reason, note, sentence_start, sentence_end, next_start
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            await self.conn.commit()

    async def has_status(self, status: str) -> bool:
        async with self._lock:
            cursor = await self.conn.execute("SELECT 1 FROM jobs WHERE status=? LIMIT 1", (status,))
            row = await cursor.fetchone()
        return row is not None

    async def list_jobs(self) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            return await cursor.fetchall()

    async def get_job(self, job_id: str) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            return await cursor.fetchone()

    async def get_findings(self, job_id: str) -> list[Finding]:
        async with self._lock:
            cursor = await self.conn.execute(
                "SELECT * FROM findings WHERE job_id=? ORDER BY start_sec, id",
                (job_id,),
            )
            rows = await cursor.fetchall()
        return [
            Finding(
                time_range=row["time_range"],
                category=row["category"],
                source=row["source"] if "source" in row.keys() else "画面",
                text=row["text"],
                position=row["position"],
                confidence=row["confidence"],
                reason=row["reason"],
                note=row["note"] or "",
                start_sec=row["start_sec"],
                end_sec=row["end_sec"],
                sentence_start=_optional_float(row, "sentence_start"),
                sentence_end=_optional_float(row, "sentence_end"),
                next_start=_optional_float(row, "next_start"),
            )
            for row in rows
        ]

    async def delete_job(self, job_id: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            await self.conn.commit()
