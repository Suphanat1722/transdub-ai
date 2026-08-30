from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config

from ..core.config import (
    CACHE_DIR,
    CACHE_FORMAT_REVISION,
    CACHE_MAX_AGE_DAYS,
    CACHE_MAX_BYTES,
    DB_PATH,
    IMPORTS_DIR,
    JOBS_DIR,
    MODEL_NAME,
    PIPELINE_REVISION,
    ROOT,
    data_relative,
    ensure_directories,
    resolve_data_path,
)

_lock = threading.RLock()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_directories()
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _alembic_configuration() -> Config:
    configuration = Config(str(ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{DB_PATH.as_posix()}")
    return configuration


def _has_table(table: str) -> bool:
    if not DB_PATH.is_file():
        return False
    connection = sqlite3.connect(DB_PATH)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _run_alembic() -> None:
    """Stamp databases created before Alembic, then apply all future revisions."""
    from alembic import command

    configuration = _alembic_configuration()
    with connect() as conn:
        tracked = "alembic_version" in {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    if not tracked:
        # Pre-Alembic installations match the initial schema, not the latest one.
        command.stamp(configuration, "20260828_0001")
    command.upgrade(configuration, "head")


def init_db(run_legacy_migration: bool = True) -> None:
    if not _has_table("jobs"):
        from alembic import command

        ensure_directories()
        command.upgrade(_alembic_configuration(), "head")
    _run_alembic()
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DROP TABLE IF EXISTS quota_events")
        conn.execute(
            "UPDATE cues SET status='pending',error='กู้คืน cue ที่หยุดระหว่างสร้างเสียง' WHERE status='processing'"
        )
        conn.execute(
            "UPDATE jobs SET status=CASE WHEN status='pausing' THEN 'paused' "
            "WHEN status='cancelling' THEN 'cancelled' ELSE 'queued' END,"
            "current_cue_id=NULL,control_requested=NULL,"
            "wait_reason='กู้คืนงานหลังเปิดโปรแกรมใหม่',next_attempt_at=NULL "
            "WHERE engine='jaitts' AND status IN "
            "('running','retrying','waiting_model','assembling','pausing','cancelling')"
        )
        conn.execute(
            "UPDATE jobs SET status='queued',control_requested=NULL,current_cue_id=NULL,"
            "wait_reason='กู้คืนงานหลังเปิดโปรแกรมใหม่',updated_at=? "
            "WHERE engine='transdub' AND status IN "
            "('extracting','separating','transcribing','translating','synthesizing','muxing','running')",
            (utc_now(),),
        )
    if run_legacy_migration:
        migrate_gemini_jobs()
        cleanup_legacy_cache()
        reset_reference_leak_outputs()
        reset_legacy_timeline_outputs()
        reset_mismatched_reference_outputs()
        reset_short_articulation_outputs()
    migrate_paths_to_relative()
    cleanup_cache_index()


def migrate_gemini_jobs() -> list[dict]:
    from ..services.srt import parse_srt

    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name='gemini_to_jaitts_v1'").fetchone():
            return []
        legacy = conn.execute("SELECT * FROM jobs WHERE model LIKE 'gemini%' OR engine!='jaitts'").fetchall()
    migrated: list[dict] = []
    for row in legacy:
        old_id = row["id"]
        source = (JOBS_DIR / old_id / "source.srt").resolve()
        if not source.is_file() or not source.is_relative_to(JOBS_DIR.resolve()):
            raise RuntimeError(f"ไม่พบ SRT ต้นฉบับของงาน {old_id}; ยกเลิก migration เพื่อป้องกันข้อมูลสูญหาย")
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in Path(row["filename"]).stem)[:80]
        backup = IMPORTS_DIR / f"{safe_stem}-{digest[:12]}.srt"
        if not backup.exists():
            shutil.copy2(source, backup)
        if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
            raise RuntimeError("ตรวจสอบ hash ของ SRT สำรองไม่ผ่าน; ไม่ลบงานเดิม")
        parsed = parse_srt(raw)
        new_id = str(uuid.uuid4())
        new_dir = JOBS_DIR / new_id
        new_dir.mkdir(parents=True, exist_ok=False)
        (new_dir / "source.srt").write_bytes(raw)
        create_job(new_id, row["filename"], parsed.encoding, MODEL_NAME, parsed.warnings, parsed.cues)
        with connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (old_id,))
        old_dir = (JOBS_DIR / old_id).resolve()
        if old_dir.is_dir() and old_dir.is_relative_to(JOBS_DIR.resolve()):
            shutil.rmtree(old_dir)
        migrated.append(
            {"old_job_id": old_id, "new_job_id": new_id, "source_backup": str(backup), "sha256": digest}
        )
    with connect() as conn:
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            ("gemini_to_jaitts_v1", utc_now(), json.dumps(migrated, ensure_ascii=False)),
        )
    return migrated


def cleanup_legacy_cache() -> None:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name='gemini_cache_cleanup_v1'").fetchone():
            return
        migrated = conn.execute(
            "SELECT details_json FROM migrations WHERE name='gemini_to_jaitts_v1'"
        ).fetchone()
        paths = (
            [row["path"] for row in conn.execute("SELECT path FROM audio_cache").fetchall()]
            if migrated
            else []
        )
        conn.execute("DELETE FROM audio_cache")
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            ("gemini_cache_cleanup_v1", utc_now(), json.dumps({"removed_entries": len(paths)})),
        )
    cache_root = CACHE_DIR.resolve()
    for value in paths:
        path = Path(value).resolve()
        if path.is_file() and path.is_relative_to(cache_root):
            path.unlink()


def reset_reference_leak_outputs() -> list[dict]:
    """Reset outputs generated before reference preprocessing and duration used the same audio."""
    migration_name = "jaitts_reference_leak_v3"
    generated_warnings = {
        "เสียงยาวเกินช่วงหลังเร่งถึง 1.35 เท่า",
        "ปลายเสียงยังมีพลังงานสูงหลังลองเพิ่มเวลา 3 ครั้ง กรุณาตรวจฟังและสร้างใหม่",
    }
    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name=?", (migration_name,)).fetchone():
            return []
        rows = conn.execute(
            "SELECT c.id,c.job_id,c.warnings_json FROM cues c JOIN jobs j ON j.id=c.job_id "
            "WHERE j.engine='jaitts' AND j.status!='cancelled' AND c.status='completed'"
        ).fetchall()
        affected: dict[str, int] = {}
        for row in rows:
            warnings = [
                warning for warning in json.loads(row["warnings_json"]) if warning not in generated_warnings
            ]
            conn.execute(
                "UPDATE cues SET status='pending',warnings_json=?,audio_path=NULL,"
                "original_duration_ms=NULL,final_duration_ms=NULL,speed_factor=1.0,attempts=0,error=NULL "
                "WHERE id=?",
                (json.dumps(warnings, ensure_ascii=False), row["id"]),
            )
            affected[row["job_id"]] = affected.get(row["job_id"], 0) + 1
        now = utc_now()
        for job_id in affected:
            conn.execute(
                "UPDATE jobs SET status='paused',error=NULL,"
                "wait_reason='รีเซ็ตเสียงเดิมที่มี reference นำหน้า กดทำต่อเพื่อสร้างใหม่',"
                "next_attempt_at=NULL,completed_at=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
        details = [{"job_id": job_id, "reset_cues": count} for job_id, count in affected.items()]
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            (migration_name, now, json.dumps(details, ensure_ascii=False)),
        )
    return details


def reset_legacy_timeline_outputs() -> list[dict]:
    """Reset fitted cue files created before natural-duration timeline scheduling."""
    migration_name = "jaitts_natural_timeline_v4"
    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name=?", (migration_name,)).fetchone():
            return []
        rows = conn.execute(
            "SELECT c.id,c.job_id,c.warnings_json FROM cues c JOIN jobs j ON j.id=c.job_id "
            "WHERE j.engine='jaitts' AND j.status!='cancelled' AND c.status='completed'"
        ).fetchall()
        affected: dict[str, int] = {}
        for row in rows:
            warnings = [
                warning
                for warning in json.loads(row["warnings_json"])
                if "เร่งถึง 1.35 เท่า" not in warning
                and not warning.startswith("เสียงยังทับ cue ถัดไป")
                and not warning.startswith("เสียงยาวถึง cue ถัดไป")
            ]
            conn.execute(
                "UPDATE cues SET status='pending',warnings_json=?,audio_path=NULL,"
                "original_duration_ms=NULL,final_duration_ms=NULL,speed_factor=1.0,attempts=0,error=NULL "
                "WHERE id=?",
                (json.dumps(warnings, ensure_ascii=False), row["id"]),
            )
            affected[row["job_id"]] = affected.get(row["job_id"], 0) + 1
        now = utc_now()
        for job_id in affected:
            conn.execute(
                "UPDATE jobs SET status='paused',error=NULL,"
                "wait_reason='รีเซ็ตการจัดเวลาเดิม กดทำต่อเพื่อใช้ timeline แบบธรรมชาติ',"
                "next_attempt_at=NULL,completed_at=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
        details = [{"job_id": job_id, "reset_cues": count} for job_id, count in affected.items()]
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            (migration_name, now, json.dumps(details, ensure_ascii=False)),
        )
    return details


def reset_mismatched_reference_outputs() -> list[dict]:
    """Reset speech generated from clipped reference audio paired with the full transcript."""
    migration_name = "jaitts_matched_reference_v5"
    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name=?", (migration_name,)).fetchone():
            return []
        rows = conn.execute(
            "SELECT c.id,c.job_id,c.warnings_json FROM cues c JOIN jobs j ON j.id=c.job_id "
            "WHERE j.engine='jaitts' AND j.status!='cancelled' AND c.status='completed'"
        ).fetchall()
        affected: dict[str, int] = {}
        for row in rows:
            warnings = [
                warning
                for warning in json.loads(row["warnings_json"])
                if not warning.startswith("ปลายเสียงยังมีพลังงานสูง")
                and not warning.startswith("เสียงยังทับ cue ถัดไป")
                and not warning.startswith("เสียงยาวถึง cue ถัดไป")
            ]
            conn.execute(
                "UPDATE cues SET status='pending',warnings_json=?,audio_path=NULL,"
                "original_duration_ms=NULL,final_duration_ms=NULL,speed_factor=1.0,attempts=0,error=NULL "
                "WHERE id=?",
                (json.dumps(warnings, ensure_ascii=False), row["id"]),
            )
            affected[row["job_id"]] = affected.get(row["job_id"], 0) + 1
        now = utc_now()
        for job_id in affected:
            conn.execute(
                "UPDATE jobs SET status='paused',error=NULL,"
                "wait_reason='รีเซ็ตเสียงที่ reference กับ transcript ไม่ตรงกัน กดทำต่อเพื่อสร้างใหม่',"
                "next_attempt_at=NULL,completed_at=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
        details = [{"job_id": job_id, "reset_cues": count} for job_id, count in affected.items()]
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            (migration_name, now, json.dumps(details, ensure_ascii=False)),
        )
    return details


def reset_short_articulation_outputs() -> list[dict]:
    """Reset speech generated without a small end margin for complete articulation."""
    migration_name = "jaitts_articulation_margin_v6"
    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name=?", (migration_name,)).fetchone():
            return []
        rows = conn.execute(
            "SELECT c.id,c.job_id,c.warnings_json FROM cues c JOIN jobs j ON j.id=c.job_id "
            "WHERE j.engine='jaitts' AND j.status!='cancelled' AND c.status='completed'"
        ).fetchall()
        affected: dict[str, int] = {}
        for row in rows:
            warnings = [
                warning
                for warning in json.loads(row["warnings_json"])
                if not warning.startswith("ปลายเสียงยังมีพลังงานสูง")
                and not warning.startswith("เสียงยังทับ cue ถัดไป")
                and not warning.startswith("เสียงยาวถึง cue ถัดไป")
            ]
            conn.execute(
                "UPDATE cues SET status='pending',warnings_json=?,audio_path=NULL,"
                "original_duration_ms=NULL,final_duration_ms=NULL,speed_factor=1.0,attempts=0,error=NULL "
                "WHERE id=?",
                (json.dumps(warnings, ensure_ascii=False), row["id"]),
            )
            affected[row["job_id"]] = affected.get(row["job_id"], 0) + 1
        now = utc_now()
        for job_id in affected:
            conn.execute(
                "UPDATE jobs SET status='paused',error=NULL,"
                "wait_reason='รีเซ็ตเสียงสั้นที่มีเวลาออกเสียงไม่พอ กดทำต่อเพื่อสร้างใหม่',"
                "next_attempt_at=NULL,completed_at=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
        details = [{"job_id": job_id, "reset_cues": count} for job_id, count in affected.items()]
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            (migration_name, now, json.dumps(details, ensure_ascii=False)),
        )
    return details


def migrate_paths_to_relative() -> None:
    """Make existing database paths portable without changing any file content."""
    migration_name = "portable_data_paths_v7"
    with connect() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name=?", (migration_name,)).fetchone():
            return
        changed = 0
        for table, key in (("cues", "id"), ("voice_profiles", "id"), ("audio_cache", "cache_key")):
            for row in conn.execute(
                f"SELECT {key},audio_path FROM {table}"
                if table != "audio_cache"
                else f"SELECT {key},path AS audio_path FROM {table}"
            ).fetchall():
                if not row["audio_path"]:
                    continue
                try:
                    relative = data_relative(row["audio_path"])
                except ValueError:
                    continue
                column = "path" if table == "audio_cache" else "audio_path"
                conn.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (relative, row[key]))
                changed += 1
        conn.execute(
            "INSERT INTO migrations(name,applied_at,details_json) VALUES(?,?,?)",
            (migration_name, utc_now(), json.dumps({"converted_paths": changed})),
        )


def cleanup_cache_index(*, max_age_days: int = CACHE_MAX_AGE_DAYS, max_bytes: int = CACHE_MAX_BYTES) -> dict:
    """Evict invalid, obsolete, and oversized cache entries without touching job audio."""
    now = datetime.now(UTC)
    with connect() as conn:
        rows = conn.execute(
            "SELECT cache_key,path,created_at,last_accessed_at,format_revision FROM audio_cache"
        ).fetchall()
        removed_rows = 0
        indexed: set[Path] = set()
        valid: list[tuple[datetime, sqlite3.Row, Path, int]] = []
        for row in rows:
            try:
                path = resolve_data_path(row["path"])
            except ValueError:
                path = Path("__invalid__")
            timestamp_text = row["last_accessed_at"] or row["created_at"]
            try:
                timestamp = datetime.fromisoformat(timestamp_text)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                timestamp = datetime.min.replace(tzinfo=UTC)
            obsolete = row["format_revision"] != CACHE_FORMAT_REVISION
            expired = max_age_days >= 0 and (now - timestamp).days > max_age_days
            if not path.is_file() or obsolete or expired:
                conn.execute("DELETE FROM audio_cache WHERE cache_key=?", (row["cache_key"],))
                if path.is_file() and path.resolve().is_relative_to(CACHE_DIR.resolve()):
                    path.unlink(missing_ok=True)
                removed_rows += 1
            else:
                indexed.add(path.resolve())
                valid.append((timestamp, row, path, path.stat().st_size))

        total_bytes = sum(entry[3] for entry in valid)
        if max_bytes >= 0 and total_bytes > max_bytes:
            for _timestamp, row, path, size in sorted(valid, key=lambda entry: entry[0]):
                if total_bytes <= max_bytes:
                    break
                conn.execute("DELETE FROM audio_cache WHERE cache_key=?", (row["cache_key"],))
                indexed.discard(path.resolve())
                if path.resolve().is_relative_to(CACHE_DIR.resolve()):
                    path.unlink(missing_ok=True)
                total_bytes -= size
                removed_rows += 1
    removed_files = 0
    for path in CACHE_DIR.rglob("*.wav"):
        if path.resolve() not in indexed:
            path.unlink(missing_ok=True)
            removed_files += 1
    return {"removed_rows": removed_rows, "removed_files": removed_files}


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def create_job(
    job_id: str, filename: str, encoding: str, model: str, warnings: list[str], cues: list
) -> None:
    now = utc_now()
    seed = int.from_bytes(uuid.uuid4().bytes[:4], "big") & 0x7FFFFFFF
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id,filename,encoding,model,status,warnings_json,created_at,updated_at,seed,engine,pipeline_revision) "
            "VALUES(?,?,?,?,?,?,?,?,?,'jaitts',?)",
            (
                job_id,
                filename,
                encoding,
                model,
                "draft",
                json.dumps(warnings, ensure_ascii=False),
                now,
                now,
                seed,
                PIPELINE_REVISION,
            ),
        )
        conn.executemany(
            "INSERT INTO cues(job_id,position,source_index,start_ms,end_ms,text,warnings_json,seed) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    job_id,
                    c.position,
                    c.source_index,
                    c.start_ms,
                    c.end_ms,
                    c.text,
                    json.dumps(c.warnings, ensure_ascii=False),
                    (seed + c.position) & 0x7FFFFFFF,
                )
                for c in cues
            ],
        )


def _translation_progress(conn, job_id: str) -> dict | None:
    rows = conn.execute(
        "SELECT chunk_index,status FROM translation_chunks WHERE job_id=? ORDER BY chunk_index",
        (job_id,),
    ).fetchall()
    if not rows:
        return None
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    total = len(rows)
    completed = by_status.get("completed", 0)
    failed = by_status.get("failed", 0)
    # Next chunk to work on: the first non-completed chunk, 1-based.
    current_index = next(
        (row["chunk_index"] + 1 for row in rows if row["status"] != "completed"),
        total,
    )
    progress = round(completed / total * 100, 1) if total else 0
    return {
        "chunks_total": total,
        "chunks_completed": completed,
        "chunks_failed": failed,
        "current_chunk": current_index,
        "progress": progress,
        "in_progress": any(row["status"] == "pending" for row in rows),
    }


def get_job(job_id: str, include_cues: bool = True) -> dict | None:
    with connect() as conn:
        job = row_dict(
            conn.execute(
                "SELECT j.*,v.name voice_profile_name FROM jobs j LEFT JOIN voice_profiles v ON v.id=j.voice_profile_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        )
        if not job:
            return None
        job["warnings"] = json.loads(job.pop("warnings_json"))
        job["glossary"] = json.loads(job.pop("glossary_json", "[]"))
        for key in (
            "pause_after_transcription",
            "pause_after_translation",
            "transcript_approved",
            "translation_approved",
            "video_stream_copied",
        ):
            if key in job and job[key] is not None:
                job[key] = bool(job[key])
        if include_cues:
            rows = conn.execute("SELECT * FROM cues WHERE job_id=? ORDER BY position", (job_id,)).fetchall()
            job["cues"] = []
            for row in rows:
                cue = dict(row)
                cue["warnings"] = json.loads(cue.pop("warnings_json"))
                cue["tail_metrics"] = json.loads(cue.pop("tail_metrics_json", "{}"))
                cue["source_cue_indexes"] = json.loads(
                    cue.pop("source_cue_indexes_json", "[]") or "[]"
                )
                job["cues"].append(cue)
            source_rows = conn.execute(
                "SELECT * FROM source_cues WHERE job_id=? ORDER BY position", (job_id,)
            ).fetchall()
            job["source_cues"] = []
            for row in source_rows:
                cue = dict(row)
                cue["warnings"] = json.loads(cue.pop("warnings_json"))
                job["source_cues"].append(cue)
        counts = conn.execute(
            "SELECT status,COUNT(*) count FROM cues WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()
        job["counts"] = {r["status"]: r["count"] for r in counts}
        job["total_cues"] = sum(job["counts"].values())
        job["completed_cues"] = job["counts"].get("completed", 0)
        timing = conn.execute(
            "SELECT AVG(generation_duration_ms) average_ms FROM "
            "(SELECT generation_duration_ms FROM cues WHERE job_id=? AND generation_duration_ms IS NOT NULL "
            "ORDER BY id DESC LIMIT 50)",
            (job_id,),
        ).fetchone()
        job["average_cue_ms"] = round(timing["average_ms"] or 0)
        remaining = job["total_cues"] - job["completed_cues"]
        job["eta_seconds"] = (
            round(remaining * job["average_cue_ms"] / 1000) if job["average_cue_ms"] else None
        )
        job["translation_progress"] = _translation_progress(conn, job_id)
        return job


def list_jobs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT j.*,v.name voice_profile_name,COUNT(c.id) total_cues,"
            "SUM(CASE WHEN c.status='completed' THEN 1 ELSE 0 END) completed_cues "
            "FROM jobs j LEFT JOIN voice_profiles v ON v.id=j.voice_profile_id "
            "LEFT JOIN cues c ON c.job_id=j.id GROUP BY j.id ORDER BY j.created_at DESC"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["warnings"] = json.loads(item.pop("warnings_json"))
            item["glossary"] = json.loads(item.pop("glossary_json", "[]"))
            for key in (
                "pause_after_transcription",
                "pause_after_translation",
                "transcript_approved",
                "translation_approved",
                "video_stream_copied",
            ):
                if key in item and item[key] is not None:
                    item[key] = bool(item[key])
            result.append(item)
        return result


def delete_job(job_id: str) -> bool:
    deleted = False
    with connect() as conn:
        deleted = bool(conn.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount)
    if deleted:
        # The shared audio cache may hold WAVs generated only for this job.
        # Drop any entry no longer referenced by a remaining cue, and delete
        # the file on disk, so deleting a project does not leave orphan files.
        _purge_unreferenced_cache()
    return deleted


def _purge_unreferenced_cache() -> None:
    with connect() as conn:
        rows = conn.execute("SELECT cache_key,path FROM audio_cache").fetchall()
        referenced = {
            row["cache_key"]
            for row in conn.execute(
                "SELECT DISTINCT cache_key FROM cues WHERE cache_key IS NOT NULL"
            ).fetchall()
        }
        ids_to_delete = [row["cache_key"] for row in rows if row["cache_key"] not in referenced]
        if ids_to_delete:
            placeholders = ",".join("?" for _ in ids_to_delete)
            conn.execute(
                f"DELETE FROM audio_cache WHERE cache_key IN ({placeholders})", ids_to_delete
            )
        files_to_delete = [
            row["path"] for row in rows if row["cache_key"] not in referenced
        ]
    cache_root = CACHE_DIR.resolve()
    for value in files_to_delete:
        try:
            path = resolve_data_path(value)
        except ValueError:
            continue
        if path.is_file() and path.resolve().is_relative_to(cache_root):
            path.unlink(missing_ok=True)


def update_job(job_id: str, **fields) -> None:
    allowed = {
        "voice_profile_id",
        "nfe_step",
        "inference_speed",
        "max_start_delay_ms",
        "status",
        "error",
        "wait_reason",
        "next_attempt_at",
        "started_at",
        "completed_at",
        "control_requested",
        "current_cue_id",
        "active_output_revision",
        "pipeline_revision",
        "glossary_json",
        "glossary_revision",
        "source_path",
        "original_audio_path",
        "background_path",
        "source_srt_path",
        "translated_srt_path",
        "dub_audio_path",
        "output_video_path",
        "video_duration_ms",
        "video_codec",
        "source_language",
        "target_language",
        "pause_after_transcription",
        "pause_after_translation",
        "transcript_approved",
        "translation_approved",
        "background_volume",
        "voice_volume",
        "stage",
        "progress",
        "quota_retries",
        "translation_model",
        "video_stream_copied",
        "warnings_json",
    }
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    values["updated_at"] = utc_now()
    with connect() as conn:
        conn.execute(
            f"UPDATE jobs SET {','.join(f'{k}=?' for k in values)} WHERE id=?", (*values.values(), job_id)
        )


def next_cue(job_id: str) -> dict | None:
    with connect() as conn:
        return row_dict(
            conn.execute(
                "SELECT * FROM cues WHERE job_id=? AND status IN ('pending','failed') ORDER BY position LIMIT 1",
                (job_id,),
            ).fetchone()
        )


def update_cue(cue_id: int, **fields) -> None:
    allowed = {
        "status",
        "warnings_json",
        "audio_path",
        "original_duration_ms",
        "final_duration_ms",
        "speed_factor",
        "attempts",
        "error",
        "effective_seed",
        "generation_revision",
        "inference_text",
        "duration_multiplier",
        "generation_passes",
        "tail_metrics_json",
        "generation_duration_ms",
        "cache_key",
        "requested_duration_multiplier",
        "pipeline_revision",
        "text",
        "start_ms",
        "end_ms",
        "source_cue_indexes_json",
        "translation_chunk_id",
    }
    values = {k: v for k, v in fields.items() if k in allowed}
    if values:
        with connect() as conn:
            conn.execute(
                f"UPDATE cues SET {','.join(f'{k}=?' for k in values)} WHERE id=?", (*values.values(), cue_id)
            )


def reset_failed_cues(job_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE cues SET status='pending',error=NULL WHERE job_id=? AND status='failed'", (job_id,)
        )


def recover_processing_cues(job_id: str) -> int:
    """Return interrupted cues to the queue without touching completed audio."""
    with connect() as conn:
        return conn.execute(
            "UPDATE cues SET status='pending',error='กู้คืน cue ที่หยุดระหว่างสร้างเสียง' "
            "WHERE job_id=? AND status='processing'",
            (job_id,),
        ).rowcount


def create_voice_profile(
    profile_id: str, name: str, transcript: str, path: str, digest: str, duration_ms: int, warnings: list[str]
) -> dict:
    with connect() as conn:
        conn.execute(
            "INSERT INTO voice_profiles(id,name,transcript,audio_path,audio_hash,duration_ms,warnings_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                profile_id,
                name,
                transcript,
                path,
                digest,
                duration_ms,
                json.dumps(warnings, ensure_ascii=False),
                utc_now(),
            ),
        )
    profile = get_voice_profile(profile_id)
    if profile is None:
        raise RuntimeError("สร้างโปรไฟล์เสียงแล้วแต่ไม่สามารถอ่านข้อมูลกลับได้")
    return profile


def get_voice_profile(profile_id: str) -> dict | None:
    with connect() as conn:
        item = row_dict(conn.execute("SELECT * FROM voice_profiles WHERE id=?", (profile_id,)).fetchone())
    if item:
        item["warnings"] = json.loads(item.pop("warnings_json"))
    return item


def list_voice_profiles() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM voice_profiles ORDER BY name COLLATE NOCASE").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json"))
        result.append(item)
    return result


def delete_voice_profile(profile_id: str) -> str | None:
    with connect() as conn:
        used = conn.execute(
            "SELECT COUNT(*) count FROM jobs WHERE voice_profile_id=?", (profile_id,)
        ).fetchone()["count"]
        if used:
            raise ValueError("โปรไฟล์นี้ถูกใช้อยู่ในงาน จึงยังลบไม่ได้")
        row = conn.execute("SELECT audio_path FROM voice_profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM voice_profiles WHERE id=?", (profile_id,))
        return row["audio_path"]


def get_settings() -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT nfe_step,inference_speed,max_start_delay_ms,allow_cpu FROM settings WHERE id=1"
        ).fetchone()
    item = dict(row)
    item["allow_cpu"] = bool(item["allow_cpu"])
    return item


def save_settings(nfe_step: int, inference_speed: float, max_start_delay_ms: int, allow_cpu: bool) -> dict:
    with connect() as conn:
        conn.execute(
            "UPDATE settings SET nfe_step=?,inference_speed=?,max_start_delay_ms=?,allow_cpu=? WHERE id=1",
            (nfe_step, inference_speed, max_start_delay_ms, int(allow_cpu)),
        )
    return get_settings()


def cache_get(cache_key: str) -> dict | None:
    with connect() as conn:
        item = row_dict(conn.execute("SELECT * FROM audio_cache WHERE cache_key=?", (cache_key,)).fetchone())
        if item:
            conn.execute(
                "UPDATE audio_cache SET last_accessed_at=? WHERE cache_key=?", (utc_now(), cache_key)
            )
    if not item:
        return None
    try:
        item["path"] = str(resolve_data_path(item["path"]))
    except ValueError:
        return None
    item["quality"] = json.loads(item.get("quality_json") or "{}")
    return item if Path(item["path"]).is_file() else None


def cache_put(cache_key: str, path: str, duration_ms: int, quality: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO audio_cache(cache_key,path,duration_ms,created_at,quality_json,format_revision,last_accessed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                cache_key,
                data_relative(path),
                duration_ms,
                utc_now(),
                json.dumps(quality or {}),
                CACHE_FORMAT_REVISION,
                utc_now(),
            ),
        )


def cue_counts(job_id: str) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status,COUNT(*) count FROM cues WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def list_cues(
    job_id: str,
    *,
    offset: int = 0,
    limit: int = 100,
    status: str | None = None,
    query: str = "",
    warning: bool = False,
) -> tuple[list[dict], int]:
    clauses = ["job_id=?"]
    params: list[object] = [job_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if warning:
        clauses.append("warnings_json <> '[]'")
    if query:
        clauses.append("(text LIKE ? OR source_index LIKE ?)")
        term = f"%{query}%"
        params.extend((term, term))
    where = " AND ".join(clauses)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM cues WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM cues WHERE {where} ORDER BY position LIMIT ? OFFSET ?", (*params, limit, offset)
        ).fetchall()
    result = []
    for row in rows:
        cue = dict(row)
        cue["warnings"] = json.loads(cue.pop("warnings_json"))
        cue["tail_metrics"] = json.loads(cue.pop("tail_metrics_json", "{}"))
        cue["source_cue_indexes"] = json.loads(
            cue.pop("source_cue_indexes_json", "[]") or "[]"
        )
        result.append(cue)
    return result, total


def create_video_job(
    *,
    job_id: str,
    filename: str,
    source_path: Path,
    voice_profile_id: str,
    source_language: str,
    pause_after_transcription: bool,
    pause_after_translation: bool,
    background_volume: float,
    voice_volume: float,
) -> dict:
    now = utc_now()
    seed = int.from_bytes(uuid.uuid4().bytes[:4], "big") & 0x7FFFFFFF
    settings = get_settings()
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs(
                id,filename,encoding,model,status,warnings_json,created_at,updated_at,
                voice_profile_id,nfe_step,inference_speed,max_start_delay_ms,seed,engine,
                pipeline_revision,source_path,source_language,target_language,
                pause_after_transcription,pause_after_translation,background_volume,voice_volume,
                stage,progress
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'transdub',?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                filename,
                "utf-8",
                MODEL_NAME,
                "queued",
                "[]",
                now,
                now,
                voice_profile_id,
                settings["nfe_step"],
                settings["inference_speed"],
                settings["max_start_delay_ms"],
                seed,
                PIPELINE_REVISION,
                data_relative(source_path),
                source_language or "auto",
                "th",
                int(pause_after_transcription),
                int(pause_after_translation),
                background_volume,
                voice_volume,
                "uploaded",
                0,
            ),
        )
    created = get_job(job_id, include_cues=False)
    if created is None:
        raise RuntimeError("สร้างงานแล้วแต่ไม่สามารถอ่านข้อมูลกลับได้")
    return created


def replace_source_cues(job_id: str, cues: list[dict]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM source_cues WHERE job_id=?", (job_id,))
        conn.executemany(
            """INSERT INTO source_cues(
                job_id,position,source_index,start_ms,end_ms,text,speaker,warnings_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                (
                    job_id,
                    index + 1,
                    str(cue.get("source_index") or index + 1),
                    int(cue["start_ms"]),
                    int(cue["end_ms"]),
                    str(cue["text"]).strip(),
                    cue.get("speaker"),
                    json.dumps(cue.get("warnings", []), ensure_ascii=False),
                )
                for index, cue in enumerate(cues)
            ],
        )


def source_cues(job_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM source_cues WHERE job_id=? ORDER BY position", (job_id,)
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json", "[]"))
        result.append(item)
    return result


def update_source_cue(cue_id: int, *, text: str, start_ms: int, end_ms: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT job_id FROM source_cues WHERE id=?", (cue_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE source_cues SET text=?,start_ms=?,end_ms=? WHERE id=?",
            (text, start_ms, end_ms, cue_id),
        )
        return row_dict(conn.execute("SELECT * FROM source_cues WHERE id=?", (cue_id,)).fetchone())


def replace_translation_cues(job_id: str, cues: list[dict]) -> None:
    job = get_job(job_id, include_cues=False)
    if not job:
        raise ValueError("ไม่พบงาน")
    seed = int(job["seed"])
    with connect() as conn:
        conn.execute("DELETE FROM cues WHERE job_id=?", (job_id,))
        conn.executemany(
            """INSERT INTO cues(
                job_id,position,source_index,start_ms,end_ms,text,status,warnings_json,seed,
                source_cue_indexes_json,translation_chunk_id
            ) VALUES(?,?,?,?,?,?,'pending',?,?,?,?)""",
            [
                (
                    job_id,
                    index + 1,
                    str(cue.get("source_index") or index + 1),
                    int(cue["start_ms"]),
                    int(cue["end_ms"]),
                    str(cue["text"]).strip(),
                    json.dumps(cue.get("warnings", []), ensure_ascii=False),
                    (seed + index + 1) & 0x7FFFFFFF,
                    json.dumps(cue.get("source_cue_indexes", [])),
                    cue.get("translation_chunk_id"),
                )
                for index, cue in enumerate(cues)
            ],
        )


def replace_translation_chunks(job_id: str, chunks: list[dict]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM translation_chunks WHERE job_id=?", (job_id,))
        conn.executemany(
            """INSERT INTO translation_chunks(
                id,job_id,chunk_index,target_start,target_end,context_start,context_end,status
            ) VALUES(?,?,?,?,?,?,?,'pending')""",
            [
                (
                    chunk["id"],
                    job_id,
                    chunk["index"],
                    chunk["target_start"],
                    chunk["target_end"],
                    chunk["context_start"],
                    chunk["context_end"],
                )
                for chunk in chunks
            ],
        )


def sync_translation_chunks(job_id: str, chunks: list[dict]) -> None:
    """Synchronise a translation plan while retaining completed chunk metadata.

    Translation can split a failed request into smaller requests.  Keeping this
    operation incremental means a restart can still reuse checkpoints that were
    already generated, while replacing the failed parent chunk with its children
    does not leave stale ranges in the UI or database.
    """
    desired = {str(chunk["id"]): chunk for chunk in chunks}
    with connect() as conn:
        existing_rows = conn.execute(
            "SELECT id FROM translation_chunks WHERE job_id=? ORDER BY chunk_index", (job_id,)
        ).fetchall()
        existing_ids = [str(row["id"]) for row in existing_rows]

        # The unique(job_id, chunk_index) constraint makes a direct reorder
        # unsafe (e.g. inserting a split child at index 1 while the old second
        # chunk still occupies index 1).  Move all rows to a temporary range
        # before applying the desired indexes.
        for offset, chunk_id in enumerate(existing_ids):
            conn.execute(
                "UPDATE translation_chunks SET chunk_index=? WHERE id=? AND job_id=?",
                (-1_000_000 - offset, chunk_id, job_id),
            )

        stale_ids = [chunk_id for chunk_id in existing_ids if chunk_id not in desired]
        if stale_ids:
            conn.executemany(
                "DELETE FROM translation_chunks WHERE id=? AND job_id=?",
                [(chunk_id, job_id) for chunk_id in stale_ids],
            )

        for index, chunk in enumerate(chunks):
            chunk_id = str(chunk["id"])
            values = (
                index,
                int(chunk["target_start"]),
                int(chunk["target_end"]),
                int(chunk["context_start"]),
                int(chunk["context_end"]),
                chunk_id,
                job_id,
            )
            if chunk_id in existing_ids:
                conn.execute(
                    """UPDATE translation_chunks SET
                       chunk_index=?,target_start=?,target_end=?,context_start=?,context_end=?
                       WHERE id=? AND job_id=?""",
                    values,
                )
            else:
                conn.execute(
                    """INSERT INTO translation_chunks(
                       id,job_id,chunk_index,target_start,target_end,context_start,context_end,status
                    ) VALUES(?,?,?,?,?,?,?,'pending')""",
                    (
                        chunk_id,
                        job_id,
                        index,
                        int(chunk["target_start"]),
                        int(chunk["target_end"]),
                        int(chunk["context_start"]),
                        int(chunk["context_end"]),
                    ),
                )


def translation_chunks(job_id: str) -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM translation_chunks WHERE job_id=? ORDER BY chunk_index", (job_id,)
            ).fetchall()
        ]


def update_translation_chunk(chunk_id: str, **fields) -> None:
    allowed = {"status", "model", "output_cue_count", "error"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if values:
        with connect() as conn:
            conn.execute(
                f"UPDATE translation_chunks SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), chunk_id),
            )


def record_attempt(
    job_id: str,
    stage: str,
    outcome: str,
    *,
    unit_id: str | None = None,
    model: str | None = None,
    message: str | None = None,
    usage: dict | None = None,
) -> None:
    usage = usage or {}
    with connect() as conn:
        conn.execute(
            """INSERT INTO stage_attempts(
                job_id,stage,unit_id,model,outcome,message,input_tokens,output_tokens,
                thought_tokens,total_tokens,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                stage,
                unit_id,
                model,
                outcome,
                message,
                int(usage.get("input", 0)),
                int(usage.get("output", 0)),
                int(usage.get("thoughts", 0)),
                int(usage.get("total", 0)),
                utc_now(),
            ),
        )


def record_api_usage(
    job_id: str, stage: str, model: str, *, audio_seconds: float = 0, total_tokens: int = 0
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_usage(job_id,stage,model,requested_at,audio_seconds,total_tokens) VALUES(?,?,?,?,?,?)",
            (job_id, stage, model, utc_now(), audio_seconds, total_tokens),
        )


def put_artifact(job_id: str, kind: str, path: Path, media_type: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO artifacts(job_id,kind,path,media_type,created_at) VALUES(?,?,?,?,?)
               ON CONFLICT(job_id,kind) DO UPDATE SET path=excluded.path,
               media_type=excluded.media_type,created_at=excluded.created_at""",
            (job_id, kind, data_relative(path), media_type, utc_now()),
        )


def get_artifact(job_id: str, kind: str) -> dict | None:
    with connect() as conn:
        item = row_dict(
            conn.execute(
                "SELECT * FROM artifacts WHERE job_id=? AND kind=?", (job_id, kind)
            ).fetchone()
        )
    if item:
        item["resolved_path"] = str(resolve_data_path(item["path"]))
    return item


def list_artifacts(job_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind,media_type,created_at FROM artifacts WHERE job_id=? ORDER BY kind", (job_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def delete_artifacts(job_id: str, kinds: set[str]) -> None:
    if not kinds:
        return
    placeholders = ",".join("?" for _ in kinds)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT path FROM artifacts WHERE job_id=? AND kind IN ({placeholders})",
            (job_id, *sorted(kinds)),
        ).fetchall()
        conn.execute(
            f"DELETE FROM artifacts WHERE job_id=? AND kind IN ({placeholders})",
            (job_id, *sorted(kinds)),
        )
    for row in rows:
        try:
            path = resolve_data_path(row["path"])
        except ValueError:
            continue
        if path.is_file():
            path.unlink(missing_ok=True)


def get_cue(cue_id: int) -> dict | None:
    with connect() as conn:
        item = row_dict(conn.execute("SELECT * FROM cues WHERE id=?", (cue_id,)).fetchone())
    if item:
        item["warnings"] = json.loads(item.pop("warnings_json", "[]"))
        item["source_cue_indexes"] = json.loads(item.pop("source_cue_indexes_json", "[]"))
    return item
