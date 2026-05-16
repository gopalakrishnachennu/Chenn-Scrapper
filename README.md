# Chennu Job Viewer

Job scraping + validation pipeline for JobRight, Glassdoor, ZipRecruiter, and LinkedIn Jobs (`eLinkedIn.py`), with persistence via `utils/dataManager.py` (MongoDB and/or SQLite; see `.env.example`).

## What This Repo Does

- Scrapes jobs from multiple platforms:
  - `aJobRight.py`
  - `bGlassDoor.py`
  - `cZipRecruiter.py`
  - `eLinkedIn.py` (logged-in LinkedIn Jobs search; external apply URLs only — skips Easy Apply-only postings)
- Normalizes and writes jobs via `utils/dataManager.py` (MongoDB and/or SQLite per `.env`).
- Runs validation/push flow against Midhtech using `dValidate.py`.
- Supports local Docker runs and Cloud Run Job + Scheduler CI/CD.

## Current Architecture

- **Scrapers**: browser-based collection and normalization
- **Data layer**: `utils/dataManager.py` (MongoDB and/or SQLite)
- **Validation pipeline**: `dValidate.py`
- **Maintenance**: `klean.py` for temp/cache cleanup
- **Deploy**:
  - `Dockerfile` (container for `dValidate.py`)
  - `docker-compose.yml` (local one-shot run)
  - `.github/workflows/deployValidation.yml` (build + deploy job + deploy scheduler)
  - `.github/workflows/runValidationManual.yml` (manual one-time run)
  - `gcpCloudRun.md` (GCP setup guide)

## Environment Variables

Copy `.env.example` to `.env` and set values.

### Scraping

- `CHROME_APP_PATH`
- `SCRAPING_CHROME_DIR`
- `SCRAPING_PORT`
- `DATA_DIR`
- `SCRAPER_SEARCH_KEYWORDS`
- `SCRAPING_STALE_RETRIES`
- `SCRAPING_STALE_DELAY`
- `SCRAPING_HEADLESS`
- `CLOSE_ON_COMPLETE`
- `LINKEDIN_JOBS_LOCATION` (LinkedIn Jobs search location for `eLinkedIn.py`; default United States if unset)
- `LINKEDIN_SCROLL_MAX_ROUNDS`, `LINKEDIN_MAX_DETAIL_JOBS` (optional tuning for `eLinkedIn.py`)

### Database + Midhtech

- `MONGODB_URI`
- `MONGODB_DATABASE`
- `MIDHTECH_EMAIL`
- `MIDHTECH_PASSWORD`

## Local Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run Scrapers

```bash
python aJobRight.py
python bGlassDoor.py
python cZipRecruiter.py
python eLinkedIn.py
```

## Run Validation (`dValidate.py`)

Interactive mode:

```bash
python dValidate.py
```

CLI mode:

```bash
python dValidate.py -1
python dValidate.py -2
python dValidate.py -3
python dValidate.py -4
```

Where:

- `-1`: validate pending jobs
- `-2`: cleanup non-APPLY + prune old pastData
- `-3`: push APPLY jobs, then cleanup
- `-4`: show DB status report

## Docker (Local)

Build:

```bash
docker build -t chennu-dvalidate .
```

Run once:

```bash
docker run --rm \
  -e MONGODB_URI="..." \
  -e MONGODB_DATABASE="chennuJobViewer" \
  -e MIDHTECH_EMAIL="..." \
  -e MIDHTECH_PASSWORD="..." \
  chennu-dvalidate
```

Compose one-shot:

```bash
docker compose up
```

## CI/CD + Cloud Run Job

Automated deploy flow is documented in:

- `gcpCloudRun.md`

Current workflow:

- Build and push Docker image to Artifact Registry
- Update/create Cloud Run Job
- Ensure Cloud Scheduler runs job daily at `00:00 UTC`

## Security Notes

- Never commit real credentials in `.env`.
- Docker image is configured to avoid baking `.env` into image layers.
- Use Secret Manager for Cloud Run runtime secrets.

## Cleanup

```bash
python klean.py --dry-run
python klean.py
```

## Notes

- `linkedIn/` contains legacy LinkedIn experiments; production LinkedIn scraping uses root-level **`eLinkedIn.py`** and **`utils/`**.
- `zata/` is ignored for runtime artifacts and logs.
