# ClarioScope Lead Harvester — Integration Specification

**Audience:** the developer building the Python lead-collection service.
**Goal:** a standalone Python worker that scrapes real, high-intent healthcare-practice
leads (primary care, mental health, dental, med-spa, etc.) across every US state/city
**without using any paid data APIs**, and feeds them into the ClarioScope Laravel app so
they auto-populate our lead lists and marketing campaigns.

**Hosting (decided):** the worker runs as a Docker container on an **Oracle Cloud Free Tier**
Ampere A1 (**ARM64**) VM — always-on, free, reaching ClarioScope over HTTPS. It does **not**
run on Laravel Cloud (that platform is PHP-only and can't host a Python scraper). Full VM
setup, including the ARM64 and single-IP gotchas, is in §11.

This spec tells you **exactly** what shape the data must be, **how** to hand it to the
Laravel app, and **what the Laravel side will expose for you**. Build against this contract
and the integration will drop in cleanly — no rework on our side.

---

## 0. TL;DR — the contract in one paragraph

Your Python service runs as a long-lived container on an **Oracle Cloud Free Tier** Ampere
(ARM64) VM — same poller pattern as our existing `FL/` video worker. It **pulls a work assignment** from Laravel (`GET /api/lead-harvester/jobs/next`),
scrapes business listings for that `(vertical, state, city)` target using your own
scraping stack, then **pushes a batch of leads back** (`POST /api/lead-harvester/jobs/{id}/ingest`).
All calls carry a single shared bearer token in the `X-Harvester-Token` header. You never
touch the database directly and you never need an inbound port — Laravel Cloud can't reach
you, so everything is poll/push initiated by **you**. We handle dedupe, enrichment, and
list/campaign enrollment after ingest.

> **"No APIs" = no paid lead-data vendors** (no ZoomInfo, Apollo, Google Places API, etc.)
> for *acquiring* the leads. The HTTP calls **into ClarioScope are not "an API" in that
> sense** — that's our own app's private ingest endpoint and is the correct, expected way
> to deliver data. Do **not** try to write to Postgres directly.

---

## 1. Why this architecture (read this first)

We already run one external worker this exact way — the Remotion video renderer in `FL/`.
It works because:

- **Laravel Cloud is the server; your scraper is a client.** The platform can't open a
  connection *to* your container. So the worker must initiate every call (pull work, push
  results). This is the "poller" pattern.
- **Shared bearer token, not HMAC.** Internal trusted worker → a constant-time-compared
  secret header is enough. (We reserve HMAC for third parties like Stripe/ElevenLabs.)
- **No direct DB writes.** All writes go through Laravel so our validation, de-duplication,
  consent defaults, enrichment jobs, and campaign auto-enrollment all fire automatically.
  If you `INSERT` into Postgres yourself you bypass every one of those and create silent
  data corruption. **Do not do it.**

Reference implementation to copy from (already in this repo):
- Worker side: `FL/scripts/clarioscope-poller.ts`, `FL/Dockerfile`, `FL/docker-compose.yml`
- Laravel side: `app/Http/Controllers/Api/ReelJobController.php`, routes in `routes/api.php`
  (search for `/reels/jobs/next`), token config in `config/services.php` (`reels.ingest_token`).

Build the Python version as a 1:1 analogue of that.

---

## 2. The two halves

```
┌──────────────────────────────┐         pull work          ┌───────────────────────────┐
│  Python Lead Harvester        │  GET /jobs/next  ───────▶  │  ClarioScope (Laravel Cloud)│
│  (your container)             │                            │                             │
│  - scrape public listings     │  ◀─── {vertical, state,    │  - hands out scrape targets │
│  - parse name/phone/site/etc. │        city, job_id}       │  - validates + dedupes      │
│  - normalize                  │                            │  - writes `leads` rows      │
│                               │  POST /jobs/{id}/ingest ─▶ │  - queues enrichment        │
│                               │   { leads: [ {...}, ... ]} │  - auto-enrolls campaigns   │
│                               │  ◀─── {created, duplicates,│                             │
│                               │        failed, errors[]}   │                             │
└──────────────────────────────┘                            └───────────────────────────┘
```

**You build:** the left box (Python).
**We build:** the right box endpoints (`LeadHarvesterJobController` + routes + config +
a `lead_harvester_jobs` table). See §7 — that part is on the ClarioScope team, but it's
documented here so you know the exact contract.

---

## 3. The lead payload — the single most important section

When you POST a batch, each lead is a JSON object. The field names below map **directly**
to columns the Laravel importer already understands. Use these exact keys.

### 3.1 Required (at least ONE contact channel must be present)

A lead is rejected if it has **neither** `email` **nor** `phone`.

| Field           | Type   | Notes |
|-----------------|--------|-------|
| `practice_name` | string | The business/clinic name. **Always send this.** This is the de-dupe anchor for many cases. Max 255. |
| `email`         | string | Public business email if found while scraping. Nullable. We will *enrich* missing emails ourselves (see §5) if you send a `website`. |
| `phone`         | string | Public phone. Digits/format don't matter; we normalize. Nullable. |

> Rule: send **`email` OR `phone`** (ideally both). If you only have a phone, that's fine —
> we mark the lead `phone_only` and try to scrape an email from the website later.

### 3.2 Strongly recommended (drives targeting & quality)

| Field          | Type   | Notes |
|----------------|--------|-------|
| `name`         | string | Owner / primary contact name if discoverable. If unknown, send the practice name or omit. Max 255. |
| `website`      | string | Practice website URL. **High value** — if present and email is missing, we auto-run our email-harvest + clinic-intelligence enrichment on it. |
| `specialty`    | string | Free-text specialty as scraped (e.g. "Pediatric Dentistry"). |
| `vertical_slug`| string | Normalized vertical. **Use one of the controlled values in §4.** If you can't classify confidently, omit it — our enrichment will infer it from the website. |
| `city`         | string | Max 120. |
| `state`        | string | 2-letter or full; max 60. We index on this. |
| `country`      | string | Default `"US"`. Max 60. |

### 3.3 Optional extras (stored if present)

| Field          | Type    | Notes |
|----------------|---------|-------|
| `linkedin_url` | string  | Max 512. |
| `founded_year` | integer | Year, e.g. 2014. |
| `notes`        | string  | Anything useful you scraped (hours, # of providers, blurb). Max 2000. |
| `source`       | string  | **Always set to `"lead_harvester"`** so we can trace origin. We'll default it if omitted. |
| `source_detail`| string  | Where you scraped it from (e.g. `"yelp"`, `"healthgrades_listing"`, `"clinic_directory"`). Stored in `external_ids`/`enrichment_data`. |
| `external_id`  | string  | Your own stable ID for this listing, so re-scrapes can be matched. Stored under `external_ids.harvester_id`. |
| `raw`          | object  | The full raw scraped record (any shape). We persist it in `enrichment_data.harvester` for auditing. **Send this** — it's cheap and lets us reprocess later without re-scraping. |

### 3.4 Do NOT send these (Laravel owns them)

`stage`, `lifecycle_stage`, `risk_score`, `fit_score`, `practice_id`, `assigned_to`,
opt-out / consent flags, demo_* fields, engagement_* fields, timestamps. These are managed
by our CRM and marketing engine. Anything you send here will be ignored.

### 3.5 Example batch payload

```json
{
  "leads": [
    {
      "practice_name": "Lakeview Family Dentistry",
      "name": "Dr. Sarah Tran",
      "email": "frontdesk@lakeviewdental.com",
      "phone": "(312) 555-0184",
      "website": "https://lakeviewdental.com",
      "specialty": "General & Cosmetic Dentistry",
      "vertical_slug": "dental",
      "city": "Chicago",
      "state": "IL",
      "country": "US",
      "founded_year": 2011,
      "source": "lead_harvester",
      "source_detail": "yelp_business_listing",
      "external_id": "yelp:lakeview-family-dentistry-chicago",
      "notes": "4 operatories, accepts new patients, open Sat.",
      "raw": { "...": "the full scraped record goes here" }
    },
    {
      "practice_name": "Calm Mind Counseling",
      "phone": "773-555-0199",
      "website": "https://calmmindcounseling.com",
      "vertical_slug": "mental-health",
      "city": "Chicago",
      "state": "IL",
      "country": "US",
      "source": "lead_harvester",
      "source_detail": "psychology_today_profile",
      "external_id": "ptoday:calm-mind-counseling",
      "raw": { "...": "..." }
    }
  ]
}
```

---

## 4. Controlled vocabulary — `vertical_slug`

Use these exact slugs (lowercase, hyphenated). They match what our clinic-intelligence
classifier already produces. If unsure, **omit** the field rather than guess — we will infer
it from the scraped website.

| `vertical_slug`  | Covers |
|------------------|--------|
| `primary-care`   | Family medicine, internal medicine, GP, preventive care |
| `mental-health`  | Therapists, psychologists, counseling, psychiatry, CBT/EMDR |
| `dental`         | General dentistry, ortho, implants, cosmetic |
| `med-spa`        | Aesthetics, botox/filler, laser, coolsculpt |
| `weight-loss`    | GLP-1 / semaglutide / metabolic clinics |
| `house-call`     | Mobile / in-home clinics |

> If the founder wants more verticals (chiropractic, derm, vision, urgent care, etc.) we'll
> add slugs on the Laravel side first and tell you the new values. **Don't invent slugs** —
> an unknown slug just won't match any campaign targeting.

### Geography
Target **all 50 US states + DC**. Iterate `(vertical × state × city)`. Laravel hands you one
target per job (§6) so you don't have to manage the iteration grid yourself — but your
scraper must be able to take a `state` + `city` + `vertical` and return listings for it.

---

## 5. What happens after you POST (so you understand the downstream)

You don't need to implement any of this — but knowing it prevents you from duplicating work:

1. **Validation** — each lead is validated (contact channel present, field lengths).
2. **De-duplication** — see §8. Existing leads are *updated*, not duplicated.
3. **Email harvesting** — if a lead has a `website` but no `email`, Laravel queues
   `HarvestLeadEmailJob` which scrapes the site (root + `/contact`, `/about`) for a real
   practice email and sets `email_status` accordingly. **So: always send `website` even if
   you couldn't find an email** — we'll get it.
4. **Clinic intelligence enrichment** — `EnrichLeadJob` visits the website to detect vertical,
   tech stack, provider count, owner name, marketing maturity → written to `clinic_profiles`.
5. **List + campaign enrollment** — leads land in a `lead_list` (named per job/run) and can be
   auto-enrolled into the matching vertical's marketing campaign.

This means **the value you add is breadth and accuracy of scraping**, not enrichment — focus
your effort on finding real practices with at least a website or phone.

---

## 6. Work-assignment endpoint — `GET /jobs/next`

Pull-based so you never need an inbound port.

```
GET https://clarioscope.ai/api/lead-harvester/jobs/next
Headers:
  X-Harvester-Token: <shared secret>
  Accept: application/json
```

**Response 200 (work available):**
```json
{
  "job": {
    "id": 4821,
    "vertical_slug": "dental",
    "state": "IL",
    "city": "Chicago",
    "target_count": 200,
    "exclude_domains": ["lakeviewdental.com"],
    "notes": "skip listings already in our DB where possible"
  }
}
```

**Response 204 (no work right now):** empty body. Sleep `HARVESTER_POLL_INTERVAL_MS` and poll again.

**Response 401:** bad/missing token. Log and stop — don't hammer.

> `exclude_domains` is a best-effort hint of domains we already have, so you can skip them and
> spend your scrape budget on net-new practices. It is **not** exhaustive — we still de-dupe
> server-side, so don't rely on it for correctness, only for efficiency.

If the founder hasn't queued any targets yet, you'll just get 204s. There will be an admin
screen on our side to enqueue `(vertical, state, city, count)` targets.

---

## 7. Ingest endpoint — `POST /jobs/{id}/ingest`

```
POST https://clarioscope.ai/api/lead-harvester/jobs/4821/ingest
Headers:
  X-Harvester-Token: <shared secret>
  Content-Type: application/json
Body:
  { "leads": [ { ...lead object... }, ... ], "is_final": true }
```

- Send in **batches of up to 200 leads** per request. For a large target, send multiple
  batches and set `"is_final": true` only on the last one — that signals Laravel to close the
  job and trigger list/campaign enrollment.
- **Idempotency:** include `external_id` on every lead (§3.3). Re-sending the same batch will
  update existing rows, never duplicate. Safe to retry on network failure.

**Response 200:**
```json
{
  "job_id": 4821,
  "received": 200,
  "created": 173,
  "updated": 12,
  "duplicates": 9,
  "failed": 6,
  "errors": [
    { "index": 41, "reason": "no contact channel (email or phone required)" },
    { "index": 88, "reason": "website is not a valid url" }
  ]
}
```

Use `errors[]` to fix your scraper output. A lead in `errors` was **not** stored.

**Failure reporting:** if the *whole job* fails on your side (e.g. the source blocked you),
call:
```
POST /api/lead-harvester/jobs/{id}/failed
Body: { "error": "yelp returned 403 after 12 pages" }
```
so the job doesn't sit "in progress" forever.

---

## 8. De-duplication rules (server-side — know these)

Laravel decides "is this a new lead?" using the list's `dedupe_strategy`. For the harvester
the default will be **`email_or_phone`**:

- If an existing lead matches on **email** OR **normalized phone**, it's an **update** (we
  fill in any missing fields you provided — e.g. add a website we didn't have).
- Otherwise it's a **create**.
- `external_id` is also matched (under `external_ids.harvester_id`) so re-scrapes of the same
  listing reconcile cleanly even if the phone/email changed.

**Implication for you:** always send `external_id`, and send phone in any format — we
normalize. You do **not** need to de-dupe across runs yourself, but de-duping *within a single
batch* (don't send the same listing twice in one POST) is appreciated.

---

## 9. Data quality bar (what makes a lead "rich")

The founder's intent is *real, high-intent* leads — not directory noise. Aim for:

- ✅ A real, currently-operating practice (skip closed/"permanently closed" listings).
- ✅ At least a `website` **or** a `phone`. A `website` is worth more (it unlocks our enrichment).
- ✅ Correct `state`/`city` (we segment campaigns geographically).
- ✅ A plausible `vertical_slug` or a website we can classify.
- ❌ Avoid: hospital systems / huge enterprises (we sell to SMB practices), aggregators,
  duplicate franchise pages, listings with no contactable channel at all.

Send the `raw` blob always — it's our safety net for reprocessing.

---

## 10. Legal / compliance — read, don't skip

We are scraping **public business listings for B2B outreach**, which is generally acceptable,
but build defensively:

- **Respect `robots.txt`** and reasonable rate limits. Identify a sane `User-Agent`. Getting
  our scraper IPs banned hurts everyone. Add jitter/backoff; never hammer a single host.
- **Public business contact info only.** Do **not** scrape patient data, reviews containing
  PII, or anything behind a login. This is HIPAA-adjacent territory — practices are the
  *target*, patients are never in scope.
- **Consent defaults are handled by Laravel:** harvested leads get `marketing_consent_email = true`
  (B2B cold email under CAN-SPAM is permitted with opt-out), but `sms`/`whatsapp` consent
  default to **false** — we will **not** auto-text scraped numbers (TCPA). You don't need to
  set these; just know phone numbers you send won't be auto-SMS'd.
- Each source's ToS varies. When in doubt about a specific site, flag it to the founder before
  adding it as a source.

---

## 11. Deployment — Oracle Cloud Free Tier (decided)

The worker runs as a Docker container on an **Oracle Cloud "Always Free" Ampere A1 VM**, in the
same poller shape as our `FL/` renderer. It reaches ClarioScope purely over **outbound HTTPS**,
so it needs **no inbound ports** — which conveniently sidesteps Oracle's restrictive default
firewall. It does **not** run on Laravel Cloud (PHP-only; see §0).

### 11.1 Project layout
Create a sibling directory, `lead-harvester/`, structured like `FL/`:
```
lead-harvester/
  Dockerfile            # FROM python:3.12-slim (multi-arch → pulls arm64); + Playwright/Chromium arm64 deps
  docker-compose.yml    # service "harvester", restart: unless-stopped, env_file: .env  (optional — docker run works too)
  requirements.txt
  .env                  # CLARIOSCOPE_URL, HARVESTER_TOKEN, HARVESTER_POLL_INTERVAL_MS, PROXY_URL
  src/
    poller.py           # main loop: pull job → scrape → push batches → mark final
    scrapers/           # one module per source
    normalize.py        # phone/url/name cleanup → the §3 schema
```

### 11.2 Main loop (pseudocode)
```python
while True:
    job = get_next_job()            # GET /jobs/next
    if job is None:                 # 204
        sleep(POLL_INTERVAL); continue
    try:
        leads = scrape(job)         # your scraping stack, target = job(vertical,state,city)
        for batch in chunked(leads, 200):
            post_ingest(job.id, batch, is_final=(batch is last))
    except Exception as e:
        post_failed(job.id, str(e))
```

### 11.3 Environment variables
```
CLARIOSCOPE_URL=https://clarioscope.ai
HARVESTER_TOKEN=<64-char hex, generated by founder, set on the VM AND in Laravel Cloud>
HARVESTER_POLL_INTERVAL_MS=30000
PROXY_URL=<rotating-proxy endpoint>        # see §11.8 — effectively mandatory at volume
```

### 11.4 ⚠️ ARM64 — the one gotcha that will bite you
Oracle's free tier is **Ampere A1 = ARM64 (aarch64)**, not x86. Everything in the image must be
arm64-built:
- `python:3.12-slim` is multi-arch — it auto-pulls arm64. Fine.
- **Playwright / Chromium:** use the arm64 browser build. `playwright install chromium` +
  `playwright install-deps` work on aarch64 Linux, but the system libs differ from x86 —
  **launch the headless browser on the VM once, by hand, before writing any scrapers.** This
  is where almost every ARM surprise happens.
- If you build the image on an x86 laptop, either build for arm64
  (`docker buildx build --platform linux/arm64`) or — simplest — **just build on the VM itself.**

### 11.5 Provision the VM
- Shape: **VM.Standard.A1.Flex** (Ampere). The Always-Free envelope is up to **4 OCPU / 24 GB
  RAM** total — give this box ~**2 OCPU / 12 GB** (headless Chromium is memory-hungry).
- OS image: **Ubuntu 22.04/24.04 (aarch64)** or Oracle Linux — your choice.
- Storage: default boot volume is fine (Always Free includes up to 200 GB block storage).
- **Keep it busy.** Oracle can reclaim *idle* Always-Free compute after extended low usage; a
  scraper that polls + scrapes around the clock is never idle, so this won't bite — just don't
  leave it parked at 0% for weeks.

### 11.6 Networking — you need nothing inbound
- The poller only makes **outbound** HTTPS calls to `clarioscope.ai`. Oracle permits outbound by
  default, so **leave the Security List / iptables locked down** — do **not** open any ingress
  ports. (OCI Ubuntu images ship with strict baked-in `iptables` rules that block inbound; that
  is *correct* for us — ignore it, don't "fix" it.)

### 11.7 Install + run (on the VM)
```bash
sudo apt-get update && sudo apt-get install -y docker.io git
sudo systemctl enable --now docker      # <-- survives VM reboots (the FL power-cut lesson)

git clone <repo>           # or scp the lead-harvester/ dir over
cd lead-harvester
docker build -t lead-harvester:latest .          # builds arm64 natively on the VM
docker run -d --name lead-harvester \
  --restart unless-stopped \
  --env-file .env \
  lead-harvester:latest
```
`--restart unless-stopped` **plus** `systemctl enable docker` together guarantee the worker
comes back automatically after a VM reboot — unlike the `FL/` poller, which we have to restart
by hand after every power cut. **Don't repeat that mistake here.**

### 11.8 The single-IP problem (read before §13.3)
An Oracle VM has **one datacenter IP**. Many listing sites (a) rate-limit hard per IP and
(b) block known cloud IP ranges outright — Oracle's included. Scraping US-wide volume from one
Oracle IP will get throttled or banned fast. So **rotating proxies are effectively mandatory**
beyond light volume (§13.3). Route all *scraping* traffic through the proxy pool (`PROXY_URL`);
keep only the *ClarioScope ingest* calls on the VM's own IP.

---

## 12. What the ClarioScope team will build (so you can integrate against it)

So you can develop against a real endpoint, here's what we'll add on our side. None of this is
your job, but the contract above is what we'll honor:

1. `lead_harvester_jobs` table — `id, vertical_slug, state, city, target_count, status
   (queued|in_progress|completed|failed), claimed_at, completed_at, stats(json), error`.
2. `App\Http\Controllers\Api\LeadHarvesterJobController` with `next()`, `ingest()`, `failed()`
   — modeled on `ReelJobController`, token-checked with `hash_equals()`.
3. Routes in `routes/api.php`:
   ```php
   Route::get('/lead-harvester/jobs/next', [LeadHarvesterJobController::class, 'next']);
   Route::post('/lead-harvester/jobs/{job}/ingest', [LeadHarvesterJobController::class, 'ingest']);
   Route::post('/lead-harvester/jobs/{job}/failed', [LeadHarvesterJobController::class, 'failed']);
   ```
4. `config/services.php`: `'lead_harvester' => ['token' => env('HARVESTER_TOKEN')]`.
5. `ingest()` reuses our existing `LeadImportService` mapping + dedupe so your batches flow
   through the same validated path as CSV imports, then attaches leads to a per-job `lead_list`.
6. An admin screen to enqueue targets `(vertical × state × city × count)`.

---

## 13. Open questions for the founder (answer before coding the scrapers)

1. **Which sources first?** (Google Maps/Business listings, Yelp, Healthgrades, Psychology
   Today, Zocdoc, state license boards, vertical-specific directories…?) Pick 2–3 to start.
2. **Volume target** — leads/day, and how aggressively to scrape (affects ban risk + infra).
3. **Proxy budget** — the Oracle VM has a single datacenter IP that big sites will throttle or
   block (§11.8), so rotating residential proxies are effectively required at any real volume.
   Proxies are *infrastructure*, not a data API, so they don't violate the "no APIs" rule — but
   confirm the founder is OK spending on a proxy pool, and roughly how much.
4. **Vertical priority order** — which verticals/states matter most for the first run.

---

*Questions on the Laravel-side contract → ping the ClarioScope team. Build the Python worker
as a twin of `FL/`, emit the §3 schema, and we'll wire the rest.*
