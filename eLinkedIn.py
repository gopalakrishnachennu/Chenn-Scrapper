"""
LinkedIn Jobs search scraper (logged-in browser).

Uses the same pipeline as other scrapers: utils/startChrome, fileManagement merge/save,
and dataManager persistence (Mongo or SQLite per JOB_STORAGE_BACKEND).

Requires manual login in SCRAPING_CHROME_DIR before/during first run when prompted.

Only persists jobs with a usable external apply URL (skips LinkedIn Easy Apply-only postings).

Job title is required and never stored empty: rows without a non-empty plausible title are skipped.
Employment type is normalized to: Full-time, Contract, W2 (optional when not shown).
Work model is normalized to: Hybrid, Remote, Onsite (optional when not shown).
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import sqlite3
import time
from html import unescape as html_unescape
from pathlib import Path
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from dotenv import load_dotenv
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.dataManager import loadKnownJobIdsByPlatform
from utils.fileManagement import (
    inferPlatformFromPath,
    isAcceptableJobTitle,
    isCompleteJobRow,
    loadJobsDocumentOrEmpty,
    mergeNewJobsIntoDocument,
    resolveOutputJsonPath,
    resolveScraperSearchKeywords,
    saveOutputDocument,
    shouldSkipJob,
)
from utils.scraperTerminalLog import PLATFORM_LINKEDIN, ScraperRunLog
from utils.startChrome import (
    createScrapingChromeDriver,
    envBool,
    promptBeforeClosingBrowserIfHeaded,
)

load_dotenv()


def _safe_driver_get(driver: WebDriver, url: str) -> None:
    """
    LinkedIn often keeps network activity open so the window 'load' event never fires.
    With page_load_strategy='eager' (see startChrome) that is rare; this still recovers
    if driver.get hits page_load_timeout (Selenium TimeoutException or urllib3 read timeout).
    """
    try:
        driver.get(url)
    except Exception as exc:
        blob = f"{type(exc).__name__} {exc}".lower()
        if "timeout" not in blob and "timed out" not in blob:
            raise
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass


LINKEDIN_ORIGIN = "https://www.linkedin.com"
skippedOriginalUrlIdsKey = "skippedOriginalUrlIds"

LINKEDIN_SOURCE_PATH = resolveOutputJsonPath("linkedin.source")

_JOB_VIEW_ID_RE = re.compile(r"/jobs/view/(\d+)", re.I)
# City, ST / City, State — LinkedIn location lines (new UI uses opaque span classes).
_LOCATION_LINE_RE = re.compile(r"^[A-Za-z\s\-.'’()]+\s*,\s*[A-Za-z]{2,}(\s+[A-Za-z\s\-]+)?$")

_EMPLOYMENT_TYPE_LABELS = frozenset(
    {
        "full-time",
        "part-time",
        "contract",
        "temporary",
        "internship",
        "freelance",
        "fixed-term",
        "fixed term",
    }
)

# These are workplace chips, not role names — never accept as job title.
_WORK_MODEL_STANDALONE_LABELS = frozenset(
    {
        "remote",
        "hybrid",
        "on-site",
        "onsite",
        "on site",
        "in-office",
        "in office",
        "100% remote",
        "fully remote",
    }
)

_TITLE_UI_NOISE_LABELS = frozenset(
    {
        "show all",
        "show more",
        "show less",
        "view all",
        "see all",
        "learn more",
        "read more",
        "apply",
        "save",
    }
)

_LINKEDIN_TOP_CARD_ROOTS = (
    ".job-details-jobs-unified-top-card",
    ".jobs-unified-top-card",
    ".jobs-details-top-card",
)

_TITLE_FAILURES_PATH = Path(__file__).resolve().parent / "zata" / "linkedin_title_failures.json"


def _normalize_linkedin_employment_type(raw: str) -> str:
    """
    Canonical values: Full-time, Contract, W2. Empty if the chip does not map cleanly.
    """
    if not raw or not str(raw).strip():
        return ""
    tl = " ".join(str(raw).lower().split())
    if re.search(r"\bw-?2\b", tl) or "w 2" in tl:
        return "W2"
    if any(
        x in tl
        for x in (
            "contract",
            "contractor",
            "temporary",
            "freelance",
            "internship",
            "fixed-term",
            "fixed term",
        )
    ) or re.search(r"\bintern\b", tl):
        return "Contract"
    if "part-time" in tl or "part time" in tl:
        return ""
    if "full-time" in tl or "full time" in tl:
        return "Full-time"
    return ""


def _normalize_linkedin_work_model(raw: str) -> str:
    """Canonical values: Hybrid, Remote, Onsite."""
    if not raw or not str(raw).strip():
        return ""
    tl = " ".join(str(raw).lower().split())
    if "hybrid" in tl:
        return "Hybrid"
    if "remote" in tl:
        return "Remote"
    if any(
        x in tl
        for x in ("on-site", "onsite", "on site", "in-office", "in office")
    ):
        return "Onsite"
    return ""

# Title column must not be pay bands or employment chips mistaken for headings.
_SALARY_OR_PAY_BAND_RE = re.compile(
    r"(^\s*[\$£€]|[\$£€]\s*[\d,.]+|/\s*(?:yr|year|hr|hour)\b|"
    r"\d+[kKmM]\s*(?:/|\s*-\s*|\s+to\s+)\s*[\$£€]?\s*[\d,.]+[kKmM]?)",
    re.I,
)

# Comma-separated lines where the left side looks like a role (not "City, ST").
_JOB_TITLE_LEFT_HINT = re.compile(
    r"\b(engineer|engineering|developer|devops|sre|manager|management|analyst|"
    r"architect|scientist|designer|specialist|consultant|director|lead|administrator|"
    r"admin|coordinator|associate|executive|intern|technician|operator|representative|"
    r"scientist|researcher|writer|editor|recruiter|seller|sales)\b",
    re.I,
)


def _linkedin_jobs_location() -> str:
    raw = (os.getenv("LINKEDIN_JOBS_LOCATION") or "").strip()
    return raw if raw else "United States"


def buildLinkedInJobsSearchUrl(keyword: str) -> str:
    kw = keyword.strip()
    if not kw:
        raise ValueError("LinkedIn search keyword must be non-empty")
    q = {
        "keywords": kw,
        "location": _linkedin_jobs_location(),
        "origin": "JOB_SEARCH_PAGE_JOB_FILTER",
    }
    return f"{LINKEDIN_ORIGIN}/jobs/search/?{urlencode(q)}"


def _is_plausible_linkedin_job_title(t: str) -> bool:
    if not isAcceptableJobTitle(t):
        return False
    raw = " ".join((t or "").strip().split())
    tl = raw.lower()
    if tl in _EMPLOYMENT_TYPE_LABELS:
        return False
    if tl in _WORK_MODEL_STANDALONE_LABELS:
        return False
    if tl in _TITLE_UI_NOISE_LABELS:
        return False
    if _SALARY_OR_PAY_BAND_RE.search(raw):
        return False
    if tl.startswith("title:"):
        return False
    # Chip text like "Matches your job preferences, workplace type is Remote."
    if "job preferences" in tl and "workplace" in tl:
        return False
    if "matches your job preferences" in tl:
        return False
    return True


def _record_title_failure(
    *,
    job_id: str,
    job_url: str,
    phase_label: str,
    candidate: str,
) -> None:
    entry = {
        "jobId": job_id,
        "jobUrl": job_url,
        "phase": phase_label,
        "candidate": candidate,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _TITLE_FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if _TITLE_FAILURES_PATH.exists():
        try:
            rows = json.loads(_TITLE_FAILURES_PATH.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
    rows.append(entry)
    _TITLE_FAILURES_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _cleanup_linkedin_ui_noise_titles_sqlite(log: ScraperRunLog) -> None:
    """
    Defensive cleanup: remove previously inserted LinkedIn rows with UI-control text as title.
    Runs only when JOB_STORAGE_BACKEND=sqlite.
    """
    backend = (os.getenv("JOB_STORAGE_BACKEND") or "").strip().lower()
    if backend not in {"sqlite", "sql"}:
        return
    db_raw = (os.getenv("SQLITE_JOBS_PATH") or "").strip() or "zata/chennu_jobs.sqlite"
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = (Path(__file__).resolve().parent / db_path).resolve()
    if not db_path.exists():
        return

    noise_titles = sorted(_TITLE_UI_NOISE_LABELS)
    if not noise_titles:
        return
    ph = ",".join("?" for _ in noise_titles)
    sql = (
        "DELETE FROM job_data "
        "WHERE platform = ? AND lower(trim(title)) IN (" + ph + ")"
    )
    params = ["LinkedIn", *noise_titles]
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(sql, params)
            deleted = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
        if deleted > 0:
            log.info(f"Cleaned {deleted} stale LinkedIn row(s) with UI-noise titles.")
    except Exception as exc:
        log.warning(f"LinkedIn title cleanup skipped: {exc}")


def _looks_like_location_line_candidate(t: str) -> bool:
    t = (t or "").strip().replace("\n", " ")
    if not (2 <= len(t) <= 140):
        return False
    tl = t.lower()
    if "ago" in tl or "applicant" in tl or "clicked apply" in tl:
        return False
    if tl in _EMPLOYMENT_TYPE_LABELS:
        return False
    if _SALARY_OR_PAY_BAND_RE.search(t):
        return False
    if "metropolitan area" in tl:
        return True
    if tl in {"remote", "hybrid", "on-site", "onsite"}:
        return True
    if re.search(r",\s*(United States|USA)\s*$", t, re.I):
        left = t.rsplit(",", 1)[0].strip()
        if _JOB_TITLE_LEFT_HINT.search(left):
            return False
        return True
    if "," not in t:
        return False
    left, _, right = t.partition(",")
    left, right = left.strip(), right.strip()
    if _JOB_TITLE_LEFT_HINT.search(left):
        return False
    if _JOB_TITLE_LEFT_HINT.search(right):
        return False
    if len(right) > 48:
        return False
    return True


def _pick_best_location_candidate(candidates: list[str]) -> str:
    """Prefer City, ST-style lines over vague comma phrases."""
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        k = c.strip()
        if k and k not in seen:
            seen.add(k)
            deduped.append(k)
    if not deduped:
        return ""
    strict = [c for c in deduped if _LOCATION_LINE_RE.match(c)]
    if strict:
        return strict[0]
    us_region = [
        c for c in deduped if re.search(r",\s*(United States|USA)\s*$", c, re.I)
    ]
    if us_region:
        return us_region[0]
    metro = [c for c in deduped if "metropolitan area" in c.lower()]
    if metro:
        return metro[0]
    remote = [
        c
        for c in deduped
        if c.strip().lower() in {"remote", "hybrid", "on-site", "onsite"}
    ]
    if remote:
        return remote[0]
    return deduped[0]


def _location_from_page_source(page_source: str) -> str:
    """LinkedIn embeds formatted location in inlined JSON even when DOM is sparse."""
    if not page_source:
        return ""
    normalized = (
        page_source.replace("&amp;", "&")
        .replace("\\u002F", "/")
        .replace("\\u003A", ":")
        .replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\/", "/")
    )
    normalized = html_unescape(normalized)
    patterns = (
        r'"formattedLocation"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"formattedLocationName"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"geoLocationName"\s*:\s*"((?:[^"\\]|\\.)*)"',
    )
    for pattern in patterns:
        m = re.search(pattern, normalized)
        if not m:
            continue
        raw = m.group(1).replace(r"\"", '"').replace("\\n", " ")
        t = html_module.unescape(raw).strip()
        if not (2 <= len(t) <= 180):
            continue
        tl = t.lower()
        if "ago" in tl or "applicant" in tl:
            continue
        if _SALARY_OR_PAY_BAND_RE.search(t):
            continue
        if _looks_like_location_line_candidate(t):
            return t
        if _LOCATION_LINE_RE.match(t):
            return t
        if "metropolitan area" in tl:
            return t
        if re.search(r",\s*(United States|USA)\s*$", t, re.I):
            return t
        if len(t) <= 56 and "," not in t and not _JOB_TITLE_LEFT_HINT.search(t):
            return t
    return ""


def resolveLinkedInSearchPhases(cli_url: str | None) -> list[tuple[str, str]]:
    if cli_url and str(cli_url).strip():
        return [(str(cli_url).strip(), "cli")]
    keywords = resolveScraperSearchKeywords()
    if not keywords:
        return []
    return [(buildLinkedInJobsSearchUrl(kw), kw) for kw in keywords]


def ensureSkippedOriginalUrlIds(data: dict) -> None:
    bucket = data.get(skippedOriginalUrlIdsKey)
    if isinstance(bucket, list):
        return
    data[skippedOriginalUrlIdsKey] = []


def seedSeenIdsFromDocument(data: dict) -> set[str]:
    out: set[str] = set()
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        for j in jobs:
            if isinstance(j, dict):
                jid = j.get("jobId")
                if isinstance(jid, str) and jid:
                    out.add(jid)
    skip_ids = data.get(skippedOriginalUrlIdsKey)
    if isinstance(skip_ids, list):
        for sid in skip_ids:
            if isinstance(sid, str) and sid.strip():
                out.add(sid.strip())
    return out


def linkedinSeenIdsBeforeScrape(data: dict, output_path: Path) -> set[str]:
    platform = inferPlatformFromPath(output_path)
    return set(loadKnownJobIdsByPlatform(platform)) | seedSeenIdsFromDocument(data)


def _extract_redirect_target(linkedin_href: str | None) -> str | None:
    if not linkedin_href:
        return None
    try:
        parsed = urlparse(linkedin_href.strip())
        query = parse_qs(parsed.query)
        wrapped = query.get("url", [None])[0]
        return unquote(wrapped) if wrapped else linkedin_href.strip()
    except Exception:
        return linkedin_href.strip()


def _external_apply_from_page_source(page_source: str) -> str | None:
    if not page_source:
        return None
    normalized = (
        page_source.replace("&amp;", "&")
        .replace("\\u002F", "/")
        .replace("\\u003A", ":")
        .replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\/", "/")
    )
    normalized = html_unescape(normalized)

    keyed_patterns = [
        r'"offsiteApplyUrl"\s*:\s*"([^"]+)"',
        r'"offsiteApplyTrackingUrl"\s*:\s*"([^"]+)"',
        r'"companyApplyUrl"\s*:\s*"([^"]+)"',
        r'"externalApplyUrl"\s*:\s*"([^"]+)"',
        r'"applyRedirectUrl"\s*:\s*"([^"]+)"',
        r'"applyUrl"\s*:\s*"([^"]+)"',
        r'"jobPostingUrl"\s*:\s*"([^"]+)"',
        r'"trackingUrl"\s*:\s*"([^"]+)"',
    ]
    for pattern in keyed_patterns:
        matches = re.findall(pattern, normalized)
        if matches:
            cand = html_module.unescape(matches[0])
            return _extract_redirect_target(cand) or cand

    patterns = [
        r"https://www\.linkedin\.com/safety/go/\?url=[^\"'\\s<]+",
        r"https://www\.linkedin\.com/redir/redirect\?url=[^\"'\\s<]+",
        r"https://[^\"'\\s<]*greenhouse\.io[^\"'\\s<]*",
        r"https://[^\"'\\s<]*lever\.co[^\"'\\s<]*",
        r"https://[^\"'\\s<]*workdayjobs\.com[^\"'\\s<]*",
        r"https://[^\"'\\s<]*myworkdayjobs\.com[^\"'\\s<]*",
        r"https://[^\"'\\s<]*smartrecruiters\.com[^\"'\\s<]*",
        r"https://[^\"'\\s<]*icims\.com[^\"'\\s<]*",
        r"https://[^\"'\\s<]*taleo\.net[^\"'\\s<]*",
        r"https://[^\"'\\s<]*indeed\.com[^\"'\\s<]*",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, normalized)
        if matches:
            return _extract_redirect_target(matches[0])
    return None


def _scroll_height(driver: WebDriver, el) -> None:
    try:
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollHeight;",
            el,
        )
    except Exception:
        pass


def _find_scroll_container(driver: WebDriver):
    selectors = (
        "div.jobs-search-results-list",
        ".scaffold-layout__list-container",
        "ul.scaffold-layout__list",
    )
    for sel in selectors:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            return els[0]
    return None


def _scroll_linkedin_pagination_into_view(driver: WebDriver) -> None:
    """New jobs UI: page numbers + Next sit below the list; scroll so controls are interactable."""
    for sel in (
        '[data-testid="pagination-controls-next-button-visible"]',
        '[data-testid="pagination-controls-list"]',
    ):
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if not els:
            continue
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                els[-1],
            )
            time.sleep(0.3)
            return
        except Exception:
            continue


def _linkedin_search_url_supports_start_param(url: str) -> bool:
    """Logged-in search uses /jobs/search/ or /jobs/search-results/ with optional start= offset."""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return "/jobs/search" in path or "search-results" in path


def _linkedin_navigate_search_via_start_param(driver: WebDriver, start: int) -> bool:
    """
    Advance results via start= (typically 0, 25, 50, …). More reliable than clicking Next
    when the UI duplicates controls or intercepts clicks.
    """
    cur = (driver.current_url or "").strip()
    if not cur or not _linkedin_search_url_supports_start_param(cur):
        return False
    try:
        parsed = urlparse(cur)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["start"] = [str(max(0, int(start)))]
        new_query = urlencode(qs, doseq=True)
        new_url = parsed._replace(query=new_query).geturl()
    except Exception:
        return False
    if new_url == cur:
        return False
    try:
        _safe_driver_get(driver, new_url)
    except Exception:
        return False
    pause = max(0.5, float(os.getenv("LINKEDIN_PAGE_LOAD_PAUSE_SEC", "1.75")))
    time.sleep(pause)
    return True


def _job_ids_from_search_result_list(driver: WebDriver) -> set[str]:
    """
    Prefer job IDs from the left results rail. The detail pane keeps a /jobs/view/ link for
    the selected card; counting all links on the page can hide pagination changes.
    """
    list_selectors = (
        "li.jobs-search-results__list-item a[href*='/jobs/view/']",
        "div.jobs-search-results__list-item a[href*='/jobs/view/']",
        ".job-card-container a[href*='/jobs/view/']",
        "ul.scaffold-layout__list a[href*='/jobs/view/']",
        "div.scaffold-layout__list-container a[href*='/jobs/view/']",
        "div.jobs-search-results-list a[href*='/jobs/view/']",
        "[data-testid='job-card-list'] a[href*='/jobs/view/']",
    )
    from selenium.common.exceptions import StaleElementReferenceException
    ids: set[str] = set()
    for sel in list_selectors:
        for link in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                href = (link.get_attribute("href") or "").strip()
            except StaleElementReferenceException:
                continue
            m = _JOB_VIEW_ID_RE.search(href)
            if m:
                ids.add(m.group(1))
        if ids:
            return ids
    ids = set()
    for link in driver.find_elements(
        By.CSS_SELECTOR,
        'a[href*="/jobs/view/"], a[href*="/jobs/collect/"]',
    ):
        try:
            href = (link.get_attribute("href") or "").strip()
        except StaleElementReferenceException:
            continue
        m = _JOB_VIEW_ID_RE.search(href)
        if m:
            ids.add(m.group(1))
    return ids


def _collect_job_ids_from_dom(driver: WebDriver, seen: set[str], ordered: list[str]) -> None:
    for link in driver.find_elements(
        By.CSS_SELECTOR,
        'a[href*="/jobs/view/"], a[href*="/jobs/collect/"]',
    ):
        href = (link.get_attribute("href") or "").strip()
        m = _JOB_VIEW_ID_RE.search(href)
        if not m:
            continue
        jid = m.group(1)
        if jid not in seen:
            seen.add(jid)
            ordered.append(jid)


def _click_linkedin_search_next_page(driver: WebDriver) -> bool:
    """
    Next is a sibling of the page-number <ul>, not inside it — use stable test ids.
    See: data-testid="pagination-controls-next-button-visible"
    """
    next_selectors = (
        '[data-testid="pagination-controls-next-button-visible"]',
        "button.pagination-controls-next-button-visible",
        'button[data-testid^="pagination-controls-next-button"]',
        'nav[aria-label*="pagination"] button[aria-label*="Next"]',
        'button[aria-label="Next"]',
        'button[aria-label*="Next page"]',
    )
    for sel in next_selectors:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        for el in els:
            try:
                if not el.is_displayed():
                    continue
            except Exception:
                continue
            try:
                cls = (el.get_attribute("class") or "").lower()
                if "hidden" in cls and "visible" not in cls:
                    continue
            except Exception:
                pass
            try:
                if not el.is_enabled():
                    continue
            except Exception:
                continue
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                time.sleep(0.25)
            except Exception:
                pass
            try:
                el.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", el)
                except Exception:
                    continue
            time.sleep(0.35)
            return True
    return False


def _wait_fresh_job_ids_after_page_turn(
    driver: WebDriver,
    ids_before: set[str],
    *,
    timeout: float,
) -> bool:
    """After advancing pages, wait until the results rail shows a job id we have not seen yet."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fresh = _job_ids_from_search_result_list(driver)
        if fresh - ids_before:
            return True
        time.sleep(0.45)
    return False


def _harvest_ids_one_result_page(
    driver: WebDriver,
    log: ScraperRunLog,
    seen: set[str],
    ordered: list[str],
    *,
    max_rounds: int,
) -> None:
    """Scroll within the current search result page to lazy-load cards, then collect IDs."""
    stagnant = 0
    for _rnd in range(max_rounds):
        prev = len(seen)
        _collect_job_ids_from_dom(driver, seen, ordered)

        pane = _find_scroll_container(driver)
        if pane:
            _scroll_height(driver, pane)
        else:
            driver.execute_script("window.scrollBy(0, 900);")

        time.sleep(0.65)
        if len(seen) == prev:
            stagnant += 1
            if stagnant >= 8:
                break
        else:
            stagnant = 0


def collect_ordered_job_ids(driver: WebDriver, log: ScraperRunLog, *, max_rounds: int) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    max_pages = max(1, int(os.getenv("LINKEDIN_MAX_SEARCH_PAGES", "12")))
    page_turn_timeout = float(os.getenv("LINKEDIN_PAGINATION_WAIT_SEC", "28"))
    page_size = max(1, int(os.getenv("LINKEDIN_RESULTS_PAGE_SIZE", "25")))

    try:
        WebDriverWait(driver, 45).until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "li.jobs-search-results__list-item, .job-card-container, .scaffold-layout__list, "
                    'a[href*="/jobs/view/"]',
                )
            )
        )
    except TimeoutException:
        log.warning("Job list selector timeout — continuing with whatever is on page.")

    for page_idx in range(max_pages):
        _harvest_ids_one_result_page(driver, log, seen, ordered, max_rounds=max_rounds)

        if page_idx + 1 >= max_pages:
            break

        ids_before_turn = set(seen)
        next_start = (page_idx + 1) * page_size
        _scroll_linkedin_pagination_into_view(driver)

        navigated = False
        via = ""
        if _linkedin_navigate_search_via_start_param(driver, next_start):
            navigated = True
            via = f"start={next_start}"
        elif _click_linkedin_search_next_page(driver):
            navigated = True
            via = "next_button"

        if not navigated:
            log.info(
                f"LinkedIn job search: end of pagination after {page_idx + 1} result page(s) "
                "(no URL/start navigation and no Next control)."
            )
            break

        if _wait_fresh_job_ids_after_page_turn(
            driver, ids_before_turn, timeout=page_turn_timeout
        ):
            log.info(f"LinkedIn job search: opened result page {page_idx + 2} ({via}).")
            continue

        log.warning(
            "LinkedIn pagination: listing did not refresh — retrying alternate method once."
        )
        retry_ok = False
        if via.startswith("start="):
            retry_ok = _click_linkedin_search_next_page(driver)
        else:
            retry_ok = _linkedin_navigate_search_via_start_param(driver, next_start)

        if retry_ok and _wait_fresh_job_ids_after_page_turn(
            driver, ids_before_turn, timeout=page_turn_timeout
        ):
            log.info(f"LinkedIn job search: opened result page {page_idx + 2} (fallback).")
            continue

        log.warning(
            "LinkedIn job search: stopping pagination — no new job cards detected after advance."
        )
        break

    return ordered


def _detail_is_easy_apply(driver: WebDriver) -> bool:
    try:
        for btn in driver.find_elements(By.CSS_SELECTOR, "button.jobs-apply-button"):
            try:
                if not btn.is_displayed():
                    continue
            except Exception:
                continue
            blob = f"{btn.text or ''} {(btn.get_attribute('aria-label') or '')}".lower()
            if "easy apply" in blob:
                return True
    except Exception:
        pass
    return False


def _extract_job_description(driver: WebDriver) -> str:
    # New LinkedIn UI: stable test id on "About the job" expandable body (hashed CSS elsewhere).
    selectors = (
        '[data-testid="expandable-text-box"]',
        '[data-testid*="expandable-text"]',
        "#job-details",
        ".jobs-description-content__text",
        ".jobs-description__container",
        ".jobs-box__html-content",
        "article.jobs-description",
        "[class*='jobs-description-content']",
        ".jobs-details-module",
        ".jobs-search__job-details--container",
    )
    best = ""
    try:
        pane = driver.find_elements(
            By.CSS_SELECTOR,
            ".jobs-search__job-details--container, .jobs-details__main, "
            ".jobs-search__job-details",
        )
        if pane:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight;",
                pane[0],
            )
            time.sleep(0.35)
    except Exception:
        pass
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = (el.text or "").strip()
                if len(t) > len(best):
                    best = t
        except Exception:
            continue
    return best


def _linkedin_pill_text_is_salary(t: str) -> bool:
    raw = (t or "").strip()
    if not raw or len(raw) > 80:
        return False
    if _SALARY_OR_PAY_BAND_RE.search(raw):
        return True
    if "$" in raw and any(ch.isdigit() for ch in raw):
        return True
    rl = raw.lower()
    if "/yr" in rl or "/hr" in rl or "k/yr" in rl.replace(" ", ""):
        return True
    if re.search(r"\d+\s*[-–]\s*\d+\s*[kK]", raw):
        return True
    return False


def _linkedin_heading_title(driver: WebDriver, job_id: str) -> str:
    """
    Detail header: div.job-details-jobs-unified-top-card__job-title > h1 > a (often also .t-24 on div).
    Prefer job-scoped /jobs/view/{id}/ href, then fall back within top-card roots and document-wide.
    """
    narrow_selectors = (
        f"div.job-details-jobs-unified-top-card__job-title h1 a[href*='/jobs/view/{job_id}']",
        f"div.t-24.job-details-jobs-unified-top-card__job-title h1 a[href*='/jobs/view/{job_id}']",
        f"div.job-details-jobs-unified-top-card__job-title h1 a[href*='/jobs/view/']",
        "div.t-24.job-details-jobs-unified-top-card__job-title h1 a[href*='/jobs/view/']",
        f"h1.t-24 a[href*='/jobs/view/{job_id}']",
        f"h1.t-24.t-bold a[href*='/jobs/view/{job_id}']",
        f"h1 a[href*='/jobs/view/{job_id}']",
        "h1.t-24.t-bold.inline a[href*='/jobs/view/']",
        "h1.t-24 a.ember-view[href*='/jobs/view/']",
        "h1.t-24 a[href*='/jobs/view/']",
        ".job-details-jobs-unified-top-card__job-title h1 a",
        "h1 a.ember-view[href*='/jobs/view/']",
    )
    for root_sel in _LINKEDIN_TOP_CARD_ROOTS:
        try:
            roots = driver.find_elements(By.CSS_SELECTOR, root_sel)
        except Exception:
            continue
        for root in roots[:3]:
            for sel in narrow_selectors:
                try:
                    for el in root.find_elements(By.CSS_SELECTOR, sel):
                        t = (el.text or "").strip()
                        if _is_plausible_linkedin_job_title(t):
                            return t
                except Exception:
                    continue
    job_scoped = tuple(s for s in narrow_selectors if job_id in s)
    for sel in job_scoped:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = (el.text or "").strip()
                if _is_plausible_linkedin_job_title(t):
                    return t
        except Exception:
            continue
    for sel in narrow_selectors:
        if sel in job_scoped:
            continue
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = (el.text or "").strip()
                if _is_plausible_linkedin_job_title(t):
                    return t
        except Exception:
            continue
    return ""


def _linkedin_title_from_page_source(page_source: str) -> str:
    if not page_source:
        return ""
    normalized = (
        page_source.replace("&amp;", "&")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
    )
    normalized = html_unescape(normalized)

    m = re.search(
        r'<meta\s+property=["\']og:title["\']\s+content=["\']((?:[^"\'\\]|\\.)*)["\']',
        page_source,
        re.I,
    )
    if m:
        t = html_module.unescape(m.group(1)).strip()
        if " | " in t:
            t = t.split(" | ")[0].strip()
        elif "|" in t:
            t = t.split("|")[0].strip()
        tl = t.lower()
        if tl.startswith("linkedin") or "job alert" in tl:
            t = ""
        if t and _is_plausible_linkedin_job_title(t):
            return t

    m = re.search(r"<title>\s*([^<]+?)\s*</title>", page_source, re.I)
    if m:
        t = html_module.unescape(m.group(1)).strip()
        for sep in (" | ", " - ", "|", " – "):
            if sep in t:
                t = t.split(sep)[0].strip()
                break
        t = re.sub(r"\s+with verification\s*$", "", t, flags=re.I).strip()
        if t.lower().startswith("linkedin"):
            t = ""
        if t and _is_plausible_linkedin_job_title(t):
            return t

    for key in ("jobPostingTitle", "formattedJobTitle", "jobTitle"):
        pat = rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"'
        for m2 in re.finditer(pat, normalized):
            raw = m2.group(1).replace(r"\"", '"')
            t = html_module.unescape(raw).strip()
            if _is_plausible_linkedin_job_title(t):
                return t
    return ""


def _linkedin_parse_fit_level_preferences(driver: WebDriver) -> tuple[str, str, str]:
    """
    Buttons under div.job-details-fit-level-preferences: salary, workplace, employment type.
    Order varies by listing — classify by text, not index.
    Returns (employment_type, work_model, salary_or_empty).
    """
    employment_type = ""
    work_model = ""
    salaries: list[str] = []
    try:
        roots = driver.find_elements(By.CSS_SELECTOR, "div.job-details-fit-level-preferences")
        if not roots:
            return "", "", ""
        for btn in roots[0].find_elements(By.CSS_SELECTOR, "button"):
            try:
                if not btn.is_displayed():
                    continue
            except Exception:
                continue
            t = " ".join((btn.text or "").split()).strip()
            if not t or len(t) > 160:
                continue
            tl = t.lower()
            if "job preferences" in tl and "match" in tl:
                continue
            if _linkedin_pill_text_is_salary(t):
                salaries.append(t)
                continue
            if tl in _EMPLOYMENT_TYPE_LABELS:
                employment_type = t
                continue
            if "contract" in tl and "contractor" not in tl:
                employment_type = t
                continue
            if any(
                x in tl
                for x in (
                    "remote",
                    "hybrid",
                    "on-site",
                    "on site",
                    "onsite",
                    "in-office",
                    "in office",
                )
            ):
                work_model = t
                continue
    except Exception:
        pass
    salary_str = " · ".join(salaries) if salaries else ""
    return employment_type, work_model, salary_str


def _fallback_job_description(
    *,
    title: str,
    company: str,
    location: str,
    posted: str,
    external: str,
) -> str:
    parts = [
        f"Title: {title}".strip() if title else "",
        f"Company: {company}".strip() if company else "",
        f"Location: {location}".strip() if location else "",
        f"Posted: {posted}".strip() if posted else "",
        f"Apply (from LinkedIn): {external}".strip() if external else "",
        "",
        "[Summary scraped from LinkedIn — expand description selectors if you need full text.]",
    ]
    return "\n".join(p for p in parts if p).strip()


def _linkedin_detail_title(driver: WebDriver, job_id: str) -> str:
    """Fallback: longest plausible /jobs/view/{id}/ anchor on the page (not rail-first)."""
    sel = f'a[href*="/jobs/view/{job_id}"]'
    best = ""
    for el in driver.find_elements(By.CSS_SELECTOR, sel):
        try:
            if el.find_elements(By.XPATH, "./ancestor::div[contains(@class,'job-details-fit-level-preferences')]"):
                continue
        except Exception:
            pass
        t = (el.text or "").strip()
        if not _is_plausible_linkedin_job_title(t):
            continue
        if len(t) > len(best):
            best = t
    if len(best) >= 5:
        return best
    return ""


def _linkedin_detail_company(driver: WebDriver) -> str:
    for el in driver.find_elements(By.CSS_SELECTOR, 'a[href*="linkedin.com/company/"]'):
        href = (el.get_attribute("href") or "").lower()
        if "/school/" in href:
            continue
        t = (el.text or "").strip()
        if len(t) < 2:
            continue
        tl = t.lower()
        if tl in {"follow", "following", "learn more", "see jobs"}:
            continue
        return t
    return ""


def _linkedin_detail_location(driver: WebDriver) -> str:
    candidates: list[str] = []
    try:
        for el in driver.find_elements(
            By.CSS_SELECTOR,
            '[data-testid*="location"], [data-testid*="geo"]',
        ):
            t = (el.text or "").strip().replace("\n", " ")
            if _looks_like_location_line_candidate(t):
                candidates.append(t)
    except Exception:
        pass

    roots = driver.find_elements(
        By.CSS_SELECTOR,
        ".jobs-search__job-details--container, "
        ".jobs-details-top-card, "
        ".jobs-unified-top-card, "
        "[class*='jobs-details'], main",
    )
    if not roots:
        roots = [driver.find_element(By.TAG_NAME, "body")]
    for root in roots[:8]:
        try:
            for sel in ("span", "div", "li"):
                for el in root.find_elements(By.CSS_SELECTOR, sel):
                    t = (el.text or "").strip().replace("\n", " ")
                    if _looks_like_location_line_candidate(t):
                        candidates.append(t)
        except Exception:
            continue
    return _pick_best_location_candidate(candidates)


def _top_card_text(driver: WebDriver, selectors: tuple[str, ...]) -> str:
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                t = (el.text or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def _first_top_card_text(
    driver: WebDriver,
    selectors: tuple[str, ...],
    *,
    accept: Callable[[str], bool],
) -> str:
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = (el.text or "").strip()
                if accept(t):
                    return t
        except Exception:
            continue
    return ""


def _unwrap_external_candidate(raw_href: str) -> str | None:
    href = (raw_href or "").strip()
    if not href:
        return None
    if "/safety/go/" in href or "/redir/" in href or "/redirect" in href:
        unwrapped = _extract_redirect_target(href)
        href = unwrapped or href
    if not href.startswith("http"):
        return None
    host = (urlparse(href).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "linkedin.com" in host:
        return None
    return href


def scrape_detail_job_view(driver: WebDriver, job_id: str) -> dict[str, str | None] | None:
    url = f"{LINKEDIN_ORIGIN}/jobs/view/{job_id}/"
    _safe_driver_get(driver, url)

    try:
        detail_ready_sel = (
            "div.job-details-jobs-unified-top-card__job-title, "
            "div.t-24.job-details-jobs-unified-top-card__job-title, "
            '[data-testid="expandable-text-box"], '
            f'a[href*="/jobs/view/{job_id}"], '
            ".jobs-unified-top-card__job-title, "
            "h1.t-24, .jobs-details-top-card__job-title, "
            "#job-details, "
            ".jobs-search__job-details--container"
        )
        WebDriverWait(driver, 35).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, detail_ready_sel))
        )
    except TimeoutException:
        return None

    time.sleep(0.5)

    try:
        panes = driver.find_elements(
            By.CSS_SELECTOR,
            ".jobs-search__job-details--container, .jobs-details__main",
        )
        if panes:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                panes[0],
            )
            time.sleep(0.35)
    except Exception:
        pass

    if _detail_is_easy_apply(driver):
        return {"_easyApplyOnly": "1"}

    external: str | None = None

    for anchor in driver.find_elements(
        By.CSS_SELECTOR,
        'a[aria-label="Apply on company website"], '
        'a[aria-label*="Apply on company website"]',
    ):
        href = (anchor.get_attribute("href") or "").strip()
        cand = _unwrap_external_candidate(href)
        if cand:
            external = cand
            break

    for css in (
        "a[href*='linkedin.com/safety/go']",
        "a[href*='linkedin.com/redir']",
        "a[href*='linkedin.com/redirect']",
        "a[href*='http']",
    ):
        for anchor in driver.find_elements(By.CSS_SELECTOR, css):
            href = (anchor.get_attribute("href") or "").strip()
            cand = _unwrap_external_candidate(href)
            if cand:
                external = cand
                break
        if external:
            break

    if not external:
        external = _external_apply_from_page_source(driver.page_source)
        if external:
            external = _extract_redirect_target(external) or external
            if "linkedin.com" in (urlparse(external).hostname or "").lower():
                external = None

    if not external or not str(external).startswith("http"):
        return None

    host = (urlparse(external).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "linkedin.com" in host:
        return None

    probe = {
        "jobId": job_id,
        "jobUrl": url,
        "originalJobPostUrl": external,
        "platform": "LinkedIn",
    }
    if shouldSkipJob(probe):
        return None

    def _extract_valid_title() -> str:
        out = _linkedin_heading_title(driver, job_id)
        if not out:
            out = _linkedin_detail_title(driver, job_id)
        if not out:
            out = _first_top_card_text(
                driver,
                (
                    "div.job-details-jobs-unified-top-card__job-title h1 a",
                    "div.t-24.job-details-jobs-unified-top-card__job-title h1 a",
                    ".job-details-jobs-unified-top-card__job-title h1 a",
                    ".job-details-jobs-unified-top-card__job-title h1",
                    ".job-details-jobs-unified-top-card__job-title",
                    ".jobs-unified-top-card__job-title",
                    ".jobs-details-top-card__job-title",
                    "h1.t-24 a",
                    "h1.t-24",
                ),
                accept=_is_plausible_linkedin_job_title,
            )
        out = (out or "").strip()
        if not _is_plausible_linkedin_job_title(out):
            alt = (_linkedin_title_from_page_source(driver.page_source) or "").strip()
            if _is_plausible_linkedin_job_title(alt):
                out = alt
        return out if _is_plausible_linkedin_job_title(out) else ""

    title = _extract_valid_title()
    if not title:
        # One recovery attempt for SPA hydration races before dropping the row.
        _safe_driver_get(driver, url)
        time.sleep(0.6)
        title = _extract_valid_title()
    if not title:
        return {
            "_titleFailed": "1",
            "jobId": job_id,
            "jobUrl": url,
            "_titleCandidate": (_linkedin_title_from_page_source(driver.page_source) or "").strip() or None,
        }
    company = _linkedin_detail_company(driver)
    if not company:
        company = _top_card_text(
            driver,
            (
                ".jobs-unified-top-card__company-name a",
                ".jobs-unified-top-card__company-name",
                ".jobs-details-top-card__company-url",
                ".job-details-jobs-unified-top-card__company-name a",
                ".job-details-jobs-unified-top-card__company-name",
            ),
        )

    location = ""
    try:
        subs = driver.find_elements(
            By.CSS_SELECTOR,
            ".jobs-unified-top-card__primary-description-container, "
            ".jobs-unified-top-card__bullet, "
            ".jobs-details-top-card__primary-description, "
            ".job-details-jobs-unified-top-card__primary-description-container",
        )
        for el in subs:
            raw = (el.text or "").strip()
            if not raw:
                continue
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                tl = line.lower()
                if "ago" in tl or "applicant" in tl:
                    continue
                if tl in _EMPLOYMENT_TYPE_LABELS:
                    continue
                if _SALARY_OR_PAY_BAND_RE.search(line):
                    continue
                if _looks_like_location_line_candidate(line):
                    location = line
                    break
            if location:
                break
    except Exception:
        pass

    if not location:
        location = _linkedin_detail_location(driver)

    if not location:
        location = _location_from_page_source(driver.page_source)

    location = (location or "").strip() or _linkedin_jobs_location()

    posted = ""
    try:
        for el in driver.find_elements(
            By.CSS_SELECTOR,
            ".jobs-unified-top-card__posted-date, "
            ".jobs-details-top-card__primary-description",
            ".job-details-jobs-unified-top-card__posted-date",
        ):
            t = (el.text or "").strip()
            if "ago" in t.lower() or re.search(r"\d+\s+(day|week|month|hour)", t.lower()):
                posted = t.split("\n")[0].strip()
                break
    except Exception:
        pass

    employment_type, work_model, salary_hint = _linkedin_parse_fit_level_preferences(driver)
    if not employment_type and not work_model:
        try:
            for pill in driver.find_elements(
                By.CSS_SELECTOR,
                ".jobs-unified-top-card__job-insight-text-button, "
                ".job-details-jobs-unified-top-card__job-insight-text-button",
            ):
                t = (pill.text or "").strip()
                if not t:
                    continue
                tl = t.lower()
                if tl in _EMPLOYMENT_TYPE_LABELS:
                    employment_type = t
                elif any(x in tl for x in ("remote", "hybrid", "on-site", "onsite", "on site")):
                    work_model = t
                elif _linkedin_pill_text_is_salary(t) and not salary_hint:
                    salary_hint = t
        except Exception:
            pass

    employment_type = _normalize_linkedin_employment_type(employment_type)
    work_model = _normalize_linkedin_work_model(work_model)

    description = _extract_job_description(driver)
    if len(description) < 80:
        fb = _fallback_job_description(
            title=title,
            company=company,
            location=location,
            posted=posted,
            external=external,
        )
        description = fb if len(fb) >= len(description) else description

    return {
        "jobId": job_id,
        "jobUrl": url,
        "title": title,
        "companyName": company or None,
        "location": location,
        "employmentType": employment_type or None,
        "workModel": work_model or None,
        "seniority": None,
        "experience": salary_hint or None,
        "postedAgo": posted or None,
        "originalJobPostUrl": external,
        "jobDescription": description or None,
        "platform": "LinkedIn",
    }


def scrape_linkedin_phase(
    driver: WebDriver,
    log: ScraperRunLog,
    start_url: str,
    output_path: Path,
    *,
    phase_label: str,
) -> tuple[int, int, int]:
    log.bindPhase(phase_label)
    _safe_driver_get(driver, start_url)
    time.sleep(2.0)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except TimeoutException:
        pass

    max_rounds = int(os.getenv("LINKEDIN_SCROLL_MAX_ROUNDS", "45"))
    max_detail = int(os.getenv("LINKEDIN_MAX_DETAIL_JOBS", "120"))
    max_search_pages = max(1, int(os.getenv("LINKEDIN_MAX_SEARCH_PAGES", "12")))

    data = loadJobsDocumentOrEmpty(output_path)
    ensureSkippedOriginalUrlIds(data)
    seen = linkedinSeenIdsBeforeScrape(data, output_path)
    if seen:
        log.existingJobsNotice(len(seen), output_path.name)

    ordered_ids = collect_ordered_job_ids(driver, log, max_rounds=max(10, max_rounds))
    log.info(
        f"Collected {len(ordered_ids)} job id(s) from search results "
        f"(scroll per page + pagination, cap {max_search_pages} page(s))."
    )

    added_rows = 0
    skipped_known = 0
    skipped_merge = 0
    skipped_easy = 0
    skipped_no_external = 0

    for idx, job_id in enumerate(ordered_ids[: max(1, max_detail)], start=1):
        if job_id in seen:
            skipped_known += 1
            continue

        row = scrape_detail_job_view(driver, job_id)
        if isinstance(row, dict) and row.get("_titleFailed"):
            skipped_no_external += 1
            seen.add(job_id)
            _record_title_failure(
                job_id=job_id,
                job_url=str(row.get("jobUrl") or f"{LINKEDIN_ORIGIN}/jobs/view/{job_id}/"),
                phase_label=phase_label,
                candidate=str(row.get("_titleCandidate") or ""),
            )
            log.jobSkip(idx, len(ordered_ids), "title unresolved", job_id)
            continue
        if row is None:
            skipped_no_external += 1
            seen.add(job_id)
            log.jobSkip(idx, len(ordered_ids), "no external apply URL", job_id)
            continue

        if row.get("_easyApplyOnly"):
            skipped_easy += 1
            bucket = data.setdefault(skippedOriginalUrlIdsKey, [])
            if isinstance(bucket, list) and job_id not in bucket:
                bucket.append(job_id)
                saveOutputDocument(output_path, data)
            seen.add(job_id)
            log.jobSkip(idx, len(ordered_ids), "Easy Apply only", job_id)
            continue

        if not isCompleteJobRow(row):
            skipped_no_external += 1
            seen.add(job_id)
            log.jobSkip(idx, len(ordered_ids), "incomplete row", job_id)
            continue

        added, skipped_one = mergeNewJobsIntoDocument(data, [row])
        if added:
            saveOutputDocument(output_path, data)
            added_rows += added
            seen.add(job_id)
            label = (row.get("companyName") or row.get("title") or "?")[:50]
            log.jobLine(idx, len(ordered_ids), f"{label} — view/{job_id}")
        else:
            skipped_merge += skipped_one
            seen.add(job_id)

    return added_rows, skipped_merge, skipped_known + skipped_easy + skipped_no_external


def main() -> int:
    run_log = ScraperRunLog(PLATFORM_LINKEDIN)
    parser = argparse.ArgumentParser(
        description="Scrape LinkedIn Jobs search into DB-backed storage (external apply only)."
    )
    parser.add_argument(
        "searchUrl",
        nargs="?",
        default=None,
        help="Optional full LinkedIn jobs search URL (single phase). Else SCRAPER_SEARCH_KEYWORDS.",
    )
    args = parser.parse_args()

    phases = resolveLinkedInSearchPhases(args.searchUrl)
    if not phases:
        run_log.error("No LinkedIn search keywords or URL configured.")
        return 1

    output_path = LINKEDIN_SOURCE_PATH
    headless = envBool("SCRAPING_HEADLESS", default=True)
    os.environ.setdefault("USE_UNDETECTED_CHROME", "1")

    try:
        driver = createScrapingChromeDriver(headless=headless, quiet=True)
    except ValueError as exc:
        run_log.error(str(exc))
        return 1

    try:
        driver.set_page_load_timeout(120)
        _cleanup_linkedin_ui_noise_titles_sqlite(run_log)
        total_added = 0
        total_skip_merge = 0
        total_skip_misc = 0

        for phase_num, (start_url, phase_label) in enumerate(phases, start=1):
            run_log.bindPhase(phase_label)
            run_log.phaseStart(
                phase_num,
                len(phases),
                phase_label,
                "scroll ids → visit job view → external apply only",
            )
            added, skipped_merge, skipped_misc = scrape_linkedin_phase(
                driver,
                run_log,
                start_url,
                output_path,
                phase_label=phase_label,
            )
            total_added += added
            total_skip_merge += skipped_merge
            total_skip_misc += skipped_misc
            run_log.phaseDone(
                phase_label,
                f"+{added} row(s); merge-skips {skipped_merge}; other skips {skipped_misc}",
            )

        run_log.runDone(
            f"+{total_added} new row(s); merge-skips {total_skip_merge}; skips {total_skip_misc} "
            f"across {len(phases)} phase(s) → {output_path.resolve()}",
        )
        promptBeforeClosingBrowserIfHeaded()
    finally:
        driver.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
