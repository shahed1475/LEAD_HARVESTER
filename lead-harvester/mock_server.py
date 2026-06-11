"""
Mock server — mimics the 3 ClarioScope Laravel endpoints for local testing.

Run:
    uvicorn mock_server:app --reload --port 8000

Then set in .env:
    CLARIOSCOPE_URL=http://localhost:8000
    HARVESTER_TOKEN=test_token_123

The server serves one job per poll, cycles through a preset list,
validates every ingest batch against the §3 schema, and prints clear
summaries so you can see what the poller is doing without a real backend.
"""
from __future__ import annotations
import json
import sys
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Path
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="ClarioScope Lead Harvester Mock")

TOKEN = "test_token_123"

# ── Job queue (cycles endlessly) ───────────────────────────────────────────────
_JOBS = [
    {"id": 1, "vertical_slug": "dental",       "state": "IL", "city": "Chicago",   "target_count": 10, "exclude_domains": []},
    {"id": 2, "vertical_slug": "mental-health", "state": "TX", "city": "Austin",    "target_count": 10, "exclude_domains": []},
    {"id": 3, "vertical_slug": "primary-care",  "state": "CA", "city": "Los Angeles","target_count": 10, "exclude_domains": []},
    {"id": 4, "vertical_slug": "med-spa",        "state": "FL", "city": "Miami",     "target_count": 5,  "exclude_domains": []},
    {"id": 5, "vertical_slug": "weight-loss",    "state": "NY", "city": "New York",  "target_count": 5,  "exclude_domains": []},
]

_job_index = 0
_job_states: dict[int, str] = {}  # job_id → status
_served_at: dict[int, float] = {}

# ── Auth helper ────────────────────────────────────────────────────────────────

def _check_token(token: str | None) -> None:
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Harvester-Token")


# ── Endpoint 1: GET /api/lead-harvester/jobs/next ─────────────────────────────

@app.get("/api/lead-harvester/jobs/next")
def get_next_job(x_harvester_token: str | None = Header(default=None)):
    global _job_index
    _check_token(x_harvester_token)

    # Find a queued job (simple round-robin; skip in_progress)
    for _ in range(len(_JOBS)):
        job = _JOBS[_job_index % len(_JOBS)]
        _job_index += 1
        jid = job["id"]
        if _job_states.get(jid) not in ("in_progress", "completed"):
            _job_states[jid] = "in_progress"
            _served_at[jid] = time.monotonic()
            print(f"\n[MOCK] >> Serving job {jid}: {job['vertical_slug']} / {job['state']} / {job['city']}")
            return {"job": job}

    print("[MOCK] 204 — all jobs are in_progress or completed")
    return Response(status_code=204)


# ── Endpoint 2: POST /api/lead-harvester/jobs/{id}/ingest ─────────────────────

class IngestPayload(BaseModel):
    leads: list[dict[str, Any]]
    is_final: bool = False


@app.post("/api/lead-harvester/jobs/{job_id}/ingest")
def ingest_leads(
    job_id: int = Path(...),
    payload: IngestPayload = ...,
    x_harvester_token: str | None = Header(default=None),
):
    _check_token(x_harvester_token)

    created = 0
    failed = 0
    errors = []

    for i, lead in enumerate(payload.leads):
        issues = _validate_lead(lead)
        if issues:
            failed += 1
            for issue in issues:
                errors.append({"index": i, "reason": issue})
        else:
            created += 1

    print(
        f"[MOCK] IN  Job {job_id} batch received: "
        f"{len(payload.leads)} leads | created={created} failed={failed} "
        f"is_final={payload.is_final}"
    )
    if errors:
        for e in errors[:5]:  # print first 5 errors
            print(f"       WARN index {e['index']}: {e['reason']}")

    if payload.is_final:
        _job_states[job_id] = "completed"
        elapsed = time.monotonic() - _served_at.get(job_id, time.monotonic())
        print(f"[MOCK] DONE Job {job_id} COMPLETED in {elapsed:.1f}s")

    return {
        "job_id": job_id,
        "received": len(payload.leads),
        "created": created,
        "updated": 0,
        "duplicates": 0,
        "failed": failed,
        "errors": errors,
    }


# ── Endpoint 3: POST /api/lead-harvester/jobs/{id}/failed ─────────────────────

class FailedPayload(BaseModel):
    error: str


@app.post("/api/lead-harvester/jobs/{job_id}/failed")
def job_failed(
    job_id: int = Path(...),
    payload: FailedPayload = ...,
    x_harvester_token: str | None = Header(default=None),
):
    _check_token(x_harvester_token)
    _job_states[job_id] = "failed"
    print(f"[MOCK] FAIL Job {job_id} FAILED: {payload.error}")
    return {"ok": True}


# ── Validation helper ──────────────────────────────────────────────────────────

def _validate_lead(lead: dict) -> list[str]:
    issues = []
    if not lead.get("practice_name"):
        issues.append("practice_name is required")
    if not lead.get("email") and not lead.get("phone"):
        issues.append("no contact channel (email or phone required)")
    if lead.get("website") and not lead["website"].startswith("http"):
        issues.append("website is not a valid url")
    if not lead.get("source"):
        issues.append("source field is missing")
    if not lead.get("external_id"):
        issues.append("external_id is missing (idempotency key required)")
    return issues


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "mock server running", "token": TOKEN}


if __name__ == "__main__":
    print("=" * 60)
    print("ClarioScope Mock Server")
    print(f"  Token:    {TOKEN}")
    print("  URL:      http://localhost:8000")
    print("")
    print("Set your .env:")
    print(f"  CLARIOSCOPE_URL=http://localhost:8000")
    print(f"  HARVESTER_TOKEN={TOKEN}")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
