# ClarioScope Lead Harvester — Full Build Roadmap

---

## System Readiness Check

| Tool | Status | Version | Notes |
|------|--------|---------|-------|
| Docker Desktop | ✅ Running | 29.0.1 | Fully operational |
| Docker Buildx | ✅ Ready | 0.29.1 | Can build ARM64 images cross-platform |
| Docker Compose | ✅ Ready | 2.40.3 | |
| WSL2 + Ubuntu | ✅ Running | Ubuntu 2 | Good for Linux testing locally |
| Python | ✅ Installed | 3.14.0 | Also 3.11 present |
| Git | ✅ Ready | 2.52.0 | |
| httpx | ✅ Installed | 0.28.1 | |
| BeautifulSoup4 | ✅ Installed | 4.14.3 | |
| Playwright | ✅ Installed | 1.59.0 | |
| Pydantic v2 | ✅ Installed | 2.12.5 | |
| pydantic-settings | ✅ Installed | 2.13.1 | |
| lxml | ✅ Installed | 6.0.2 | |
| tenacity | ✅ Installed | 9.1.2 | Retry logic |
| fake-useragent | ✅ Installed | 2.2.0 | |
| aiofiles | ✅ Installed | 25.1.0 | |

> **One gap:** Playwright browsers (Chromium) may not be installed locally.
> Run `playwright install chromium` once before testing the Google Maps scraper.

---

## PHASE 0 — Project Scaffold (Day 1)

Set up the directory structure exactly as the spec defines:

```
lead-harvester/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── src/
    ├── __init__.py
    ├── poller.py          ← main async loop
    ├── client.py          ← ClarioScope API calls
    ├── config.py          ← env config via pydantic-settings
    ├── models.py          ← Job + Lead pydantic models
    ├── normalize.py       ← phone / URL / name / external_id
    └── scrapers/
        ├── __init__.py
        ├── base.py        ← BaseScraper abstract class
        ├── registry.py    ← vertical → [scrapers] dispatch
        ├── yelp.py
        ├── healthgrades.py
        ├── psychology_today.py
        ├── google_maps.py ← Playwright only
        └── opencare.py
```

**Deliverables:**
- `requirements.txt` pinned to exact versions
- `.env.example` with all 4 env vars documented
- `Dockerfile` using `python:3.12-slim` (not 3.14 — spec says 3.12, and 3.14 is too new for production)
- `docker-compose.yml` with `restart: unless-stopped`

---

## PHASE 1 — Core Foundation (Day 1–2)

### 1.1 — `config.py`
```
CLARIOSCOPE_URL              (required)
HARVESTER_TOKEN              (required)
HARVESTER_POLL_INTERVAL_MS   (default: 30000)
PROXY_URL                    (optional, None by default)
LOG_LEVEL                    (default: INFO)
BATCH_SIZE                   (default: 200)
```

### 1.2 — `models.py`
- `Job` model: `id, vertical_slug, state, city, target_count, exclude_domains`
- `Lead` model: all §3 fields with exact names, types, max lengths
- `IngestResponse` model: `job_id, received, created, updated, duplicates, failed, errors`
- `VerticalSlug` enum: exactly 6 values from the spec

### 1.3 — `normalize.py`
- `normalize_phone(raw)` → strip formatting, keep digits
- `normalize_url(raw)` → ensure https://, strip trailing slash
- `normalize_name(raw)` → strip whitespace, title-case
- `make_external_id(source, slug)` → `"yelp:lakeview-family-dentistry-chicago"`

### 1.4 — `client.py`
- `ClarioScopeClient` using `httpx.AsyncClient`
- `get_next_job()` → `Job | None` (204 = None, 401 = raise + stop)
- `post_ingest(job_id, leads, is_final)` → `IngestResponse`
- `post_failed(job_id, error_msg)` → void
- Retry with `tenacity` on 5xx / network errors (3 retries, exponential backoff)
- All requests carry header: `X-Harvester-Token`, `Accept: application/json`

### 1.5 — `poller.py` (skeleton)
```python
while True:
    job = await client.get_next_job()
    if job is None:
        await sleep(POLL_INTERVAL)
        continue
    try:
        leads = await scrape(job)
        for batch in chunked(leads, BATCH_SIZE):
            await client.post_ingest(job.id, batch, is_final=(batch is last))
    except Exception as e:
        await client.post_failed(job.id, str(e))
```

---

## PHASE 2 — Scrapers (Day 2–5)

Build in order of easiest → hardest. Each scraper:
- Takes `(state: str, city: str, vertical: str)` → `list[Lead]`
- Respects `exclude_domains` list from the job
- Has per-domain rate limiting: 3s base + random jitter (0.5–2s)
- Circuit breaker: stop after 5 consecutive HTTP errors
- Always sets `source`, `source_detail`, `external_id`, `raw`
- Skips "permanently closed" listings

### 2.1 — `yelp.py` (all 6 verticals)
- Search URL: `yelp.com/search?find_desc={vertical}&find_loc={city}+{state}`
- Category mappings: dental → `dentists`, mental-health → `counseling+mental+health`, etc.
- Extract: name, phone, website, address, neighborhood
- Paginate through all result pages
- Static HTML — httpx + BS4

### 2.2 — `healthgrades.py` (dental, primary-care, mental-health, weight-loss)
- Specialty search listings
- Doctor + practice profiles
- Extract: practice name, doctor name, phone, website, specialty, address
- Static HTML — httpx + BS4

### 2.3 — `psychology_today.py` (mental-health only)
- Therapist finder directory
- Very structured — name, credentials, phone, website, specialties
- Static HTML — httpx + BS4

### 2.4 — `opencare.py` (dental only)
- Dental practice directory
- Good structured data: name, phone, website, city
- Static HTML — httpx + BS4

### 2.5 — `google_maps.py` (all verticals — Phase 2 final)
- Playwright headless Chromium
- Search: `{vertical} near {city}, {state}`
- Scroll + extract all listings
- Highest ban risk — use proxy, longer delays, realistic UA
- **Test on VM ARM64 before writing full code**

### 2.6 — `registry.py` — dispatch table
```python
SCRAPER_REGISTRY = {
    "dental":        [YelpScraper, HealthgradesScraper, OpencareScraper],
    "mental-health": [PsychologyTodayScraper, HealthgradesScraper, YelpScraper],
    "primary-care":  [HealthgradesScraper, YelpScraper],
    "med-spa":       [YelpScraper],
    "weight-loss":   [HealthgradesScraper, YelpScraper],
    "house-call":    [YelpScraper, GoogleMapsScraper],
}
```

---

## PHASE 3 — Docker + ARM64 Build (Day 5–6)

### Dockerfile (key parts)
```dockerfile
FROM python:3.12-slim

# ARM64 Playwright system deps
RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY src/ ./src/
CMD ["python", "-m", "src.poller"]
```

### Build ARM64 image from Windows
```bash
docker buildx build --platform linux/arm64 -t lead-harvester:latest .
```

### docker-compose.yml
```yaml
services:
  harvester:
    image: lead-harvester:latest
    restart: unless-stopped
    env_file: .env
```

> `restart: unless-stopped` + `systemctl enable docker` together guarantee the worker
> survives VM reboots — the spec explicitly calls out that the FL/ worker had to be
> restarted manually after every power cut. **Do not repeat that mistake.**

---

## PHASE 4 — Local Testing (Day 6–7)

Since the Laravel endpoints don't exist yet, build a **mock server**:

```python
# mock_server.py — FastAPI mock of the 3 Laravel endpoints
GET  /api/lead-harvester/jobs/next         → returns a hardcoded test job
POST /api/lead-harvester/jobs/{id}/ingest  → validates schema, prints result summary
POST /api/lead-harvester/jobs/{id}/failed  → logs error message
```

### Test Checklist
- [ ] Poller gets job, calls scrapers, posts batches, marks final
- [ ] `is_final=True` only on the last batch
- [ ] `external_id` present on every lead
- [ ] `source="lead_harvester"` on every lead
- [ ] `raw` blob present on every lead
- [ ] No lead has both `email=null` and `phone=null`
- [ ] No duplicate `external_id` within a single batch
- [ ] 204 response → sleep → poll again
- [ ] Exception during scrape → POST /failed is called
- [ ] 5xx from ingest → retry with backoff, not crash

---

## PHASE 5 — Oracle Cloud Deployment (Day 7–8)

### VM Provisioning
1. Create Oracle Cloud Free Tier account
2. Provision `VM.Standard.A1.Flex` — **2 OCPU / 12 GB RAM** (Chromium is memory-hungry)
3. OS: Ubuntu 22.04 aarch64
4. Security rules: **no inbound ports** — outbound-only HTTPS is all we need

### VM Setup Commands
```bash
sudo apt-get update && sudo apt-get install -y docker.io git
sudo systemctl enable --now docker        # critical — survives VM reboots

git clone <repo>
cd lead-harvester
docker build -t lead-harvester:latest .   # builds natively on ARM64, no cross-compile needed

# Test Playwright FIRST before running the full container
docker run --rm lead-harvester:latest python -c \
  "from playwright.sync_api import sync_playwright; \
   p=sync_playwright().start(); b=p.chromium.launch(); \
   print('Chromium OK'); b.close()"

# Start the worker
docker run -d --name lead-harvester \
  --restart unless-stopped \
  --env-file .env \
  lead-harvester:latest
```

### Proxy Wiring
- All `httpx` + Playwright scraping calls route through `PROXY_URL` env var
- Ingest calls to `clarioscope.ai` skip the proxy (use VM's own IP)

---

## PHASE 6 — Monitoring & Hardening (Ongoing)

### Structured Logging
Log every poll cycle and batch response:
```json
{
  "job_id": 4821,
  "vertical": "dental",
  "state": "IL",
  "city": "Chicago",
  "leads_scraped": 187,
  "batch": "2/2",
  "created": 173,
  "updated": 8,
  "duplicates": 6,
  "failed": 0,
  "errors": []
}
```
Use `errors[]` from each ingest response to detect and fix scraper output issues.

### Rate Limiting Strategy

| Source | Base Delay | Jitter | Circuit Breaker |
|--------|-----------|--------|-----------------|
| Yelp | 3s | 0.5–2s | 5 errors → stop |
| Healthgrades | 2s | 0.5–1.5s | 5 errors → stop |
| Psychology Today | 2s | 0.5–1s | 5 errors → stop |
| OpenCare | 2s | 0.5–1s | 5 errors → stop |
| Google Maps | 5s | 1–3s | 3 errors → stop |

### Anti-Detection
- Rotate User-Agents via `fake-useragent`
- Realistic browser headers: `Accept-Language`, `Accept-Encoding`, `Referer`
- Playwright: disable automation flags (`--disable-blink-features=AutomationControlled`)
- Honor `robots.txt` for each source domain

### Proxy (When Budget Approved)
- Recommended providers: SmartProxy or Bright Data (residential rotating)
- Estimated cost: ~$15–$40/month for 500 leads/day volume
- Proxies = infrastructure, not a "paid data API" — does not violate the "no APIs" rule
- Wire through `PROXY_URL` env var — already in config from day one

---

## Summary Timeline

| Day | Milestone |
|-----|-----------|
| 1 | Scaffold, config, models, normalize, client |
| 2 | Poller loop complete, mock server for local testing |
| 3 | Yelp scraper |
| 4 | Healthgrades + Psychology Today scrapers |
| 5 | OpenCare + registry dispatch |
| 6 | Dockerfile + ARM64 build + Google Maps scraper |
| 7 | End-to-end local test against mock server — all checklist items pass |
| 8 | Oracle Cloud VM provisioned + container running live |
| 9+ | Monitor errors[], tune scrapers per source, add proxy |

---

## What We Need From the Laravel Team Before Go-Live

- [ ] 3 endpoints live and accessible on `clarioscope.ai`
  - `GET  /api/lead-harvester/jobs/next`
  - `POST /api/lead-harvester/jobs/{id}/ingest`
  - `POST /api/lead-harvester/jobs/{id}/failed`
- [ ] `HARVESTER_TOKEN` value (64-char hex) — set in both Laravel Cloud env AND on the Oracle VM
- [ ] First scrape targets queued in the admin screen `(vertical × state × city × count)`

---

## Lead Schema Quick Reference (§3 contract)

| Field | Required | Notes |
|-------|----------|-------|
| `practice_name` | Yes | Always. Dedupe anchor. Max 255. |
| `email` | One of these two | Nullable if phone present |
| `phone` | One of these two | Nullable if email present |
| `website` | Recommended | Unlocks Laravel email harvest + enrichment |
| `vertical_slug` | Recommended | Must be one of 6 exact values below |
| `city` | Recommended | Max 120 |
| `state` | Recommended | 2-letter preferred |
| `country` | Recommended | Default "US" |
| `specialty` | Optional | Free text |
| `name` | Optional | Owner/contact name |
| `source` | Always set | Hardcode to `"lead_harvester"` |
| `source_detail` | Always set | e.g. `"yelp"`, `"healthgrades_listing"` |
| `external_id` | Always set | `"{source}:{stable-slug}"` — idempotency key |
| `raw` | Always set | Full scraped object — cheap insurance |
| `notes` | Optional | Hours, provider count, blurb. Max 2000. |
| `founded_year` | Optional | Integer year |
| `linkedin_url` | Optional | Max 512 |

### Valid `vertical_slug` Values
```
primary-care   mental-health   dental   med-spa   weight-loss   house-call
```

### Never Send
`stage`, `lifecycle_stage`, `risk_score`, `fit_score`, `practice_id`,
`assigned_to`, opt-out/consent flags, `demo_*`, `engagement_*`, timestamps.
