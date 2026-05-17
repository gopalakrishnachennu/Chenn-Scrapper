"""
NLP-based enrichment: fills blank structured fields by parsing job_description text.

Fields enriched (only when currently empty):
  workModel       -> Remote | Hybrid | Onsite
  seniority       -> Entry Level | Mid Level | Senior Level | Lead/Staff
  experience      -> e.g. "3+ years exp"
  employmentType  -> Full-time | Contract

Entry point for whole-DB pass:
  python utils/descriptionEnricher.py
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Patterns — work model
# ---------------------------------------------------------------------------

_HYBRID_RES = [
    re.compile(r'\bhybrid\b', re.I),
    re.compile(r'in[- ]?office\s+\d+\s+day', re.I),
    re.compile(r'\d+\s+days?\s+(?:per\s+week\s+)?(?:in[- ]?office|on[- ]?site)', re.I),
    re.compile(r'part(?:ly|ially)[- ]remote', re.I),
]

_REMOTE_RES = [
    re.compile(r'\bfully[- ]remote\b', re.I),
    re.compile(r'\b100\s*%\s*remote\b', re.I),
    re.compile(r'\bremote[- ]first\b', re.I),
    re.compile(r'\bremote[- ]only\b', re.I),
    # plain "remote" but NOT remote sensing / desktop / access / server / control
    re.compile(
        r'\bremote\b(?!\s*(?:sensing|desktop|access|server|control|monitoring|support|procedure|assistance))',
        re.I,
    ),
]

_ONSITE_RES = [
    re.compile(r'\bon[- ]?site\b', re.I),
    re.compile(r'\bin[- ]?office\b', re.I),
    re.compile(r'\bin[- ]?person\b', re.I),
    re.compile(r'\bfully\s+(?:on[- ]?site|in[- ]?office)\b', re.I),
]

# ---------------------------------------------------------------------------
# Patterns — seniority  (checked against title + description)
# ---------------------------------------------------------------------------

# List of (compiled pattern, label) — MOST specific first
_SENIORITY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(?:principal|distinguished|fellow)\b', re.I), 'Lead/Staff'),
    (re.compile(r'\b(?:staff\s+engineer|lead\s+engineer|tech\s+lead|team\s+lead|engineering\s+lead)\b', re.I), 'Lead/Staff'),
    (re.compile(r'\b(?:senior|sr\.?)\s+(?:devops|cloud|sre|platform|software|data|infra|infrastructure|systems?|network|security)\b', re.I), 'Senior Level'),
    (re.compile(r'\b(?:senior|sr\.?)\b', re.I), 'Senior Level'),
    (re.compile(r'\bmid[- ]?(?:level|senior|career)\b', re.I), 'Mid Level'),
    (re.compile(r'\bintermediate\b', re.I), 'Mid Level'),
    (re.compile(r'\b(?:entry[- ]?level|associate|new\s+grad(?:uate)?|recent\s+grad(?:uate)?)\b', re.I), 'Entry Level'),
    (re.compile(r'\bjunior\b|\bjr\.?\b', re.I), 'Entry Level'),
]

# ---------------------------------------------------------------------------
# Patterns — experience years
# ---------------------------------------------------------------------------

# "3-5 years", "3–5 years of experience"  →  use lower bound
_EXP_RANGE_RE = re.compile(
    r'(\d+)\s*[-–]\s*(\d+)\s*\+?\s*years?(?:\s+of)?\s*(?:experience|exp\.?)?',
    re.I,
)
# "5+ years experience"
_EXP_PLUS_RE = re.compile(
    r'(\d+)\s*\+\s*years?(?:\s+of)?\s*(?:experience|exp\.?)?',
    re.I,
)
# "minimum 3 years" / "at least 4 years"
_EXP_MIN_RE = re.compile(
    r'(?:minimum|at\s+least|min\.?)\s+(\d+)\s+years?(?:\s+of)?\s*(?:experience|exp\.?)?',
    re.I,
)
# plain "X years experience"
_EXP_PLAIN_RE = re.compile(
    r'(\d+)\s+years?\s+(?:of\s+)?(?:experience|exp\.?)',
    re.I,
)

# ---------------------------------------------------------------------------
# Patterns — employment type
# ---------------------------------------------------------------------------

_CONTRACT_EXCL_RE = re.compile(r'\bno\s+(?:contract|c2c|1099)\b', re.I)

_CONTRACT_RES = [
    re.compile(r'\bc2c\b|\bcorp[- ]to[- ]corp\b', re.I),
    re.compile(r'\b1099\b', re.I),
    re.compile(r'\bw[- ]?2\s+contract\b', re.I),
    re.compile(r'\bcontract(?:or)?\b', re.I),
]

_FULLTIME_RES = [
    re.compile(r'\bfull[- ]?time\b', re.I),
    re.compile(r'\bpermanent\s+(?:position|role|job|employee)\b', re.I),
    re.compile(r'\bdirect\s+hire\b', re.I),
]


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _any_match(text: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def _extract_work_model(text: str) -> str:
    # Hybrid check first — many hybrid postings also mention "remote days"
    if _any_match(text, _HYBRID_RES):
        return 'Hybrid'
    if _any_match(text, _REMOTE_RES):
        return 'Remote'
    if _any_match(text, _ONSITE_RES):
        return 'Onsite'
    return ''


def _extract_seniority(title: str, desc: str) -> str:
    # Title is a stronger signal — check it alone first
    for pattern, label in _SENIORITY_RULES:
        if pattern.search(title):
            return label
    # Fall back to description
    for pattern, label in _SENIORITY_RULES:
        if pattern.search(desc):
            return label
    return ''


def _extract_experience(text: str) -> str:
    m = _EXP_RANGE_RE.search(text)
    if m:
        return f'{m.group(1)}+ years exp'
    m = _EXP_PLUS_RE.search(text)
    if m:
        return f'{m.group(1)}+ years exp'
    m = _EXP_MIN_RE.search(text)
    if m:
        return f'{m.group(1)}+ years exp'
    m = _EXP_PLAIN_RE.search(text)
    if m:
        return f'{m.group(1)}+ years exp'
    return ''


def _extract_employment_type(text: str) -> str:
    if _CONTRACT_EXCL_RE.search(text):
        return 'Full-time'
    if _any_match(text, _CONTRACT_RES):
        return 'Contract'
    if _any_match(text, _FULLTIME_RES):
        return 'Full-time'
    return ''


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_row(row: dict) -> dict:
    """
    Returns {field: new_value} for fields that were blank and could be inferred.
    Never overwrites fields that already have a value.
    """
    desc = str(row.get('jobDescription') or '').strip()
    if not desc:
        return {}

    title = str(row.get('title') or '').strip()
    updates: dict[str, str] = {}

    if not str(row.get('workModel') or '').strip():
        val = _extract_work_model(desc)
        if val:
            updates['workModel'] = val

    if not str(row.get('seniority') or '').strip():
        val = _extract_seniority(title, desc)
        if val:
            updates['seniority'] = val

    if not str(row.get('experience') or '').strip():
        val = _extract_experience(desc)
        if val:
            updates['experience'] = val

    if not str(row.get('employmentType') or '').strip():
        val = _extract_employment_type(desc)
        if val:
            updates['employmentType'] = val

    return updates


def enrich_jobs_in_db(*, verbose: bool = True) -> dict[str, int]:
    """
    Load every job from DB, fill blank fields, write back only changed rows.
    Returns a summary with counts per field.
    """
    from utils.dataManager import loadAllJobs, upsertJobs

    jobs = loadAllJobs()
    enriched_rows: list[dict] = []
    counts: dict[str, int] = {
        'workModel': 0,
        'seniority': 0,
        'experience': 0,
        'employmentType': 0,
    }

    for job in jobs:
        updates = enrich_row(job)
        if updates:
            enriched_rows.append({**job, **updates})
            for field in counts:
                if field in updates:
                    counts[field] += 1

    if enriched_rows:
        upsertJobs(enriched_rows)

    result = {'totalJobs': len(jobs), 'enriched': len(enriched_rows), **counts}

    if verbose:
        print(f"  Jobs in DB:            {result['totalJobs']}")
        print(f"  Rows enriched:         {result['enriched']}")
        print(f"    workModel  filled:   {result['workModel']}")
        print(f"    seniority  filled:   {result['seniority']}")
        print(f"    experience filled:   {result['experience']}")
        print(f"    empType    filled:   {result['employmentType']}")

    return result


if __name__ == '__main__':
    print('Running description enrichment on all jobs in DB...\n')
    enrich_jobs_in_db(verbose=True)
