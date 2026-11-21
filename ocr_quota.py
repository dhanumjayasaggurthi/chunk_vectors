"""Shared PostgreSQL OCR rate limiter for two or more extraction servers.

Only the opt-in page staging command uses this. The original embedding quota
and fast-embedding runtime are not modified. It enforces separate Vision
request/minute and image/minute budgets per Google Cloud project.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path

from config import GCP_KEY_PATH
from db import get_conn


def detect_project_id() -> str:
    """Use the same service-account key source as the existing production OCR."""
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or GCP_KEY_PATH
    if path and Path(path).is_file():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        project = data.get("project_id")
        if isinstance(project, str) and project:
            return project
    try:
        import google.auth
        _credentials, project = google.auth.default()
        if project:
            return project
    except Exception:
        pass
    raise ValueError(
        "Could not discover Vision quota project. Set "
        "GOOGLE_APPLICATION_CREDENTIALS to the production gcp_key.json path"
    )


def init_quota(project_id: str, *, rpm: int, ipm: int) -> str:
    if type(rpm) is not int or type(ipm) is not int or rpm < 1 or ipm < 16:
        raise ValueError("OCR rpm >=1 and ipm >=16 are required")
    key = hashlib.sha256(
        ("gcp-vision-document-text:" + project_id).encode("utf-8")
    ).hexdigest()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS ocr_runtime")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ocr_runtime.quota (
                  quota_key text PRIMARY KEY,
                  rpm integer NOT NULL,
                  ipm integer NOT NULL,
                  requests float8 NOT NULL,
                  images float8 NOT NULL,
                  last_refill timestamptz NOT NULL,
                  cooldown_until timestamptz,
                  multiplier float8 NOT NULL DEFAULT 1
                )
            """)
            cur.execute("""
                INSERT INTO ocr_runtime.quota
                  (quota_key,rpm,ipm,requests,images,last_refill)
                VALUES (%s,%s,%s,%s,%s,now())
                ON CONFLICT (quota_key) DO NOTHING
            """, (key,rpm,ipm,max(1,rpm / 6),max(16,ipm / 6)))
            cur.execute(
                "SELECT rpm,ipm FROM ocr_runtime.quota WHERE quota_key=%s",
                (key,),
            )
            current = cur.fetchone()
            if current != (rpm,ipm):
                raise ValueError(
                    "Conflicting OCR rate caps across servers for the same "
                    "Google project. Configure matching rpm/ipm everywhere."
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return key


class PostgresOcrLimiter:
    def __init__(self, quota_key: str, *, rpm: int, ipm: int,
                 stop=None, max_wait_seconds: int = 600):
        if max_wait_seconds < 1:
            raise ValueError("max_wait_seconds must be positive")
        self.key, self.rpm, self.ipm = quota_key, rpm, ipm
        self.stop, self.max_wait_seconds = stop, max_wait_seconds

    def acquire(self, image_count: int) -> None:
        if not 1 <= image_count <= 16:
            raise ValueError("Vision synchronous request must contain 1..16 images")
        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            if self.stop is not None and self.stop.is_set():
                raise RuntimeError("OCR run interrupted")
            conn = get_conn()
            delay = 0.0
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT rpm,ipm,requests,images,last_refill,
                               cooldown_until,multiplier,now()
                        FROM ocr_runtime.quota
                        WHERE quota_key=%s FOR UPDATE
                    """, (self.key,))
                    row = cur.fetchone()
                    if not row:
                        raise RuntimeError("OCR quota row missing; run page_stage init")
                    rpm,ipm,requests,images,last,cool,mult,now = row
                    if rpm != self.rpm or ipm != self.ipm:
                        raise RuntimeError("OCR quota caps changed during the run")
                    seconds = max(0.0,(now-last).total_seconds())
                    requests = min(max(1,rpm / 6),
                                   requests + seconds * rpm * mult / 60)
                    images = min(max(16,ipm / 6),
                                 images + seconds * ipm * mult / 60)
                    if cool and cool > now:
                        delay = (cool - now).total_seconds()
                    if requests < 1:
                        delay = max(delay, (1-requests)*60 / max(0.01,rpm*mult))
                    if images < image_count:
                        delay = max(
                            delay, (image_count-images)*60 / max(0.01,ipm*mult)
                        )
                    if delay <= 0:
                        cur.execute("""
                            UPDATE ocr_runtime.quota SET
                            requests=%s, images=%s, last_refill=%s
                            WHERE quota_key=%s
                        """, (requests-1,images-image_count,now,self.key))
                        conn.commit()
                    else:
                        # No token bucket row writes during a wait: avoid
                        # PostgreSQL WAL/lock contention under heavy load.
                        conn.rollback()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            if delay <= 0:
                return
            if time.monotonic() + delay > deadline:
                raise TimeoutError(
                    "OCR shared quota exhausted longer than configured wait budget"
                )
            time.sleep(min(2.0,delay) + random.random() * 0.04)

    def cooldown(self, seconds: float) -> None:
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ocr_runtime.quota SET
                    cooldown_until=GREATEST(
                      COALESCE(cooldown_until,now()),
                      now() + (%s * interval '1 second')
                    ),
                    multiplier=GREATEST(0.2,multiplier * 0.8)
                    WHERE quota_key=%s
                """, (min(120,max(1,seconds)),self.key))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def success(self) -> None:
        # Increase slowly after backpressure without a write per batch.
        # The optimistic UPDATE runs only when recovery is needed.
        conn=get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ocr_runtime.quota SET
                    multiplier=LEAST(1,multiplier + 0.01),
                    cooldown_until=NULL
                    WHERE quota_key=%s
                      AND multiplier < 1
                      AND (cooldown_until IS NULL OR cooldown_until < now())
                """, (self.key,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
