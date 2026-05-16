"""SQLite persistence for jobData / pastData (parity with Mongo helpers in dataManager)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.sqlite_job_schema import ensure_sqlite_job_tables


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def sqlite_jobs_db_path() -> Path:
    raw = (os.getenv("SQLITE_JOBS_PATH") or "").strip()
    root = _project_root()
    if raw:
        p = Path(raw).expanduser()
        return p.resolve() if p.is_absolute() else (root / p).resolve()
    return (root / "zata" / "chennu_jobs.sqlite").resolve()


_conn: sqlite3.Connection | None = None


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = sqlite_jobs_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def ensure_store(*, recreate: bool = False) -> None:
    conn = _connection()
    ensure_sqlite_job_tables(conn, recreate=recreate)
    conn.commit()


def reset_connection() -> None:
    """Close pooled connection (e.g. after replacing DB file on disk)."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _parse_stored_timestamp_to_utc(raw: object) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def _apply_status_param(row: dict) -> str | None:
    if "applyStatus" not in row:
        return None
    raw = row.get("applyStatus")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _row_to_job_dict(row: sqlite3.Row) -> dict[str, Any]:
    def _g(key: str) -> str | None:
        v = row[key]
        if v is None:
            return None
        return str(v)

    return {
        "jobId": row["job_id"],
        "title": _g("title") or "",
        "jobUrl": _g("job_url") or "",
        "location": _g("location") or "",
        "employmentType": _g("employment_type") or "",
        "workModel": _g("work_model") or "",
        "seniority": _g("seniority") or "",
        "experience": _g("experience") or "",
        "originalJobPostUrl": _g("original_job_post_url") or "",
        "companyName": _g("company_name") or "",
        "jobDescription": _g("job_description") or "",
        "timestamp": _g("timestamp") or "",
        "applyStatus": _g("apply_status"),
        "platform": _g("platform") or "",
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_jobs(rows: list[dict]) -> int:
    if not rows:
        return 0
    ensure_store(recreate=False)
    conn = _connection()
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        jid = str(row.get("jobId") or "").strip()
        if not jid:
            continue
        apply_val = _apply_status_param(row)

        cur = conn.execute("SELECT apply_status FROM job_data WHERE job_id = ?", (jid,))
        existing = cur.fetchone()
        if apply_val is not None:
            apply_sql = apply_val
        elif existing is not None:
            apply_sql = existing["apply_status"]
        else:
            apply_sql = None

        set_doc = {
            "jobId": jid,
            "title": str(row.get("title") or ""),
            "jobUrl": str(row.get("jobUrl") or ""),
            "location": str(row.get("location") or ""),
            "employmentType": str(row.get("employmentType") or ""),
            "workModel": str(row.get("workModel") or ""),
            "seniority": str(row.get("seniority") or ""),
            "experience": str(row.get("experience") or ""),
            "originalJobPostUrl": str(row.get("originalJobPostUrl") or ""),
            "companyName": str(row.get("companyName") or ""),
            "jobDescription": str(row.get("jobDescription") or ""),
            "timestamp": str(row.get("timestamp") or _utc_now_iso()),
            "platform": str(row.get("platform") or "Unknown"),
        }

        conn.execute(
            """
            INSERT INTO job_data (
                job_id, mongo_oid, title, job_url, location, employment_type,
                work_model, seniority, experience, original_job_post_url,
                company_name, job_description, timestamp, apply_status, platform
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                mongo_oid = excluded.mongo_oid,
                title = excluded.title,
                job_url = excluded.job_url,
                location = excluded.location,
                employment_type = excluded.employment_type,
                work_model = excluded.work_model,
                seniority = excluded.seniority,
                experience = excluded.experience,
                original_job_post_url = excluded.original_job_post_url,
                company_name = excluded.company_name,
                job_description = excluded.job_description,
                timestamp = excluded.timestamp,
                platform = excluded.platform,
                apply_status = COALESCE(excluded.apply_status, job_data.apply_status)
            """,
            (
                set_doc["jobId"],
                None,
                set_doc["title"],
                set_doc["jobUrl"],
                set_doc["location"],
                set_doc["employmentType"],
                set_doc["workModel"],
                set_doc["seniority"],
                set_doc["experience"],
                set_doc["originalJobPostUrl"],
                set_doc["companyName"],
                set_doc["jobDescription"],
                set_doc["timestamp"],
                apply_sql,
                set_doc["platform"],
            ),
        )
        n += 1
    conn.commit()
    return n


def record_past_data(rows: list[dict], *, platform: str) -> int:
    if not rows:
        return 0
    ensure_store(recreate=False)
    conn = _connection()
    now = _utc_now_iso()
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("jobId") or "").strip()
        company_name = str(row.get("companyName") or "").strip() or "Unknown"
        if not job_id:
            continue
        ts = str(row.get("timestamp") or now).strip() or now
        conn.execute(
            """
            INSERT INTO past_data (job_id, mongo_oid, platform, timestamp, company_name)
            VALUES (?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                mongo_oid = excluded.mongo_oid,
                platform = excluded.platform,
                timestamp = excluded.timestamp,
                company_name = excluded.company_name
            """,
            (job_id, None, platform, ts, company_name),
        )
        n += 1
    conn.commit()
    return n


def sort_jobs_fifo_by_timestamp(jobs: list[dict]) -> list[dict]:
    def sort_key(row: dict) -> tuple[int, str, str]:
        ts = str(row.get("timestamp") or "").strip()
        return (1 if not ts else 0, ts, str(row.get("jobId") or ""))

    return sorted(jobs, key=sort_key)


def load_jobs_by_platform(platform: str) -> list[dict]:
    ensure_store(recreate=False)
    conn = _connection()
    cur = conn.execute(
        "SELECT * FROM job_data WHERE platform = ? ORDER BY timestamp, job_id",
        (platform,),
    )
    return sort_jobs_fifo_by_timestamp([_row_to_job_dict(r) for r in cur.fetchall()])


def load_all_jobs() -> list[dict]:
    ensure_store(recreate=False)
    conn = _connection()
    cur = conn.execute("SELECT * FROM job_data")
    jobs = [_row_to_job_dict(r) for r in cur.fetchall()]
    return sort_jobs_fifo_by_timestamp(jobs)


def load_jobs_with_empty_apply_status(platform: str | None = None) -> list[dict]:
    ensure_store(recreate=False)
    conn = _connection()
    if platform:
        cur = conn.execute(
            """
            SELECT * FROM job_data
            WHERE platform = ?
              AND (apply_status IS NULL OR TRIM(apply_status) = '')
            """,
            (platform,),
        )
    else:
        cur = conn.execute(
            """
            SELECT * FROM job_data
            WHERE apply_status IS NULL OR TRIM(apply_status) = ''
            """
        )
    jobs = [_row_to_job_dict(r) for r in cur.fetchall()]
    return sort_jobs_fifo_by_timestamp(jobs)


def update_apply_status_by_job_id(job_id: str, apply_status: str) -> bool:
    jid = str(job_id or "").strip()
    status = str(apply_status or "").strip()
    if not jid:
        return False
    ensure_store(recreate=False)
    conn = _connection()
    cur = conn.execute(
        "UPDATE job_data SET apply_status = ? WHERE job_id = ?",
        (status, jid),
    )
    conn.commit()
    return cur.rowcount > 0


def load_jobs_by_apply_status(apply_status: str) -> list[dict]:
    status = str(apply_status or "").strip()
    if not status:
        return []
    ensure_store(recreate=False)
    conn = _connection()
    cur = conn.execute(
        "SELECT * FROM job_data WHERE apply_status = ?",
        (status,),
    )
    jobs = [_row_to_job_dict(r) for r in cur.fetchall()]
    return sort_jobs_fifo_by_timestamp(jobs)


def job_data_apply_status_summary() -> dict[str, int]:
    ensure_store(recreate=False)
    conn = _connection()
    total = conn.execute("SELECT COUNT(*) FROM job_data").fetchone()[0]
    past_n = conn.execute("SELECT COUNT(*) FROM past_data").fetchone()[0]

    pending = 0
    n_apply = 0
    n_dna = 0
    n_ex = 0
    n_other = 0
    for row in conn.execute("SELECT apply_status FROM job_data"):
        s = str(row["apply_status"] or "").strip()
        if not s:
            pending += 1
        elif s == "APPLY":
            n_apply += 1
        elif s == "DO_NOT_APPLY":
            n_dna += 1
        elif s == "EXISTING":
            n_ex += 1
        else:
            n_other += 1

    return {
        "total": total,
        "nullPending": pending,
        "apply": n_apply,
        "doNotApply": n_dna,
        "existing": n_ex,
        "otherStatus": n_other,
        "pastDataRows": past_n,
    }


def delete_jobs_by_apply_status_not_in(allowed_statuses: list[str] | tuple[str, ...]) -> int:
    normalized = sorted(
        {str(item or "").strip() for item in allowed_statuses if str(item or "").strip()}
    )
    if not normalized:
        raise ValueError("allowedStatuses must include at least one non-empty status")

    ensure_store(recreate=False)
    conn = _connection()
    to_delete: list[str] = []
    for row in conn.execute("SELECT job_id, apply_status FROM job_data"):
        s = str(row["apply_status"] or "").strip()
        if s and s not in normalized:
            jid = str(row["job_id"] or "").strip()
            if jid:
                to_delete.append(jid)
    if not to_delete:
        return 0
    placeholders = ",".join("?" * len(to_delete))
    cur = conn.execute(f"DELETE FROM job_data WHERE job_id IN ({placeholders})", to_delete)
    conn.commit()
    return int(cur.rowcount or 0)


def delete_past_data_older_than_hours(*, hours: float = 48) -> int:
    if hours <= 0:
        raise ValueError("hours must be positive")
    ensure_store(recreate=False)
    conn = _connection()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale_ids: list[str] = []
    for row in conn.execute("SELECT job_id, timestamp FROM past_data"):
        jid = str(row["job_id"] or "").strip()
        parsed = _parse_stored_timestamp_to_utc(row["timestamp"])
        if parsed is not None and parsed < cutoff and jid:
            stale_ids.append(jid)
    if not stale_ids:
        return 0
    placeholders = ",".join("?" * len(stale_ids))
    cur = conn.execute(f"DELETE FROM past_data WHERE job_id IN ({placeholders})", stale_ids)
    conn.commit()
    return int(cur.rowcount or 0)


def load_known_job_ids_by_platform(platform: str) -> set[str]:
    ensure_store(recreate=False)
    conn = _connection()
    job_ids = {
        str(r["job_id"]).strip()
        for r in conn.execute(
            "SELECT job_id FROM job_data WHERE platform = ?", (platform,)
        )
        if r["job_id"]
    }
    past_ids = {
        str(r["job_id"]).strip()
        for r in conn.execute(
            "SELECT job_id FROM past_data WHERE platform = ?", (platform,)
        )
        if r["job_id"]
    }
    return job_ids | past_ids
