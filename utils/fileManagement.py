from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .dataManager import (
    appendScrapeLog,
    loadJobsByPlatform,
    loadKnownJobIdsByPlatform,
    recordPastData,
    upsertJobs,
)
from .urlCleaner import cleanUrl, normalizeCompanyName

# Project root = parent of utils/ folder.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SCRAPER_SEARCH_KEYWORDS: list[str] = ["devops"]


def resolveScraperSearchKeywords() -> list[str]:
    """
    Comma- or pipe-separated terms from env SCRAPER_SEARCH_KEYWORDS.
    Same list is used by all platform scrapers for sequential keyword phases.
    """
    raw = os.getenv("SCRAPER_SEARCH_KEYWORDS")
    if isinstance(raw, str) and raw.strip():
        parts = [p.strip() for p in re.split(r"[,|]", raw) if p.strip()]
        if parts:
            return parts
    return list(DEFAULT_SCRAPER_SEARCH_KEYWORDS)


CORE_JOB_FIELDS: tuple[str, ...] = (
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
)
OPTIONAL_JOB_FIELDS: tuple[str, ...] = (
    "postedAgo",
    "postedOn",
    "timestamp",
    "applyStatus",
    "platform",
)

TARGET_PORTAL_DOMAINS = ("indeed.com", "linkedin.com", "jobright.ai")
ORIGINAL_URL_SKIP_KEY = "skippedOriginalUrlIds"


def domainFromUrl(url: object) -> str:
    if not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        host = (urlparse(raw).hostname or "").strip(".").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def isBlockedDomain(host: str) -> bool:
    if not host:
        return False
    for blocked in TARGET_PORTAL_DOMAINS:
        if host == blocked or host.endswith(f".{blocked}"):
            return True
    return False


def shouldSkipJob(job: object) -> bool:
    if not isinstance(job, dict):
        return True
    host = domainFromUrl(job.get("originalJobPostUrl"))
    platform = str(job.get("platform") or "").strip()
    # LinkedIn "Apply" often redirects to Indeed; those are still valid off-site applies for our pipeline.
    if platform == "LinkedIn" and host and (
        host == "indeed.com" or host.endswith(".indeed.com")
    ):
        return False
    return isBlockedDomain(host)


def addJobIdToSkipBucket(
    data: dict,
    job: dict,
    *,
    idKey: str = "jobId",
    skipKey: str = ORIGINAL_URL_SKIP_KEY,
) -> bool:
    bucket = data.get(skipKey)
    if not isinstance(bucket, list):
        bucket = []
        data[skipKey] = bucket
    existing = {str(x).strip() for x in bucket if str(x).strip()}
    jid = job.get(idKey)
    if not isinstance(jid, str) or not jid.strip():
        return False
    cleanId = jid.strip()
    if cleanId in existing:
        return False
    bucket.append(cleanId)
    return True


def resolveJobsOutputDirectory() -> Path:
    # DB-only mode: this is just a lightweight source-label directory.
    return _PROJECT_ROOT / "zata" / "sources"


def resolveOutputJsonPath(path: Path | str) -> Path:
    p = Path(path)
    if not str(p).strip():
        raise ValueError("Source path must not be empty.")
    if p.is_absolute():
        return p.expanduser().resolve()
    return (resolveJobsOutputDirectory() / p).resolve()


def _utcNowIso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _isoFromDt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _estimateTimestampFromPostedAgo(postedAgo: str | None) -> str | None:
    if not postedAgo:
        return None
    text = str(postedAgo).strip().lower()
    if not text:
        return None

    # Common prefixes in feeds, e.g. "Reposted 3 hours ago".
    text = re.sub(r"^reposted\s+", "", text).strip()

    now = datetime.now(timezone.utc)
    if text in {"just now", "now"}:
        return _isoFromDt(now)
    if text in {"today"}:
        return _isoFromDt(now)
    if text in {"yesterday"}:
        return _isoFromDt(now - timedelta(days=1))

    match = re.search(
        r"(\d+)\s*(minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago",
        text,
    )
    if not match:
        return None

    qty = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("minute"):
        dt = now - timedelta(minutes=qty)
    elif unit.startswith("hour"):
        dt = now - timedelta(hours=qty)
    elif unit.startswith("day"):
        dt = now - timedelta(days=qty)
    elif unit.startswith("week"):
        dt = now - timedelta(weeks=qty)
    else:
        # Month is approximate for relative feeds.
        dt = now - timedelta(days=qty * 30)
    return _isoFromDt(dt)


def inferPlatformFromPath(path: Path) -> str:
    name = path.name.lower()
    if "jobright" in name:
        return "JobRight"
    if "glassdoor" in name:
        return "GlassDoor"
    if "ziprecruiter" in name:
        return "ZipRecruiter"
    if "linkedin" in name:
        return "LinkedIn"
    return "Unknown"


def _strOrBlank(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


MIN_JOB_TITLE_LENGTH = 5


def isAcceptableJobTitle(title: object) -> bool:
    """Titles must be non-empty after strip and at least MIN_JOB_TITLE_LENGTH characters."""
    return len(_strOrBlank(title)) >= MIN_JOB_TITLE_LENGTH


def normalizeQualificationTags(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    return str(value).strip()


def buildJobDescriptionFromParts(
    salaryRange: object,
    visaOrMatchNote: object,
    jobResponsibility: object,
    qualificationTags: object,
) -> str:
    salary = _strOrBlank(salaryRange)
    visa = _strOrBlank(visaOrMatchNote)
    responsibility = _strOrBlank(jobResponsibility)
    tagsCsv = normalizeQualificationTags(qualificationTags)

    parts: list[str] = []
    if salary:
        parts.append(f"Compensation: {salary}")
    if visa:
        parts.append(f"Visa / match note: {visa}")
    if responsibility:
        parts.append(responsibility)
    if tagsCsv:
        tags = [t.strip() for t in tagsCsv.split(",") if t.strip()]
        if tags:
            parts.append("## Skills\n" + "\n".join(f"- {tag}" for tag in tags))
    return "\n\n".join(parts).strip()


def normalizeJobRecord(job: dict) -> dict:
    normalized: dict[str, object] = dict.fromkeys(CORE_JOB_FIELDS, "")
    qualificationTags = normalizeQualificationTags(job.get("qualificationTags"))
    salaryRange = _strOrBlank(job.get("salaryRange"))
    visaOrMatchNote = _strOrBlank(job.get("visaOrMatchNote"))
    jobResponsibility = _strOrBlank(job.get("jobResponsibility"))

    keyAliases = {
        "companyName": ("companyName", "company"),
        "jobResponsibility": ("jobResponsibility",),
    }

    for key in CORE_JOB_FIELDS:
        sourceKeys = keyAliases.get(key, (key,))
        value: object = ""
        for sourceKey in sourceKeys:
            if sourceKey in job:
                value = job.get(sourceKey)
                break
        normalized[key] = _strOrBlank(value)
    normalized["companyName"] = normalizeCompanyName(normalized.get("companyName"))
    normalized["jobUrl"] = cleanUrl(normalized.get("jobUrl"))
    normalized["originalJobPostUrl"] = cleanUrl(normalized.get("originalJobPostUrl"))

    for key in OPTIONAL_JOB_FIELDS:
        if key in job:
            if key == "applyStatus":
                st = _strOrBlank(job.get(key))
                if st:
                    normalized[key] = st
            else:
                normalized[key] = _strOrBlank(job.get(key))
    if not _strOrBlank(normalized.get("timestamp")):
        estimated = _estimateTimestampFromPostedAgo(
            _strOrBlank(normalized.get("postedAgo"))
        )
        normalized["timestamp"] = estimated or _utcNowIso()

    existingDescription = _strOrBlank(job.get("jobDescription"))
    rebuiltDescription = buildJobDescriptionFromParts(
        salaryRange,
        visaOrMatchNote,
        jobResponsibility,
        qualificationTags,
    )
    hasFreshDetailContent = bool(jobResponsibility or qualificationTags)
    if hasFreshDetailContent and rebuiltDescription:
        # Prefer freshly scraped detail text over stale list-only description.
        normalized["jobDescription"] = rebuiltDescription
    else:
        normalized["jobDescription"] = existingDescription or rebuiltDescription
    return normalized


def _preferRicherJob(existing: dict, candidate: dict) -> dict:
    merged = dict(existing)
    for key, val in candidate.items():
        if key not in merged:
            merged[key] = val
            continue
        existingVal = _strOrBlank(merged.get(key))
        candidateVal = _strOrBlank(val)
        if not existingVal and candidateVal:
            merged[key] = val
    # Prefer real http(s) apply URLs over placeholder text; never replace a URL with a label.
    exUrl = _strOrBlank(merged.get("originalJobPostUrl"))
    candUrl = _strOrBlank(candidate.get("originalJobPostUrl"))
    if candUrl.startswith("http") or (candUrl and not exUrl.startswith("http")):
        merged["originalJobPostUrl"] = candidate.get("originalJobPostUrl")
    if len(_strOrBlank(candidate.get("jobDescription"))) > len(
        _strOrBlank(merged.get("jobDescription"))
    ):
        merged["jobDescription"] = candidate.get("jobDescription")
    return merged


def _isCompleteForDb(job: dict) -> bool:
    required = (
        "jobId",
        "jobUrl",
        "companyName",
        "jobDescription",
        "originalJobPostUrl",
    )
    if not all(_strOrBlank(job.get(key)) for key in required):
        return False
    return bool(cleanUrl(job.get("jobUrl"))) and bool(
        cleanUrl(job.get("originalJobPostUrl"))
    )


def isCompleteJobRow(job: dict) -> bool:
    """
    True when detail scrape is done: either normalized for DB load, or still in
    memory with jobResponsibility before saveOutputDocument copies it into
    jobDescription.
    """
    if not isinstance(job, dict):
        return False
    if _isCompleteForDb(job):
        return True
    resp = str(job.get("jobResponsibility") or "").strip()
    if len(resp) < 30:
        return False
    return bool(
        cleanUrl(job.get("jobUrl"))
        and cleanUrl(job.get("originalJobPostUrl"))
        and _strOrBlank(job.get("companyName"))
    )


def _missingCompleteFields(job: dict) -> list[str]:
    required = (
        "jobId",
        "jobUrl",
        "companyName",
        "jobDescription",
        "originalJobPostUrl",
    )
    missing: list[str] = []
    for key in required:
        if not _strOrBlank(job.get(key)):
            missing.append(key)
    if "jobUrl" not in missing and not bool(cleanUrl(job.get("jobUrl"))):
        missing.append("jobUrl(invalid_url)")
    if "originalJobPostUrl" not in missing and not bool(
        cleanUrl(job.get("originalJobPostUrl"))
    ):
        missing.append("originalJobPostUrl(invalid_url)")
    return missing


def saveJsonPayload(path: Path, payload: object) -> tuple[bool, str]:
    try:
        obj: object = payload
        if not isinstance(obj, (dict, list)):
            obj = {"data": payload}
        platform = inferPlatformFromPath(path)
        appendScrapeLog(
            f"Skipped JSON write for payload at {path.name}; mode=db_only; payloadType={type(obj).__name__}",
            platform=platform,
        )
    except (OSError, TypeError) as exc:
        return False, f"Failed to log payload event: {exc}"
    return True, f"Payload event logged (db_only mode): {path}"


def loadExistingJobsAndMeta(outputPath: Path) -> tuple[list[dict], dict]:
    platform = inferPlatformFromPath(outputPath)
    jobs = loadJobsByPlatform(platform)
    return jobs, {
        ORIGINAL_URL_SKIP_KEY: [],
        "_platform": platform,
        "_sourcePath": str(outputPath),
    }


def loadJobsDocumentOrEmpty(path: Path) -> dict:
    platform = inferPlatformFromPath(path)
    jobs = loadJobsByPlatform(platform)
    return {
        "jobs": jobs,
        ORIGINAL_URL_SKIP_KEY: [],
        "count": len(jobs),
        "_platform": platform,
        "_sourcePath": str(path),
    }


def mergeJobListsById(
    existing: list[dict],
    incoming: list[dict],
    *,
    idKey: str = "jobId",
    platform: str = "Unknown",
) -> tuple[list[dict], int, int]:
    seen: set[str] = set(loadKnownJobIdsByPlatform(platform))
    for j in existing:
        jid = j.get(idKey)
        if isinstance(jid, str) and jid:
            seen.add(jid)

    appended: list[dict] = []
    filteredSkipRows: list[dict] = []
    skipped = 0
    for j in incoming:
        if not isinstance(j, dict):
            skipped += 1
            continue
        normalized = normalizeJobRecord(j)
        jid = normalized.get(idKey)
        if not jid or not isinstance(jid, str):
            skipped += 1
            continue
        normalized["platform"] = normalized.get("platform") or platform
        normalized["timestamp"] = normalized.get("timestamp") or _utcNowIso()
        if not bool(cleanUrl(normalized.get("jobUrl"))):
            skipped += 1
            continue
        if shouldSkipJob(normalized):
            filteredSkipRows.append(normalized)
            skipped += 1
            continue
        if not isAcceptableJobTitle(normalized.get("title")):
            skipped += 1
            continue
        if jid in seen:
            skipped += 1
            continue
        seen.add(jid)
        appended.append(normalized)
    if filteredSkipRows:
        recordPastData(filteredSkipRows, platform=platform)
    return existing + appended, skipped, len(appended)


def mergeNewJobsIntoDocument(
    data: dict,
    newRows: list[dict],
    *,
    idKey: str = "jobId",
) -> tuple[int, int]:
    platform = inferPlatformFromPath(Path(str(data.get("_sourcePath") or "")))
    if platform == "Unknown":
        platform = str(data.get("_platform") or "Unknown")
    jobs = data.setdefault("jobs", [])
    if not isinstance(jobs, list):
        data["jobs"] = []
        jobs = data["jobs"]
    seen = set(loadKnownJobIdsByPlatform(platform))
    seen.update({j.get(idKey) for j in jobs if isinstance(j, dict) and j.get(idKey)})
    added = 0
    skipped = 0
    filteredSkipRows: list[dict] = []
    for row in newRows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        row = normalizeJobRecord(row)
        jid = row.get(idKey)
        if not jid or jid in seen:
            row["platform"] = row.get("platform") or platform
            row["timestamp"] = row.get("timestamp") or _utcNowIso()
            skipped += 1
            continue
        row["platform"] = row.get("platform") or platform
        row["timestamp"] = row.get("timestamp") or _utcNowIso()
        if not bool(cleanUrl(row.get("jobUrl"))):
            skipped += 1
            continue
        if shouldSkipJob(row):
            addJobIdToSkipBucket(data, row, idKey=idKey, skipKey=ORIGINAL_URL_SKIP_KEY)
            filteredSkipRows.append(row)
            skipped += 1
            continue
        if not isAcceptableJobTitle(row.get("title")):
            skipped += 1
            continue
        jobs.append(row)
        seen.add(jid)
        added += 1
    if filteredSkipRows:
        recordPastData(filteredSkipRows, platform=platform)
    return added, skipped


def saveOutputDocument(path: Path, data: dict) -> None:
    jobs = data.get("jobs")
    sourcePlatform = inferPlatformFromPath(path)
    scrapeTimestamp = _utcNowIso()
    data["_platform"] = sourcePlatform
    data["_sourcePath"] = str(path)
    if isinstance(jobs, list):
        filtered: list[dict] = []
        filteredSkipRows: list[dict] = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            normalized = normalizeJobRecord(j)
            if not normalized.get("platform"):
                normalized["platform"] = sourcePlatform
            if not normalized.get("timestamp"):
                normalized["timestamp"] = scrapeTimestamp
            if not bool(cleanUrl(normalized.get("jobUrl"))):
                continue
            if bool(cleanUrl(normalized.get("originalJobPostUrl"))) and shouldSkipJob(
                normalized
            ):
                addJobIdToSkipBucket(
                    data, normalized, idKey="jobId", skipKey=ORIGINAL_URL_SKIP_KEY
                )
                filteredSkipRows.append(normalized)
                continue
            filtered.append(normalized)
        byId: dict[str, dict] = {}
        deduped: list[dict] = []
        for row in filtered:
            jid = _strOrBlank(row.get("jobId"))
            if not jid:
                continue
            if jid in byId:
                merged = _preferRicherJob(byId[jid], row)
                byId[jid] = merged
                for i, existingRow in enumerate(deduped):
                    if _strOrBlank(existingRow.get("jobId")) == jid:
                        deduped[i] = merged
                        break
            else:
                byId[jid] = row
                deduped.append(row)
        data["jobs"] = deduped
        data["count"] = len(data["jobs"])
        if filteredSkipRows:
            recordPastData(filteredSkipRows, platform=sourcePlatform)
    if isinstance(jobs, list):
        completeRows = [
            j
            for j in data.get("jobs", [])
            if isinstance(j, dict)
            and _isCompleteForDb(j)
            and isAcceptableJobTitle(j.get("title"))
        ]
        incompleteRows = [
            j
            for j in data.get("jobs", [])
            if isinstance(j, dict) and not _isCompleteForDb(j)
        ]
        upserted = upsertJobs(completeRows)
        pastAdded = recordPastData(completeRows, platform=sourcePlatform)
        sampleReasons: list[str] = []
        for row in incompleteRows[:10]:
            jid = _strOrBlank(row.get("jobId")) or "<missing-jobId>"
            missing = ",".join(_missingCompleteFields(row))
            sampleReasons.append(f"{jid}:{missing}")
        reasonBlob = " | ".join(sampleReasons) if sampleReasons else "-"
        appendScrapeLog(
            f"Saved to DB only; source={path.name}; jobs={len(data.get('jobs', []))}; complete={len(completeRows)}; incomplete={len(incompleteRows)}; upserted={upserted}; pastDataAdded={pastAdded}; skippedIds={len(data.get(ORIGINAL_URL_SKIP_KEY, []))}; incompleteSamples={reasonBlob}",
            platform=sourcePlatform,
        )


def loadOutputDocument(path: Path | str) -> tuple[Path, dict]:
    p = resolveOutputJsonPath(path)
    data = loadJobsDocumentOrEmpty(p)
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("No jobs found in DB for this source.")
    return p, data


# Backwards-compatible name used by Jobright fetch merge.
mergeFetchedJobs = mergeJobListsById
