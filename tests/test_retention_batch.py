import io
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta

from retention_batch import settings,iter_inventory,capture_one

CFG=json.loads((Path(__file__).resolve().parents[1]/"profiles/retention_batch_example.json").read_text())

class RetentionBatchTests(unittest.TestCase):
    def test_inventory_order_budget_and_expired_no_source_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            row={"path":str(root/"missing.pdf"),"source_sha256":"0"*64,
                 "purge_at_utc":(datetime.now(timezone.utc)-timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            cfg=settings(CFG)
            raw=(json.dumps(row)+"\n").encode()
            self.assertEqual(list(iter_inventory(io.BytesIO(raw),cfg)),[row])
            self.assertEqual(capture_one(row,{},None,{},cfg)["status"],"NOT_ADMITTED_DEADLINE")
            with self.assertRaisesRegex(ValueError,"strictly sorted"):
                list(iter_inventory(io.BytesIO(raw+raw),cfg))
            with self.assertRaisesRegex(ValueError,"limits"):
                list(iter_inventory(io.BytesIO(raw+raw),{**cfg,"max_documents":1}))
            invalid={**row,"purge_at_utc":"2026-11-24"}
            with self.assertRaisesRegex(ValueError,"UTC"):
                list(iter_inventory(io.BytesIO(json.dumps(invalid).encode()),cfg))
            with self.assertRaisesRegex(ValueError,"limits"):
                list(iter_inventory(io.BytesIO(b"x"*65537),cfg))
            with self.assertRaisesRegex(ValueError,"Duplicate"):
                list(iter_inventory(io.BytesIO(b'{"path":"a","path":"b"}'),cfg))
    def test_profile_limits(self):
        with self.assertRaises(ValueError):
            settings({**CFG,"workers":0})
        with self.assertRaises(ValueError):
            settings({**CFG,"authorized_receipt_storage":1})

if __name__=="__main__":
    unittest.main()
