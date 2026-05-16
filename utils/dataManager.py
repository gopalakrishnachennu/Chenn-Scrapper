from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

JOB_DATA_COLLECTION = "jobData"
PAST_DATA_COLLECTION = "pastData"

_mongo_client: Any = None
_mongo_db: Any = None


def _job_storage_backend() -> str:
    raw = (os.getenv("JOB_STORAGE_BACKEND") or "").strip().lower()
    if raw in ("sqlite", "sql"):
        return "sqlite"
    return "mongo"


def _getMongoDb():
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        return _mongo_db
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise ImportError(
            "Install pymongo and dnspython: pip install 'pymongo>=4.6,<5' 'dnspython>=2.0.0,<3'"
        ) from exc

    uri = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if not uri:
        raise ValueError("Set MONGODB_URI in .env")
    db_name = (
        (os.getenv("MONGODB_DATABASE") or os.getenv("MONGODB_DB_NAME") or "").strip()
        or "chennuJobViewer"
    )
    _mongo_client = MongoClient(uri)
    _mongo_db = _mongo_client[db_name]
    return _mongo_db


def getMongoDb():
    """Primary pymongo Database (same connection as all job/past helpers)."""
    return _getMongoDb()


def _projectRoot() -> Path:
    return Path(__file__).resolve().parent.parent


def _logsDirectory() -> Path:
    return _projectRoot() / "zata" / "logs"


def _utcNowIso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def appendScrapeLog(message: str, *, platform: str = "Unknown") -> Path:
    logsDir = _logsDirectory()
    logsDir.mkdir(parents=True, exist_ok=True)
    logPath = logsDir / f"scrape-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
    line = f"[{_utcNowIso()}] [{platform}] {message}\n"
    with logPath.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return logPath


def _mongoEnsureIndexes(recreate: bool) -> None:
    db = _getMongoDb()
    if recreate:
        names = set(db.list_collection_names())
        if JOB_DATA_COLLECTION in names:
            db[JOB_DATA_COLLECTION].drop()
        if PAST_DATA_COLLECTION in names:
            db[PAST_DATA_COLLECTION].drop()
    job_col = db[JOB_DATA_COLLECTION]
    past_col = db[PAST_DATA_COLLECTION]
    job_col.create_index("jobId", unique=True)
    past_col.create_index("jobId", unique=True)
    job_col.create_index("platform")
    past_col.create_index("platform")


def createTables(*, recreate: bool = False) -> None:
    """Ensure MongoDB collections or SQLite tables exist."""
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import ensure_store

        ensure_store(recreate=recreate)
        return
    _mongoEnsureIndexes(recreate)


def _applyStatusParam(row: dict) -> str | None:
    """Unset / blank -> None; classifier or manual statuses stay as non-empty strings."""
    if "applyStatus" not in row:
        return None
    raw = row.get("applyStatus")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _mongoDocToJobRow(doc: dict) -> dict:
    keys = (
        "jobId",
        "title",
        "jobUrl",
        "location",
        "employmentType",
        "workModel",
        "seniority",
        "experience",
        "originalJobPostUrl",
        "companyName",
        "jobDescription",
        "timestamp",
        "applyStatus",
        "platform",
    )
    out: dict[str, Any] = {}
    for k in keys:
        v = doc.get(k)
        if v is None:
            out[k] = None
        elif k == "applyStatus" and v == "":
            out[k] = ""
        else:
            out[k] = v if isinstance(v, str) else str(v)
    return out


def upsertJobs(rows: list[dict]) -> int:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import upsert_jobs

        return upsert_jobs(rows)
    if not rows:
        return 0
    from pymongo import UpdateOne

    createTables(recreate=False)
    coll = _getMongoDb()[JOB_DATA_COLLECTION]
    ops: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        jid = str(row.get("jobId") or "").strip()
        if not jid:
            continue
        apply_val = _applyStatusParam(row)
        set_doc: dict[str, Any] = {
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
            "timestamp": str(row.get("timestamp") or _utcNowIso()),
            "platform": str(row.get("platform") or "Unknown"),
        }
        if apply_val is not None:
            set_doc["applyStatus"] = apply_val
        ops.append(UpdateOne({"jobId": jid}, {"$set": set_doc}, upsert=True))
    if not ops:
        return 0
    coll.bulk_write(ops, ordered=False)
    return len(ops)


def loadJobsByPlatform(platform: str) -> list[dict]:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import load_jobs_by_platform

        return load_jobs_by_platform(platform)
    createTables(recreate=False)
    cur = _getMongoDb()[JOB_DATA_COLLECTION].find({"platform": platform})
    return [_mongoDocToJobRow(d) for d in cur]


def loadAllJobs() -> list[dict]:
    """All rows in jobData, FIFO by timestamp (oldest first), then jobId."""
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import load_all_jobs

        return load_all_jobs()
    createTables(recreate=False)
    cur = _getMongoDb()[JOB_DATA_COLLECTION].find({})
    jobs = [_mongoDocToJobRow(d) for d in cur]
    return sortJobsFifoByTimestamp(jobs)


def sortJobsFifoByTimestamp(jobs: list[dict]) -> list[dict]:
    def sortKey(row: dict) -> tuple[int, str, str]:
        ts = str(row.get("timestamp") or "").strip()
        return (1 if not ts else 0, ts, str(row.get("jobId") or ""))

    return sorted(jobs, key=sortKey)


def loadJobsWithEmptyApplyStatus(platform: str | None = None) -> list[dict]:
    """
    Jobs where applyStatus is null/missing only.
    Ordered FIFO: oldest timestamp first; rows with no timestamp sort last, then jobId.
    """
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import load_jobs_with_empty_apply_status

        return load_jobs_with_empty_apply_status(platform)
    createTables(recreate=False)
    query: dict[str, Any] = {"applyStatus": None}
    if platform:
        query["platform"] = platform
    cur = _getMongoDb()[JOB_DATA_COLLECTION].find(query)
    jobs = [_mongoDocToJobRow(d) for d in cur]
    return sortJobsFifoByTimestamp(jobs)


def updateApplyStatusByJobId(jobId: str, applyStatus: str) -> bool:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import update_apply_status_by_job_id

        return update_apply_status_by_job_id(jobId, applyStatus)
    jid = str(jobId or "").strip()
    status = str(applyStatus or "").strip()
    if not jid:
        return False
    createTables(recreate=False)
    res = _getMongoDb()[JOB_DATA_COLLECTION].update_one(
        {"jobId": jid}, {"$set": {"applyStatus": status}}
    )
    return res.matched_count > 0


def loadJobsByApplyStatus(applyStatus: str) -> list[dict]:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import load_jobs_by_apply_status

        return load_jobs_by_apply_status(applyStatus)
    status = str(applyStatus or "").strip()
    if not status:
        return []
    createTables(recreate=False)
    cur = _getMongoDb()[JOB_DATA_COLLECTION].find({"applyStatus": status})
    jobs = [_mongoDocToJobRow(d) for d in cur]
    return sortJobsFifoByTimestamp(jobs)


def jobDataApplyStatusSummary() -> dict[str, int]:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import job_data_apply_status_summary

        return job_data_apply_status_summary()
    createTables(recreate=False)
    db = _getMongoDb()
    job_col = db[JOB_DATA_COLLECTION]
    past_col = db[PAST_DATA_COLLECTION]
    total = job_col.count_documents({})
    past_n = past_col.count_documents({})

    def _trim_status(doc: dict) -> str:
        return str(doc.get("applyStatus") or "").strip()

    pending = 0
    n_apply = 0
    n_dna = 0
    n_ex = 0
    n_other = 0
    for doc in job_col.find({}):
        s = _trim_status(doc)
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


def deleteJobsByApplyStatusNotIn(allowedStatuses: list[str] | tuple[str, ...]) -> int:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import delete_jobs_by_apply_status_not_in

        return delete_jobs_by_apply_status_not_in(allowedStatuses)
    normalized = sorted(
        {str(item or "").strip() for item in allowedStatuses if str(item or "").strip()}
    )
    if not normalized:
        raise ValueError("allowedStatuses must include at least one non-empty status")

    createTables(recreate=False)
    coll = _getMongoDb()[JOB_DATA_COLLECTION]
    to_delete: list[str] = []
    for doc in coll.find(
        {"applyStatus": {"$exists": True, "$nin": [None, ""]}},
        {"jobId": 1, "applyStatus": 1},
    ):
        s = str(doc.get("applyStatus") or "").strip()
        if s and s not in normalized:
            jid = str(doc.get("jobId") or "").strip()
            if jid:
                to_delete.append(jid)
    if not to_delete:
        return 0
    res = coll.delete_many({"jobId": {"$in": to_delete}})
    return int(res.deleted_count or 0)


def _parseStoredTimestampToUtc(raw: object) -> datetime | None:
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


def deletePastDataOlderThanHours(*, hours: float = 48) -> int:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import delete_past_data_older_than_hours

        return delete_past_data_older_than_hours(hours=hours)
    if hours <= 0:
        raise ValueError("hours must be positive")
    createTables(recreate=False)
    coll = _getMongoDb()[PAST_DATA_COLLECTION]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale_ids: list[str] = []
    for doc in coll.find(
        {"timestamp": {"$exists": True, "$nin": [None, ""]}},
        {"jobId": 1, "timestamp": 1},
    ):
        jid = str(doc.get("jobId") or "").strip()
        parsed = _parseStoredTimestampToUtc(doc.get("timestamp"))
        if parsed is not None and parsed < cutoff and jid:
            stale_ids.append(jid)
    if not stale_ids:
        return 0
    res = coll.delete_many({"jobId": {"$in": stale_ids}})
    return int(res.deleted_count or 0)


def loadKnownJobIdsByPlatform(platform: str) -> set[str]:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import load_known_job_ids_by_platform

        return load_known_job_ids_by_platform(platform)
    createTables(recreate=False)
    db = _getMongoDb()
    job_ids = {
        str(d["jobId"]).strip()
        for d in db[JOB_DATA_COLLECTION].find({"platform": platform}, {"jobId": 1})
        if d.get("jobId")
    }
    past_ids = {
        str(d["jobId"]).strip()
        for d in db[PAST_DATA_COLLECTION].find({"platform": platform}, {"jobId": 1})
        if d.get("jobId")
    }
    return job_ids | past_ids


def recordPastData(rows: list[dict], *, platform: str) -> int:
    if _job_storage_backend() == "sqlite":
        from utils.sqlite_job_store import record_past_data

        return record_past_data(rows, platform=platform)
    if not rows:
        return 0
    from pymongo import UpdateOne

    createTables(recreate=False)
    coll = _getMongoDb()[PAST_DATA_COLLECTION]
    now = _utcNowIso()
    ops: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("jobId") or "").strip()
        company_name = str(row.get("companyName") or "").strip() or "Unknown"
        if not job_id:
            continue
        ts = str(row.get("timestamp") or now).strip() or now
        doc = {
            "jobId": job_id,
            "platform": platform,
            "timestamp": ts,
            "companyName": company_name,
        }
        ops.append(
            UpdateOne(
                {"jobId": job_id},
                {"$set": doc},
                upsert=True,
            )
        )
    if not ops:
        return 0
    coll.bulk_write(ops, ordered=False)
    return len(ops)
