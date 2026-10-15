"""Dependency-light demonstration API. Not a production job coordinator.

It deliberately exposes no live RIMDOCS/S3/Azure endpoints until corporate
identity, authorization and durable dispatch have been integrated.
"""
from __future__ import annotations
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from .core import Status

app = FastAPI(title="EvidenceFlow Enterprise", version="0.1.0")
_jobs: dict[str, dict] = {}


class JobRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)


@app.get("/health/live")
def liveness():
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    return {"ready": True, "mode": "demo"}


@app.post("/api/v1/jobs", status_code=202)
def create_job(request: JobRequest):
    ids = list(dict.fromkeys(value.strip() for value in request.ids if value.strip()))
    if not ids:
        raise HTTPException(422, "No valid IDs")
    job_id = str(uuid4())
    _jobs[job_id] = {"job_id": job_id, "status": Status.PENDING, "total": len(ids), "completed": 0}
    return _jobs[job_id]


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Unknown job")
    return _jobs[job_id]
