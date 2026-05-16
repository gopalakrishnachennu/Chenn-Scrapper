"""SQLite DDL shared by utils.sqlite_job_store and scripts/mongo_to_sqlite."""

from __future__ import annotations

import sqlite3

SQLITE_JOB_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS job_data (
    job_id TEXT PRIMARY KEY NOT NULL,
    mongo_oid TEXT,
    title TEXT,
    job_url TEXT,
    location TEXT,
    employment_type TEXT,
    work_model TEXT,
    seniority TEXT,
    experience TEXT,
    original_job_post_url TEXT,
    company_name TEXT,
    job_description TEXT,
    timestamp TEXT,
    apply_status TEXT,
    platform TEXT
);

CREATE TABLE IF NOT EXISTS past_data (
    job_id TEXT PRIMARY KEY NOT NULL,
    mongo_oid TEXT,
    platform TEXT,
    timestamp TEXT,
    company_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_data_platform ON job_data(platform);
CREATE INDEX IF NOT EXISTS idx_past_data_platform ON past_data(platform);
"""


def ensure_sqlite_job_tables(conn: sqlite3.Connection, *, recreate: bool = False) -> None:
    if recreate:
        conn.execute("DROP TABLE IF EXISTS job_data")
        conn.execute("DROP TABLE IF EXISTS past_data")
    conn.executescript(SQLITE_JOB_TABLES_DDL)
