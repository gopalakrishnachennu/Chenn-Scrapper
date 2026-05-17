from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import json
import os
import zlib
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.dataManager import loadKnownJobIdsByPlatform
from utils.scraperTerminalLog import PLATFORM_ZIPRECRUITER, ScraperRunLog
from utils.fileManagement import (
    DEFAULT_SCRAPER_SEARCH_KEYWORDS,
    inferPlatformFromPath,
    loadJobsDocumentOrEmpty,
    mergeNewJobsIntoDocument,
    resolveOutputJsonPath,
    resolveScraperSearchKeywords,
    saveOutputDocument,
)
from utils.startChrome import (
    createScrapingChromeDriver,
    envBool,
    promptBeforeClosingBrowserIfHeaded,
)

load_dotenv()

baseUrl = "https://www.ziprecruiter.com/jobs-search"


def getDefaultZipRecruiterParams() -> dict:
    """Query parameters when no full URL override (used to build search URLs)."""
    primary = DEFAULT_SCRAPER_SEARCH_KEYWORDS[0]
    return {
        "search": primary,
        "location": "United States",
        "radius": 5000,
        "days": 1,
        "refine_by_employment": "employment_type:full_time",
        "refine_by_location_type": "",
        "refine_by_salary": "",
        "refine_by_salary_ceil": "",
        "refine_by_apply_type": "",
        "refine_by_experience_level": "",
        "employment_types_explicitly_set": "true",
    }

easyApplyText = "Easy Apply"
oneClickApplyLabel = "1-click apply"

skippedOriginalUrlIdsKey = "skippedOriginalUrlIds"
ZIPRECRUITER_SOURCE_PATH = resolveOutputJsonPath("ziprecruiter.source")

# List-card badges: skip opening detail / scraping (Zip-hosted apply flows).
_zipCardHostedApplyPhrases: tuple[tuple[str, str], ...] = (
    ("1-click apply", oneClickApplyLabel),
    ("1 click apply", oneClickApplyLabel),
    ("one-click apply", oneClickApplyLabel),
    ("one click apply", oneClickApplyLabel),
    ("quick apply", "Quick apply"),
    ("easy apply", "Easy Apply"),
)


def cardShowsZipHostedApply(card) -> str | None:
    """
    True when the job list card shows Zip's in-flow apply (Quick / Easy / 1-click).
    Matching is substring-based on normalized card text (list UI only).
    """
    raw = (getattr(card, "text", None) or "").casefold()
    collapsed = " ".join(raw.split())
    for needle, label in _zipCardHostedApplyPhrases:
        if needle.casefold() in collapsed:
            return label
    return None


def ensureSkippedOriginalUrlIds(data: dict) -> None:
    bucket = data.get(skippedOriginalUrlIdsKey)
    if isinstance(bucket, list):
        return
    data[skippedOriginalUrlIdsKey] = []


def buildDefaultZipRecruiterUrl() -> str:
    return f"{baseUrl}?{urlencode(getDefaultZipRecruiterParams())}"


def buildZipRecruiterUrlForKeyword(keyword: str) -> str:
    kw = keyword.strip()
    if not kw:
        raise ValueError("ZipRecruiter search keyword must be non-empty")
    query = {**getDefaultZipRecruiterParams(), "search": kw}
    return f"{baseUrl}?{urlencode(query)}"


def resolveZipRecruiterSearchUrl(cliUrl: str | None = None) -> str:
    if cliUrl and str(cliUrl).strip():
        return str(cliUrl).strip()
    return buildDefaultZipRecruiterUrl()


def resolveZipRecruiterSearchPhases(cliUrl: str | None = None) -> list[tuple[str, str]]:
    """
    Each phase is (searchUrl, label). One phase paginates the full result set
    before the next. Optional cliUrl forces a single phase.
    Keyword list: SCRAPER_SEARCH_KEYWORDS (see utils.fileManagement).
    """
    if cliUrl and str(cliUrl).strip():
        return [(str(cliUrl).strip(), "cli")]
    keywords = resolveScraperSearchKeywords()
    if not keywords:
        return []
    return [(buildZipRecruiterUrlForKeyword(kw), kw) for kw in keywords]


def _b64DecodeMatchTokenSegment(segment: str) -> bytes | None:
    pad = "=" * (-len(segment) % 4)
    blob = segment + pad
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(blob)
        except binascii.Error:
            continue
    return None


def _jsonDictFromBytes(blob: bytes) -> dict | None:
    if not blob:
        return None
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            parsed = json.loads(blob.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        return parsed if isinstance(parsed, dict) else None
    if len(blob) >= 2 and blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16-le" if blob[:2] == b"\xff\xfe" else "utf-16-be"
        try:
            parsed = json.loads(blob.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _externalUrlFromTokenDict(payload: dict) -> str | None:
    return payload.get("ExternalApplyUrl") or payload.get("SeoJobPageUrl")


def _parseMatchTokenPayload(blob: bytes) -> str | None:
    d = _jsonDictFromBytes(blob)
    if d is not None:
        return _externalUrlFromTokenDict(d)
    if blob.startswith(b"\x1f\x8b"):
        try:
            d = _jsonDictFromBytes(gzip.decompress(blob))
        except (OSError, EOFError):
            d = None
        if d is not None:
            return _externalUrlFromTokenDict(d)
    for wbits in (zlib.MAX_WBITS | 16, zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            d = _jsonDictFromBytes(zlib.decompress(blob, wbits))
        except zlib.error:
            continue
        if d is not None:
            return _externalUrlFromTokenDict(d)
    return None


def extractTargetUrlFromMatchToken(url: str) -> str | None:
    parsedUrl = urlparse(url)
    queryParams = parse_qs(parsedUrl.query)
    rawToken = queryParams.get("match_token", [None])[0]
    if not rawToken:
        return None

    decodedToken = unquote(rawToken)
    tokenBytes = _b64DecodeMatchTokenSegment(decodedToken)
    if tokenBytes is None:
        return None

    resolved = _parseMatchTokenPayload(tokenBytes)
    if resolved:
        return resolved

    try:
        inner = tokenBytes.decode("ascii").strip()
    except UnicodeDecodeError:
        inner = ""
    if inner:
        innerBytes = _b64DecodeMatchTokenSegment(inner)
        if innerBytes is not None:
            return _parseMatchTokenPayload(innerBytes)

    return None


def resolveOriginalApplyUrl(originalValue: str | None) -> str | None:
    if not originalValue:
        return None
    value = originalValue.strip()
    if not value:
        return None
    if value == easyApplyText:
        return value
    if "ziprecruiter.com/job-redirect" in value and "match_token=" in value:
        decoded = extractTargetUrlFromMatchToken(value)
        if decoded:
            return decoded
    return value


def dismissLoginPopupIfPresent(driver: webdriver.Chrome, timeoutSeconds: int = 5) -> bool:
    """Dismiss first-visit overlay; call only after initial navigation, not on each page."""
    backdropSelectors = [
        'div.flex.items-center.justify-center.bg-black.bg-opacity-50.transition-opacity.z-max.fixed.inset-0[role="presentation"][tabindex="-1"]',
        'div.fixed.inset-0.bg-black.bg-opacity-50[role="presentation"][tabindex="-1"]',
        'div.fixed.inset-0[role="presentation"][tabindex="-1"]',
    ]
    for selector in backdropSelectors:
        try:
            backdrop = WebDriverWait(driver, timeoutSeconds).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            if not backdrop.is_displayed():
                continue
            try:
                backdrop.click()
            except Exception:
                pass
            driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));",
                backdrop,
            )
            ActionChains(driver).move_to_element_with_offset(backdrop, 5, 5).click().perform()
            driver.execute_script(
                """
                const overlay = arguments[0];
                const dialog = document.querySelector('[role="dialog"][aria-modal="true"]');
                if (!overlay || !dialog) return;
                const r = dialog.getBoundingClientRect();
                const x = Math.max(8, r.left - 16);
                const y = Math.max(8, r.top - 16);
                const target = document.elementFromPoint(x, y) || overlay;
                ['mousedown', 'mouseup', 'click'].forEach((ev) => {
                  target.dispatchEvent(new MouseEvent(ev, {
                    bubbles: true, cancelable: true, clientX: x, clientY: y, view: window
                  }));
                });
                """,
                backdrop,
            )
            time.sleep(0.35)
            return True
        except TimeoutException:
            continue
        except Exception:
            continue
    return False


def safeText(root, selector: str) -> str | None:
    elements = root.find_elements(By.CSS_SELECTOR, selector)
    if not elements:
        return None
    value = elements[0].text.strip()
    return value or None


def safeAttr(root, selector: str, attr: str) -> str | None:
    elements = root.find_elements(By.CSS_SELECTOR, selector)
    if not elements:
        return None
    value = (elements[0].get_attribute(attr) or "").strip()
    return value or None


def resolveApplyValue(driver: webdriver.Chrome) -> str | None:
    externalHref = safeAttr(
        driver,
        "div[data-testid='job-details-scroll-container'] a[aria-label='Apply'][href]",
        "href",
    )
    if externalHref:
        return externalHref

    easyApplyButtonText = safeText(
        driver,
        "div[data-testid='job-details-scroll-container'] button[aria-label='Quick Apply']",
    )
    if easyApplyButtonText:
        return easyApplyText

    easyApplyButtonText = safeText(
        driver,
        "div[data-testid='job-details-scroll-container'] button[aria-label='Easy Apply']",
    )
    if easyApplyButtonText:
        return easyApplyText

    return None


def firstTextWithAny(texts: list[str], needles: tuple[str, ...]) -> str | None:
    for value in texts:
        if any(needle in value for needle in needles):
            return value
    return None


def getCardElementsOnPage(driver: webdriver.Chrome) -> list:
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "section.job_results_two_pane"))
    )
    section = driver.find_element(By.CSS_SELECTOR, "section.job_results_two_pane")
    allCards = section.find_elements(By.CSS_SELECTOR, 'article[id^="job-card-"]')
    uniqueById: dict[str, object] = {}
    for card in allCards:
        cardId = (card.get_attribute("id") or "").strip()
        if cardId and cardId not in uniqueById:
            uniqueById[cardId] = card
    return list(uniqueById.values())


def clickCardById(driver: webdriver.Chrome, cardId: str) -> bool:
    card = driver.find_element(By.ID, cardId)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
    time.sleep(0.2)
    clickTargets = card.find_elements(By.CSS_SELECTOR, "button[aria-label^='View '], h2[aria-label]")
    if clickTargets:
        try:
            clickTargets[0].click()
        except Exception:
            driver.execute_script("arguments[0].click();", clickTargets[0])
    else:
        driver.execute_script("arguments[0].click();", card)
    return True


def _normalizeTitleMatch(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def _detailPaneTitleMatches(expected: str | None, actual: str | None) -> bool:
    """List card title (often aria-label) can differ from detail h2; allow substring / prefix overlap."""
    e = _normalizeTitleMatch(expected)
    a = _normalizeTitleMatch(actual)
    if not e:
        return bool(a)
    if not a:
        return False
    if e in a or a in e:
        return True
    for prefix in ("view ", "view job: ", "job: "):
        if e.startswith(prefix):
            e2 = e[len(prefix) :].strip()
            if e2 and (e2 in a or a in e2):
                return True
    n = min(len(e), len(a), 28)
    if n >= 12 and e[:n] == a[:n]:
        return True
    if len(e) >= 14 and e[:22] in a:
        return True
    if len(a) >= 14 and a[:22] in e:
        return True
    return False


def waitForDetailsPane(
    driver: webdriver.Chrome,
    expectedTitle: str | None,
    log: ScraperRunLog,
) -> None:
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div[data-testid='right-pane'], div[data-testid='job-details-scroll-container']")
        )
    )
    if not expectedTitle:
        time.sleep(0.5)
        return

    def titleReady(d: webdriver.Chrome) -> bool:
        h2 = safeText(d, "div[data-testid='job-details-scroll-container'] h2")
        return _detailPaneTitleMatches(expectedTitle, h2)

    try:
        WebDriverWait(driver, 18).until(titleReady)
    except TimeoutException:
        h2 = safeText(driver, "div[data-testid='job-details-scroll-container'] h2")
        if h2:
            log.warning(
                f"[site] Detail title mismatch after wait "
                f"(list={expectedTitle!r}, detail={h2!r}); continuing.",
            )
            return
        raise


def scrapeSelectedJobDetails(driver: webdriver.Chrome, fallback: dict[str, str | None]) -> dict[str, str | None]:
    facts = driver.find_elements(By.CSS_SELECTOR, "div[data-testid='job-details-scroll-container'] p.text-primary")
    factTexts = [fact.text.strip() for fact in facts if fact.text.strip()]

    location = fallback.get("location") or firstTextWithAny(factTexts, (",", "Remote", "Hybrid", "On-site", "Onsite"))
    employmentType = fallback.get("employmentType") or firstTextWithAny(
        factTexts, ("Full-time", "Part-time", "Contract", "Temporary")
    )
    salaryRange = fallback.get("salaryRange") or firstTextWithAny(factTexts, ("$", "/hr", "/yr", "K/yr"))
    workModel = fallback.get("workModel")
    if not workModel:
        if firstTextWithAny(factTexts, ("Remote",)):
            workModel = "Remote"
        elif firstTextWithAny(factTexts, ("Hybrid",)):
            workModel = "Hybrid"
        elif firstTextWithAny(factTexts, ("On-site", "Onsite")):
            workModel = "Onsite"

    title = safeText(driver, "div[data-testid='job-details-scroll-container'] h2")
    companyName = safeText(driver, "div[data-testid='job-details-scroll-container'] a[href*='/co/']")
    applyValue = resolveApplyValue(driver)
    postedAgo = firstTextWithAny(factTexts, ("Posted", "ago"))

    description = safeText(driver, "div[data-testid='job-details-scroll-container'] div.whitespace-pre-line") or ""
    qualificationTags = ", ".join(
        sorted(
            {
                tag.strip()
                for tag in [
                    "Kubernetes" if "Kubernetes" in description else "",
                    "AWS" if "AWS" in description else "",
                    "Python" if "Python" in description else "",
                    "CI/CD" if "CI/CD" in description else "",
                    "Terraform" if "Terraform" in description else "",
                ]
                if tag.strip()
            }
        )
    )

    return {
        "title": title or fallback.get("title"),
        "companyName": companyName or fallback.get("companyName"),
        "location": location,
        "employmentType": employmentType,
        "salaryRange": salaryRange,
        "workModel": workModel,
        "originalJobPostUrl": applyValue,
        "jobResponsibility": description,
        "qualificationTags": qualificationTags,
        "postedAgo": postedAgo,
    }


def scrapeCurrentPageJobs(
    driver: webdriver.Chrome,
    log: ScraperRunLog,
    globalSeenIds: set[str],
    pageNumber: int,
    data: dict,
    outputPath: Path,
    *,
    phaseLabel: str = "",
) -> tuple[int, int, int]:
    log.bindPhase(phaseLabel)
    addedCount = 0
    skippedMerge = 0
    skippedKnown = 0
    cards = getCardElementsOnPage(driver)
    n = len(cards)
    log.info(
        f"Page {pageNumber}: found {n} list items; "
        "skipping jobIds already in output, opening detail for the rest…",
    )

    for idx, card in enumerate(cards):
        cardId = (card.get_attribute("id") or "").strip()
        companyPreview = safeText(card, '[data-testid="job-card-company"]') or "?"
        if not cardId:
            log.jobLine(idx + 1, n, "skip: no id")
            continue
        if cardId in globalSeenIds:
            skippedKnown += 1
            log.jobSkip(
                idx + 1,
                n,
                "on disk",
                f"{companyPreview} — {cardId}",
            )
            continue

        hostedLabel = cardShowsZipHostedApply(card)
        if hostedLabel:
            skipBucket = data.setdefault(skippedOriginalUrlIdsKey, [])
            if not isinstance(skipBucket, list):
                data[skippedOriginalUrlIdsKey] = []
                skipBucket = data[skippedOriginalUrlIdsKey]
            if cardId not in skipBucket:
                skipBucket.append(cardId)
                saveOutputDocument(outputPath, data)
            globalSeenIds.add(cardId)
            log.jobSkip(
                idx + 1,
                n,
                hostedLabel,
                f"{companyPreview} — {cardId}",
            )
            continue

        fallback = {
            "title": safeAttr(card, "h2[aria-label]", "aria-label"),
            "companyName": safeText(card, '[data-testid="job-card-company"]'),
            "location": safeText(card, '[data-testid="job-card-location"]'),
            "salaryRange": firstTextWithAny(
                [
                    element.text.strip()
                    for element in card.find_elements(By.CSS_SELECTOR, "p.text-body-md")
                    if element.text.strip()
                ],
                ("$", "/hr", "/yr", "K/yr"),
            ),
            "employmentType": "Full-time",
            "workModel": None,
        }

        pane_loaded = False
        for attempt in range(2):
            try:
                clickCardById(driver, cardId)
                waitForDetailsPane(driver, fallback.get("title"), log)
                pane_loaded = True
                break
            except TimeoutException:
                if attempt == 0:
                    log.warning(
                        f"[{idx + 1}/{n}] detail pane wait failed, retry click: {cardId}",
                    )
                    time.sleep(0.75)
                    continue
                log.warning(
                    f"[{idx + 1}/{n}] detail pane timed out after retry, skipping: {cardId}",
                )
        if not pane_loaded:
            continue
        detail = scrapeSelectedJobDetails(driver, fallback)

        jobRecord = {
            "jobId": cardId,
            "jobUrl": driver.current_url,
            "visaOrMatchNote": None,
            "location": detail.get("location"),
            "employmentType": detail.get("employmentType"),
            "salaryRange": detail.get("salaryRange"),
            "workModel": detail.get("workModel"),
            "seniority": None,
            "experience": None,
            "originalJobPostUrl": resolveOriginalApplyUrl(detail.get("originalJobPostUrl")),
            "companyName": detail.get("companyName"),
            "title": detail.get("title"),
            "qualificationTags": detail.get("qualificationTags") or "",
            "jobResponsibility": detail.get("jobResponsibility") or "",
            "postedAgo": detail.get("postedAgo"),
        }
        added, skipped = mergeNewJobsIntoDocument(data, [jobRecord])
        if added:
            saveOutputDocument(outputPath, data)
            addedCount += added
        else:
            skippedMerge += skipped
        globalSeenIds.add(cardId)
        label = jobRecord.get("companyName") or companyPreview or "?"
        preview = str(jobRecord.get("jobUrl") or "").strip()[:70]
        log.jobLine(idx + 1, n, f"{label} — {preview}…")
    return addedCount, skippedMerge, skippedKnown


def goToNextResultsPage(driver: webdriver.Chrome) -> bool:
    nextLinks = driver.find_elements(By.CSS_SELECTOR, 'a[title="Next Page"]')
    if not nextLinks:
        return False
    href = (nextLinks[0].get_attribute("href") or "").strip()
    if not href:
        return False
    nextUrl = urljoin("https://www.ziprecruiter.com", href)
    driver.get(nextUrl)
    return True


def seedSeenIdsFromDocument(data: dict) -> set[str]:
    """In-memory job rows and skip-bucket card ids for this run."""
    out: set[str] = set()
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        for j in jobs:
            if isinstance(j, dict):
                jid = j.get("jobId")
                if isinstance(jid, str) and jid:
                    out.add(jid)
    skipIds = data.get(skippedOriginalUrlIdsKey)
    if isinstance(skipIds, list):
        for sid in skipIds:
            if isinstance(sid, str) and sid.strip():
                out.add(sid.strip())
    return out


def zipRecruiterSeenIdsBeforeScrape(data: dict, outputPath: Path) -> set[str]:
    """jobData ∪ pastData for ZipRecruiter, plus document / skip-bucket ids."""
    platform = inferPlatformFromPath(outputPath)
    return set(loadKnownJobIdsByPlatform(platform)) | seedSeenIdsFromDocument(data)


def scrapeAllResultPages(
    driver: webdriver.Chrome,
    log: ScraperRunLog,
    startUrl: str,
    outputPath: Path,
    *,
    phaseLabel: str = "",
) -> tuple[int, int, int]:
    log.bindPhase(phaseLabel)
    driver.get(startUrl)
    dismissLoginPopupIfPresent(driver)
    time.sleep(1.0)

    data = loadJobsDocumentOrEmpty(outputPath)
    ensureSkippedOriginalUrlIds(data)
    seenIds = zipRecruiterSeenIdsBeforeScrape(data, outputPath)
    if seenIds:
        log.existingJobsNotice(len(seenIds), outputPath.name)

    totalAdded = 0
    totalSkippedMerge = 0
    totalSkippedKnownCards = 0
    pageNumber = 1
    while True:
        added, skippedMerge, skippedKnown = scrapeCurrentPageJobs(
            driver,
            log,
            seenIds,
            pageNumber,
            data,
            outputPath,
            phaseLabel=phaseLabel,
        )
        totalAdded += added
        totalSkippedMerge += skippedMerge
        totalSkippedKnownCards += skippedKnown
        if added or skippedMerge:
            log.mergeCheckpoint(
                str(outputPath.resolve()),
                added,
                skippedMerge,
                extra="(duplicate jobId or invalid row)",
            )

        if not goToNextResultsPage(driver):
            break
        time.sleep(1.0)
        pageNumber += 1

    return totalAdded, totalSkippedMerge, totalSkippedKnownCards


def main() -> int:
    runLog = ScraperRunLog(PLATFORM_ZIPRECRUITER)
    parser = argparse.ArgumentParser(
        description="Scrape ZipRecruiter search into DB-backed storage."
    )
    parser.add_argument(
        "searchUrl",
        nargs="?",
        default=None,
        help="Optional full search URL (single run). Else SCRAPER_SEARCH_KEYWORDS / built-in defaults.",
    )
    args = parser.parse_args()

    phases = resolveZipRecruiterSearchPhases(args.searchUrl)
    if not phases:
        runLog.error("No ZipRecruiter search keywords or URLs configured.")
        return 1

    try:
        outputPath = ZIPRECRUITER_SOURCE_PATH
    except ValueError as exc:
        runLog.error(str(exc))
        return 1
    headless = envBool("SCRAPING_HEADLESS", default=True)
    os.environ["USE_UNDETECTED_CHROME"] = "1"

    try:
        driver = createScrapingChromeDriver(headless=headless, quiet=True)
    except ValueError as exc:
        runLog.error(str(exc))
        return 1

    try:
        driver.set_page_load_timeout(120)
        totalAdded = 0
        totalSkippedMerge = 0
        totalSkippedKnown = 0
        for phaseNum, (startUrl, phaseLabel) in enumerate(phases, start=1):
            runLog.bindPhase(phaseLabel)
            runLog.phaseStart(
                phaseNum,
                len(phases),
                phaseLabel,
                "all pages for this search",
            )
            added, skippedMerge, skippedKnown = scrapeAllResultPages(
                driver,
                runLog,
                startUrl,
                outputPath,
                phaseLabel=phaseLabel,
            )
            totalAdded += added
            totalSkippedMerge += skippedMerge
            totalSkippedKnown += skippedKnown
            runLog.phaseDone(
                phaseLabel,
                f"+{added} new row(s); {skippedKnown} list card(s) skipped as already known",
            )
        runLog.runDone(
            f"+{totalAdded} new row(s); {totalSkippedMerge} merge skip(s); "
            f"{totalSkippedKnown} known-card skip(s) across {len(phases)} phase(s) → "
            f"{outputPath.resolve()}",
        )
        promptBeforeClosingBrowserIfHeaded()
    finally:
        driver.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
