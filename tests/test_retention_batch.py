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


class ResourceGuardTests(unittest.TestCase):
    def test_adaptive_admission_hysteresis(self):
        from resource_guard import ResourceGuard
        cfg=json.loads((Path(__file__).resolve().parents[1]/"profiles/retention_batch_linux_8core_30gb.example.json").read_text())
        g=1024**3
        now=[0]
        sample=[50,12*g,12*g]
        guard=ResourceGuard(cfg["resource_guard"],cfg["workers"],
                            metrics=lambda:tuple(sample),clock=lambda:now[0])
        self.assertEqual(guard.poll(),4)
        now[0]=30
        self.assertEqual(guard.poll(),5)
        now[0]=60
        self.assertEqual(guard.poll(),6)
        sample[0]=96
        now[0]=65
        self.assertEqual(guard.poll(),5)
        # Poll faster than interval cannot repeatedly lower the limit.
        self.assertEqual(guard.poll(),5)
        sample[1]=4*g
        now[0]=70
        self.assertEqual(guard.poll(),0)
        sample[:]=[40,7*g,16*g]
        now[0]=100
        self.assertEqual(guard.poll(),0)
        sample[1]=9*g
        now[0]=105
        self.assertEqual(guard.poll(),0)
        now[0]=135
        self.assertEqual(guard.poll(),1)
        now[0]=165
        self.assertEqual(guard.poll(),2)
        sample[2]=21*g
        now[0]=170
        self.assertEqual(guard.poll(),0)
        self.assertEqual(guard.report()["memory_pauses"],2)
        self.assertEqual(guard.report()["cpu_reductions"],1)

    def test_guard_config_validation_and_legacy(self):
        cfg=json.loads((Path(__file__).resolve().parents[1]/"profiles/retention_batch_linux_8core_30gb.example.json").read_text())
        self.assertEqual(settings(cfg)["workers"],6)
        self.assertEqual(settings(CFG),CFG)
        with self.assertRaises(ValueError):
            settings({**cfg,"workers":2})
        with self.assertRaises(ValueError):
            settings({**cfg,"resource_guard":{**cfg["resource_guard"],"host_resume_available_gib":5}})

    def test_metadata_example_loads_and_extracts_real_values(self):
        from processing_profile import load_profile
        from metadata_extractor import _extract
        p=load_profile(str(Path(__file__).resolve().parents[1]/"profiles/regulatory_metadata_api.example.yml"))
        rules=p["metadata"]["document"]
        self.assertEqual(_extract("Report Date: 2026-11-24",rules["report_date"]),"2026-11-24")
        self.assertEqual(_extract("Title: Clinical Safety Report",rules["document_title"]),"Clinical Safety Report")
        self.assertEqual(_extract("Application Number: 123/45",rules["application_number"]),"123/45")
        self.assertIsNone(_extract("No protocol label present",rules["protocol_number"]))
        self.assertEqual(p["output"]["table"],"regulatory_chunks")
        self.assertFalse(p["embedding"]["enabled"])

    @unittest.skipUnless(__import__("sys").platform.startswith("linux"),"Linux metrics")
    def test_live_linux_metrics(self):
        from resource_guard import LinuxMetrics
        sample=LinuxMetrics()
        _,available,group=sample()
        self.assertGreater(available,0)
        self.assertTrue(group is None or group>=0)


if __name__=="__main__":
    unittest.main()
