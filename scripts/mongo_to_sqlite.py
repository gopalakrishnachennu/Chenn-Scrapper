#!/usr/bin/env python3
"""
One-way export: MongoDB (chennu job collections) -> SQLite file.

Uses JOB_STORAGE_BACKEND only indirectly via imports; export always reads MongoDB through
getMongoDb() while MONGODB_URI points at your cluster/local mongod.

To seed the live SQLite store before JOB_STORAGE_BACKEND=sqlite:

  python scripts/mongo_to_sqlite.py -o zata/chennu_jobs.sqlite
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.sqlite_job_schema import ensure_sqlite_job_tables  # noqa: E402
from utils.dataManager import (  # noqa: E402
    JOB_DATA_COLLECTION,
    PAST_DATA_COLLECTION,
    _mongoDocToJobRow,
    getMongoDb,
)


def _ensure_tables(conn: sqlite3.Connection) -> None:
    ensure_sqlite_job_tables(conn, recreate=False)


def _row_job(doc: dict) -> tuple:
    normalized = _mongoDocToJobRow(doc)
    jid = str(normalized.get("jobId") or "").strip()
    oid = doc.get("_id")
    oid_s = str(oid) if oid is not None else None
    return (
        jid,
        oid_s,
        normalized.get("title"),
        normalized.get("jobUrl"),
        normalized.get("location"),
        normalized.get("employmentType"),
        normalized.get("workModel"),
        normalized.get("seniority"),
        normalized.get("experience"),
        normalized.get("originalJobPostUrl"),
        normalized.get("companyName"),
        normalized.get("jobDescription"),
        normalized.get("timestamp"),
        normalized.get("applyStatus"),
        normalized.get("platform"),
    )


def _row_past(doc: dict) -> tuple | None:
    jid = str(doc.get("jobId") or "").strip()
    if not jid:
        return None
    oid = doc.get("_id")
    oid_s = str(oid) if oid is not None else None
    return (
        jid,
        oid_s,
        str(doc.get("platform") or ""),
        str(doc.get("timestamp") or ""),
        str(doc.get("companyName") or ""),
    )


def migrate(output: Path) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    db = getMongoDb()
    conn = sqlite3.connect(output)
    try:
        _ensure_tables(conn)
        conn.execute("DELETE FROM job_data")
        conn.execute("DELETE FROM past_data")

        jobs = list(db[JOB_DATA_COLLECTION].find({}))
        past = list(db[PAST_DATA_COLLECTION].find({}))

        job_docs = [
            d
            for d in jobs
            if str(_mongoDocToJobRow(d).get("jobId") or "").strip()
        ]
        conn.executemany(
            """
            INSERT INTO job_data (
                job_id, mongo_oid, title, job_url, location, employment_type,
                work_model, seniority, experience, original_job_post_url,
                company_name, job_description, timestamp, apply_status, platform
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [_row_job(d) for d in job_docs],
        )

        past_rows = []
        for d in past:
            r = _row_past(d)
            if r:
                past_rows.append(r)
        conn.executemany(
            """
            INSERT INTO past_data (job_id, mongo_oid, platform, timestamp, company_name)
            VALUES (?,?,?,?,?)
            """,
            past_rows,
        )

        conn.commit()
        return len(job_docs), len(past_rows)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export MongoDB chennu collections to SQLite.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=_REPO_ROOT / "zata" / "chennu_export.sqlite",
        help="SQLite file path (default: zata/chennu_export.sqlite)",
    )
    args = parser.parse_args()
    nj, np = migrate(args.output.resolve())
    print(f"Wrote {args.output} — job_data rows: {nj}, past_data rows: {np}")


if __name__ == "__main__":
    main()
