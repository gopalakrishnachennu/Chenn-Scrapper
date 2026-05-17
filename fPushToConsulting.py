"""
Push all scraped jobs from local DB to chennu.co consulting platform.

Maps scraper field formats to Django's RawJob schema with all corrections:
  - employment_type:  "Full-time"         → "FULL_TIME"
  - experience_level: "Mid, Senior Level" → "SENIOR"  (takes highest)
  - location_type:    "Hybrid"            → "HYBRID"  (not just is_remote)
  - location:         "US-TX-Austin"      → city/state/country split
  - years_required:   "3+ years exp"      → 3 (int)
  - platform_slug:    detected from original_url ATS (e.g. Greenhouse, Workday)
                      NOT from scraper source (LinkedIn/ZipRecruiter).
                      Scraper source stored in raw_payload for reference.

Why ATS detection matters:
  A job scraped from LinkedIn may actually be hosted on Workday/Greenhouse/etc.
  Tagging it with the real ATS platform lets chennu.co's harvest engine:
    1. Show it under the correct platform in the dashboard
    2. Associate the company with that ATS
    3. Enable future direct harvesting of ALL jobs from that company

Tracks pushed job IDs in zata/pushed_job_ids.json — safe to re-run.

Usage:
  python fPushToConsulting.py             # push all new APPLY jobs
  python fPushToConsulting.py --dry-run   # preview payloads, no HTTP call
  python fPushToConsulting.py --reset     # clear pushed-IDs tracking and re-push all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
PUSHED_IDS_PATH = REPO_ROOT / "zata" / "pushed_job_ids.json"

CONSULTING_API_URL = (os.getenv("CONSULTING_API_URL") or "https://chennu.co").rstrip("/")
HARVEST_PUSH_SECRET = (os.getenv("HARVEST_PUSH_SECRET") or "").strip()
PUSH_BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# Scraper source → slug (used only as fallback when ATS can't be detected)
_SCRAPER_SOURCE_SLUG: dict[str, str] = {
    "LinkedIn":     "linkedin",
    "ZipRecruiter": "ziprecruiter",
    "GlassDoor":    "glassdoor",
    "JobRight":     "jobright",
}

# URL pattern → ATS platform slug (mirrors PLATFORM_PATTERNS in consulting/jarvis.py)
_ATS_PATTERNS: dict[str, list[str]] = {
    "greenhouse":      ["boards.greenhouse.io", "boards-api.greenhouse.io"],
    "lever":           ["jobs.lever.co"],
    "ashby":           ["jobs.ashbyhq.com", "ashbyhq.com/jobs"],
    "workday":         ["myworkdayjobs.com"],
    "smartrecruiters": ["smartrecruiters.com/jobs", "jobs.smartrecruiters.com", "careers.smartrecruiters.com"],
    "workable":        ["apply.workable.com", "jobs.workable.com"],
    "bamboohr":           ["bamboohr.com/careers", "bamboohr.com/jobs"],
    "recruitee":          [".recruitee.com/o/", "recruitee.com/o/"],
    "icims":              [".icims.com/jobs/", "icims.com/jobs"],
    "jobvite":            ["jobs.jobvite.com"],
    "taleo":              ["taleo.net/careersection"],
    "oracle":             [".oraclecloud.com/hcmUI/CandidateExperience"],
    "ultipro":            ["recruiting.ultipro.com", "recruiting.ukg.net"],
    "dayforce":           ["jobs.dayforcehcm.com"],
    "breezy":             [".breezy.hr/p/"],
    "teamtailor":         [".teamtailor.com/jobs/", ".teamtailor.com"],
    "zoho":               ["jobs.zoho.com/portal/", ".zohorecruit.com/jobs/"],
    "applytojob":         ["applytojob.com/apply"],
    "adp":                ["workforcenow.adp.com", "myjobs.adp.com"],
    "applicantpro":        ["applicantpro.com"],
    "theapplicantmanager": ["theapplicantmanager.com"],
}

_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "full-time":  "FULL_TIME",
    "contract":   "CONTRACT",
    "w2":         "FULL_TIME",
    "part-time":  "PART_TIME",
    "internship": "INTERNSHIP",
    "temporary":  "TEMPORARY",
}

# (token_in_seniority_string, django_value, rank) — higher rank wins
_SENIORITY_RULES: list[tuple[str, str, int]] = [
    ("new grad",    "ENTRY",  0),
    ("entry level", "ENTRY",  1),
    ("entry",       "ENTRY",  1),
    ("mid level",   "MID",    2),
    ("mid",         "MID",    2),
    ("senior level","SENIOR", 3),
    ("senior",      "SENIOR", 3),
    ("lead/staff",  "LEAD",   4),
    ("lead",        "LEAD",   4),
    ("staff",       "LEAD",   4),
]

# work_model string → (location_type, is_remote)
_WORK_MODEL_MAP: dict[str, tuple[str, bool]] = {
    "Remote": ("REMOTE",  True),
    "Hybrid": ("HYBRID",  False),
    "Onsite": ("ONSITE",  False),
}

_COUNTRY_NAMES: dict[str, str] = {
    "united states": "US",
    "usa":           "US",
    "us":            "US",
    "canada":        "CA",
    "united kingdom":"GB",
    "uk":            "GB",
    "remote":        "US",  # scraper only runs US searches
}


# ---------------------------------------------------------------------------
# Field converters
# ---------------------------------------------------------------------------

def _detect_ats_platform(original_url: str) -> str:
    """
    Detect the real ATS platform from the company's job URL.
    Returns platform slug (e.g. 'greenhouse', 'workday') or '' if unknown.
    Jobs scraped from LinkedIn/ZipRecruiter often link to the company's ATS directly.
    """
    lower = (original_url or "").lower()
    for slug, patterns in _ATS_PATTERNS.items():
        if any(p in lower for p in patterns):
            return slug
    return ""


def _map_employment_type(val: str) -> str:
    return _EMPLOYMENT_TYPE_MAP.get((val or "").strip().lower(), "UNKNOWN")


def _map_experience_level(seniority: str) -> str:
    """Take the highest seniority from a comma-separated scraper string."""
    raw = (seniority or "").strip().lower()
    if not raw:
        return "UNKNOWN"
    best_rank = -1
    best_val = "UNKNOWN"
    for token, django_val, rank in _SENIORITY_RULES:
        if token in raw and rank > best_rank:
            best_rank = rank
            best_val = django_val
    return best_val


def _map_work_model(work_model: str) -> tuple[str, bool]:
    """Returns (location_type, is_remote). Unknown → ("UNKNOWN", False)."""
    return _WORK_MODEL_MAP.get((work_model or "").strip(), ("UNKNOWN", False))


def _parse_years(experience: str) -> int | None:
    """'3+ years exp' → 3,  '' → None."""
    if not experience:
        return None
    m = re.search(r'(\d+)', experience)
    return int(m.group(1)) if m else None


def _parse_location(raw: str) -> dict[str, str]:
    """
    Parse scraper location strings into city / state / country.

    Handles:
      "US-TX-Austin"         → country=US, state=TX,     city=Austin
      "US-NE-Omaha"          → country=US, state=NE,     city=Omaha
      "New York, NY"          → country=US, state=NY,     city=New York
      "Austin, Texas, US"    → country=US, state=Texas,  city=Austin
      "United States"        → country=US
      "Remote"               → country=US
      "US"                   → country=US
    """
    s = (raw or "").strip()
    if not s:
        return {}

    # "CC-ST-City"  (JobRight / enricher format)
    m = re.match(r'^([A-Z]{2})-([A-Z]{2,3})-(.+)$', s)
    if m:
        return {"country": m.group(1), "state": m.group(2), "city": m.group(3).strip()}

    # "City, State, Country"
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 3:
        return {"city": parts[0], "state": parts[1], "country": parts[2]}

    # "City, ST" or "City, StateName"
    if len(parts) == 2:
        return {"country": "US", "state": parts[1], "city": parts[0]}

    # Plain country / "Remote" / "US" etc.
    key = s.lower()
    if key in _COUNTRY_NAMES:
        return {"country": _COUNTRY_NAMES[key]}
    if re.match(r'^[A-Z]{2}$', s):
        return {"country": s}

    return {}


def _build_payload_job(row: dict) -> dict:
    """Map one scraper job row to the Django push_api payload schema."""
    location_raw = str(row.get("location") or "")
    loc = _parse_location(location_raw)
    location_type, is_remote = _map_work_model(row.get("workModel") or "")
    years = _parse_years(row.get("experience") or "")

    original_url = str(row.get("originalJobPostUrl") or "")
    scraper_source = str(row.get("platform") or "")

    # Detect the real ATS platform from the company's job URL.
    # A job scraped from LinkedIn may actually be on Workday/Greenhouse/etc.
    # Use the ATS platform so chennu.co correctly associates the company.
    # Fall back to the scraper source slug only if ATS is not detectable.
    ats_platform = _detect_ats_platform(original_url)
    platform_slug = ats_platform or _SCRAPER_SOURCE_SLUG.get(scraper_source, "")

    payload: dict = {
        "external_id":      str(row.get("jobId") or ""),
        "original_url":     original_url,
        "apply_url":        str(row.get("jobUrl") or ""),
        "title":            str(row.get("title") or ""),
        "company_name":     str(row.get("companyName") or ""),
        "description":      str(row.get("jobDescription") or ""),
        "location_raw":     location_raw,
        "country":          loc.get("country", "US"),
        "location_type":    location_type,
        "is_remote":        is_remote,
        "employment_type":  _map_employment_type(row.get("employmentType") or ""),
        "experience_level": _map_experience_level(row.get("seniority") or ""),
        "platform_slug":    platform_slug,
        # Keep scraper source in metadata so you know where each job was found
        "raw_payload": {
            "scraper_source":  scraper_source,
            "ats_detected":    ats_platform or None,
            "scraper_job_url": str(row.get("jobUrl") or ""),
        },
    }

    # Optional fields — only include when present
    if loc.get("city"):
        payload["city"] = loc["city"]
    if loc.get("state"):
        payload["state"] = loc["state"]
    if years is not None:
        payload["years_required"] = years

    ts = str(row.get("timestamp") or "")[:10]
    if ts and len(ts) == 10:
        payload["posted_date"] = ts

    return payload


# ---------------------------------------------------------------------------
# Pushed IDs tracking
# ---------------------------------------------------------------------------

def _load_pushed_ids() -> set[str]:
    if PUSHED_IDS_PATH.exists():
        try:
            return set(json.loads(PUSHED_IDS_PATH.read_text()).get("pushed_ids", []))
        except Exception:
            return set()
    return set()


def _save_pushed_ids(ids: set[str]) -> None:
    PUSHED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUSHED_IDS_PATH.write_text(
        json.dumps(
            {
                "pushed_ids": sorted(ids),
                "count": len(ids),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _push_batch(jobs: list[dict]) -> dict:
    resp = requests.post(
        f"{CONSULTING_API_URL}/harvest/api/push/jobs/",
        json={"jobs": jobs, "trigger_pipeline": True},
        headers={
            "Authorization": f"Bearer {HARVEST_PUSH_SECRET}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def push_apply_jobs(*, dry_run: bool = False, reset: bool = False) -> dict:
    if not dry_run and not HARVEST_PUSH_SECRET:
        raise ValueError(
            "HARVEST_PUSH_SECRET not set.\n"
            "Get it from the .env on the server running chennu.co and add it to this .env."
        )

    from utils.dataManager import loadAllJobs

    all_jobs = loadAllJobs()
    pushed_ids = set() if reset else _load_pushed_ids()
    new_jobs = [j for j in all_jobs if str(j.get("jobId") or "") not in pushed_ids]

    print(f"  Total jobs in DB:   {len(all_jobs)}")
    print(f"  Already pushed:     {len(pushed_ids)}")
    print(f"  New jobs to push:   {len(new_jobs)}")

    if not new_jobs:
        print("  Nothing new to push.")
        return {"pushed": 0, "created": 0, "skipped": 0, "errors": 0}

    if dry_run:
        sample = new_jobs[:3]
        print(f"\n  [dry-run] Sample payloads ({len(sample)} of {len(new_jobs)}):\n")
        for row in sample:
            print(json.dumps(_build_payload_job(row), indent=2))
        print(f"\n  [dry-run] No HTTP calls made.")
        return {"pushed": 0, "created": 0, "skipped": 0, "errors": 0}

    batches = [new_jobs[i : i + PUSH_BATCH_SIZE] for i in range(0, len(new_jobs), PUSH_BATCH_SIZE)]
    print(f"\n  Sending {len(new_jobs)} jobs in {len(batches)} batch(es) → {CONSULTING_API_URL}\n")

    total = {"created": 0, "skipped": 0, "errors": 0}
    newly_pushed: set[str] = set()

    for i, batch in enumerate(batches, 1):
        payloads = [_build_payload_job(row) for row in batch]
        try:
            result = _push_batch(payloads)
            total["created"] += result.get("created", 0)
            total["skipped"] += result.get("skipped", 0)
            total["errors"]  += result.get("errors", 0)
            for row in batch:
                newly_pushed.add(str(row.get("jobId") or ""))
            print(
                f"  Batch {i}/{len(batches)}: "
                f"created={result.get('created',0)}  "
                f"skipped={result.get('skipped',0)}  "
                f"errors={result.get('errors',0)}"
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code
            body = exc.response.text[:300]
            print(f"  Batch {i}/{len(batches)}: HTTP {status} — {body}")
            if status == 401:
                print("  ✖  Wrong HARVEST_PUSH_SECRET. Aborting.")
                break
            total["errors"] += len(batch)
        except requests.ConnectionError:
            print(f"  Batch {i}/{len(batches)}: connection error — is chennu.co reachable?")
            total["errors"] += len(batch)
        except Exception as exc:
            print(f"  Batch {i}/{len(batches)}: unexpected error — {exc}")
            total["errors"] += len(batch)

        if i < len(batches):
            time.sleep(1)

    _save_pushed_ids(pushed_ids | newly_pushed)
    print(f"\n  Pushed IDs saved → {PUSHED_IDS_PATH.relative_to(REPO_ROOT)}")

    return {"pushed": len(newly_pushed), **total}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push APPLY jobs to chennu.co")
    parser.add_argument("--dry-run", action="store_true", help="Preview payloads without pushing")
    parser.add_argument("--reset",   action="store_true", help="Ignore pushed history and re-push all APPLY jobs")
    args = parser.parse_args()

    print(f"\nPushing APPLY jobs to {CONSULTING_API_URL}...\n")
    result = push_apply_jobs(dry_run=args.dry_run, reset=args.reset)

    if not args.dry_run:
        print(f"\n  Total pushed: {result['pushed']}")
        print(f"  Created:      {result['created']}")
        print(f"  Skipped:      {result['skipped']}")
        print(f"  Errors:       {result['errors']}")
