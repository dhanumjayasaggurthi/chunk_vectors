"""Shared OCR throttling tests. Only an explicitly disposable DB is mutated."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


class TestOcrIdentity(unittest.TestCase):
    def test_existing_key_json_resolves_the_quota_project(self):
        from ocr_quota import detect_project_id
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/"gcp_key.json"
            file.write_text(json.dumps({"project_id":"existing-vision-ocr-project"}))
            with patch.dict(os.environ,{"GOOGLE_APPLICATION_CREDENTIALS":str(file)}):
                self.assertEqual(detect_project_id(),"existing-vision-ocr-project")


@unittest.skipUnless(
    os.getenv("INGESTION_REUSE_TEST_DB")=="1"
    and os.getenv("PGDATABASE")=="embedding_test",
    "Requires a disposable pgvector testing database",
)
class TestSharedQuotaIntegration(unittest.TestCase):
    def test_two_clients_share_request_and_image_reservations(self):
        from ocr_quota import init_quota,PostgresOcrLimiter
        from db import get_conn
        project="ci-ocr-shared-limit"
        key=init_quota(project,rpm=300,ipm=600)
        self.assertEqual(key,hashlib.sha256(
            ("gcp-vision-document-text:"+project).encode()).hexdigest())
        clients=[
            PostgresOcrLimiter(key,rpm=300,ipm=600,max_wait_seconds=10)
            for _ in range(4)
        ]
        before=get_conn()
        with before.cursor() as cur:
            cur.execute("""SELECT requests,images FROM ocr_runtime.quota
                           WHERE quota_key=%s""",(key,))
            start_requests,start_images=cur.fetchone()
        before.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda x:x.acquire(8),clients))
        after=get_conn()
        with after.cursor() as cur:
            cur.execute("""SELECT requests,images FROM ocr_runtime.quota
                           WHERE quota_key=%s""",(key,))
            requests,images=cur.fetchone()
        after.close()
        # Allow refill over the small test interval, but never lose any
        # atomic reservation across concurrent client connections.
        self.assertLess(requests,start_requests-3.0)
        self.assertLess(images,start_images-29.0)
        with self.assertRaisesRegex(ValueError,"Conflicting OCR rate"):
            init_quota(project,rpm=400,ipm=600)

    def test_429_cooldown_is_visible_to_all_clients(self):
        from ocr_quota import init_quota,PostgresOcrLimiter
        from db import get_conn
        key=init_quota("ci-ocr-shared-cooldown",rpm=300,ipm=600)
        first=PostgresOcrLimiter(key,rpm=300,ipm=600)
        first.cooldown(3)
        conn=get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT multiplier, cooldown_until>now()
                           FROM ocr_runtime.quota WHERE quota_key=%s""",(key,))
            multiplier,active=cur.fetchone()
        conn.close()
        self.assertTrue(active)
        self.assertLess(multiplier,1.0)


if __name__=="__main__":
    unittest.main()
