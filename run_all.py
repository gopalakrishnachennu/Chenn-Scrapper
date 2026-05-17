"""
Full pipeline runner — scrape all platforms, enrich, validate, push.

Usage:
  python run_all.py                # full pipeline: scrape → enrich → validate → push
  python run_all.py --no-validate  # skip dValidate and push steps
  python run_all.py --no-push      # skip push to chennu.co
  python run_all.py --enrich-only  # only run the description enricher
  python run_all.py --push-only    # only push APPLY jobs to chennu.co

Scrapers run sequentially (they share a Chrome profile).
Each scraper gets CLOSE_ON_COMPLETE=1 so headed runs don't block.
Requires HARVEST_PUSH_SECRET in .env to push to chennu.co.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

# (display name, script path relative to repo root)
SCRAPERS: list[tuple[str, str]] = [
    ('JobRight',     'aJobRight.py'),
    ('GlassDoor',    'bGlassDoor.py'),
    ('ZipRecruiter', 'cZipRecruiter.py'),
    ('LinkedIn',     'eLinkedIn.py'),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _divider(char: str = '─', width: int = 60) -> None:
    print(char * width)


def _header(text: str) -> None:
    _divider()
    print(f'  {text}')
    _divider()


def _run_scraper(name: str, script: str) -> bool:
    """Run one scraper as a subprocess. Returns True on success."""
    _header(f'[{name}] scraping...')
    env = {**os.environ, 'CLOSE_ON_COMPLETE': '1'}
    result = subprocess.run(
        [PYTHON, str(REPO_ROOT / script)],
        env=env,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f'\n  ⚠  {name} exited with code {result.returncode} — continuing.\n')
        return False
    return True


def _run_enricher() -> None:
    _header('Description enrichment (filling blank fields from job text)...')
    # Import directly — no Chrome needed
    sys.path.insert(0, str(REPO_ROOT))
    from utils.descriptionEnricher import enrich_jobs_in_db
    enrich_jobs_in_db(verbose=True)


def _run_validate() -> None:
    _header('dValidate — classifying pending jobs...')
    # Pass choice "1" = syncEmptyApplyStatuses (non-interactive)
    result = subprocess.run(
        [PYTHON, str(REPO_ROOT / 'dValidate.py'), '-1'],
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f'\n  ⚠  dValidate exited with code {result.returncode}.\n')


def _run_push() -> None:
    _header('fPushToConsulting — pushing APPLY jobs to chennu.co...')
    sys.path.insert(0, str(REPO_ROOT))
    from fPushToConsulting import push_apply_jobs
    result = push_apply_jobs()
    print(f'\n  Total pushed: {result["pushed"]}')
    print(f'  Created:      {result["created"]}')
    print(f'  Skipped:      {result["skipped"]}')
    print(f'  Errors:       {result["errors"]}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description='Run all scrapers, enrich, validate, push.')
    parser.add_argument('--no-validate', action='store_true', help='Skip dValidate and push steps')
    parser.add_argument('--no-push',     action='store_true', help='Skip push to chennu.co')
    parser.add_argument('--enrich-only', action='store_true', help='Only run the description enricher')
    parser.add_argument('--push-only',   action='store_true', help='Only push APPLY jobs to chennu.co')
    args = parser.parse_args()

    start = time.time()
    failures: list[str] = []

    if args.enrich_only:
        _run_enricher()
        _divider('=')
        print(f'  Done in {time.time() - start:.1f}s')
        _divider('=')
        return 0

    if args.push_only:
        _run_push()
        _divider('=')
        print(f'  Done in {time.time() - start:.1f}s')
        _divider('=')
        return 0

    # ── Scrapers ────────────────────────────────────────────────────────────
    for name, script in SCRAPERS:
        ok = _run_scraper(name, script)
        if not ok:
            failures.append(name)

    # ── Enricher ─────────────────────────────────────────────────────────────
    _run_enricher()

    # ── Validate → Push ──────────────────────────────────────────────────────
    if not args.no_validate:
        _run_validate()
        if not args.no_push:
            _run_push()

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    _divider('=')
    if failures:
        print(f'  Finished in {elapsed:.0f}s  |  failed scrapers: {", ".join(failures)}')
    else:
        print(f'  All done in {elapsed:.0f}s')
    _divider('=')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
