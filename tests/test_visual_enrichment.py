"""No credentials, provider traffic or production data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock

from visual_evidence import capture,_paths
from visual_enrichment import prepare_page,messages,validate_response,call_prepared


def configuration():
    return json.loads((Path(__file__).resolve().parents[1]/"profiles"/
                       "visual_enrichment_example.json").read_text(encoding="utf-8"))


class VisualEnrichmentTests(unittest.TestCase):
    def test_retained_page_contract_and_mock_provider(self):
        import fitz
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"source.pdf"
            with fitz.open() as doc:
                p=doc.new_page(width=300,height=400)
                p.draw_rect((40,60,200,150))
                p.insert_text((40,180),"Figure 1: Dose 20 mg")
                p=doc.new_page(width=300,height=400)
                p.draw_rect((40,60,200,150))
                doc.save(source)
            vs={"dpi":150,"capture_figures":True}
            cap=capture(source,root/"retained",1,2,settings=vs,policy_id="test",
                        authorized_page_derivatives=True)
            cid=cap["capture_id"]
            source.unlink()
            cfg=configuration()
            pkt=prepare_page(root/"retained",cid,1,visual_settings=vs,config=cfg)
            candidate=pkt["context"]["candidates"][0]["id"]
            response={"observations":[{"target":candidate,"observation":"A plot is visible",
                       "caption_quote":"Dose 20 mg","uncertainty":"limited"}],
                      "limitations":["Axes require review"]}
            raw=json.dumps(response)
            result=validate_response(raw,pkt,cfg)
            self.assertTrue(result["requires_review"])
            self.assertFalse(result["source_facts_verified"])
            self.assertEqual(len(messages(pkt,cfg)[1]["content"]),2)
            empty=prepare_page(root/"retained",cid,2,visual_settings=vs,config=cfg)
            self.assertEqual(empty["context"]["candidates"],[])
            self.assertEqual(validate_response(json.dumps({"observations":[{
                "target":"page","observation":"A rectangle is visible","caption_quote":"",
                "uncertainty":"limited"}],"limitations":[]}),empty,cfg)["status"],
                "MODEL_OBSERVATIONS_UNVERIFIED")
            for changes in ({"target":"unknown"},{"caption_quote":"invented"},
                            {"observation":"Dose 30 mg"}):
                bad={**response,"observations":[{**response["observations"][0],**changes}]}
                with self.assertRaises(ValueError):
                    validate_response(json.dumps(bad),pkt,cfg)
            with self.assertRaises(ValueError):
                validate_response('{"observations":[],"observations":[],"limitations":[]}',pkt,cfg)
            with self.assertRaises(PermissionError):
                call_prepared(pkt,cfg,reserve=Mock())
            approved={**cfg,"authorized_provider":True}
            reserve=Mock()
            with patch("azure_client.call_visual_structured",return_value=(raw,{"total_tokens":12})) as api:
                self.assertEqual(call_prepared(pkt,approved,reserve=reserve)["usage"]["total_tokens"],12)
                reserve.assert_called_once()
                api.assert_called_once()
            tuned={**cfg,"timeout_seconds":60}
            self.assertEqual(prepare_page(root/"retained",cid,1,visual_settings=vs,config=tuned)["request_id"],
                             pkt["request_id"])
            changed={**cfg,"prompt":"Different task"}
            self.assertNotEqual(prepare_page(root/"retained",cid,1,visual_settings=vs,config=changed)["request_id"],
                                pkt["request_id"])
            png,_=_paths(root/"retained",cid,1)
            with png.open("ab") as stream:
                stream.write(b"corrupt")
            with self.assertRaisesRegex(ValueError,"checksum"):
                prepare_page(root/"retained",cid,1,visual_settings=vs,config=cfg)

    def test_single_page_probe_is_read_only_then_cached_and_bounded(self):
        import fitz
        import requests
        from visual_enrichment import probe_page
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"source.pdf"
            with fitz.open() as doc:
                doc.new_page(width=100,height=100).draw_rect((10,10,80,80))
                doc.save(source)
            vs={"dpi":150}
            retained=root/"retained"
            cap=capture(source,retained,1,1,settings=vs,policy_id="test",
                        authorized_page_derivatives=True)
            source.unlink()
            cfg={**configuration(),"authorized_provider":True}
            execution=json.loads((Path(__file__).resolve().parents[1]/"profiles"/
                "visual_enrichment_execution_example.json").read_text(encoding="utf-8"))
            execution.update(authorized_result_storage=True,rpm=60000,tpm=10000000)
            kw=dict(visual_settings=vs,config=cfg,execution=execution)
            raw=json.dumps({"observations":[{"target":"page","observation":"A rectangle is visible",
                        "caption_quote":"","uncertainty":"limited"}],"limitations":[]})
            response=requests.Response()
            response.status_code=429
            with patch("azure_client.call_visual_structured",side_effect=[requests.HTTPError(response=response),(raw,{})]) as api:
                preview=probe_page(retained,cap["capture_id"],1,**kw)
                self.assertEqual(preview["status"],"READY")
                self.assertEqual(preview["cloud_attempts"],0)
                api.assert_not_called()
                fail=probe_page(retained,cap["capture_id"],1,apply=True,**kw)
                self.assertEqual(fail["error_code"],"HTTP_429")
                self.assertEqual(api.call_count,1)
                good=probe_page(retained,cap["capture_id"],1,apply=True,**kw)
                self.assertEqual(good["status"],"COMPLETED_UNVERIFIED")
                self.assertEqual(api.call_count,2)
                replay=probe_page(retained,cap["capture_id"],1,apply=True,**kw)
                self.assertEqual(replay["status"],"SKIPPED_VERIFIED")
                self.assertEqual(api.call_count,2)
                self.assertFalse(replay["writes"])

    def test_bounded_run_reuse_retry_and_corrupt_cache(self):
        import fitz
        import requests
        from visual_enrichment import run_pages,prepare_page,_cache_path,_local_run_lock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/"source.pdf"
            with fitz.open() as doc:
                for _ in range(2):
                    p=doc.new_page(width=100,height=100)
                    p.draw_rect((10,10,80,80))
                doc.save(source)
            vs={"dpi":150}
            retained=root/"retained"
            cap=capture(source,retained,1,2,settings=vs,policy_id="test",
                        authorized_page_derivatives=True)
            cid=cap["capture_id"]
            source.unlink()
            cfg={**configuration(),"authorized_provider":True}
            exec_cfg=json.loads((Path(__file__).resolve().parents[1]/"profiles"/
                       "visual_enrichment_execution_example.json").read_text(encoding="utf-8"))
            exec_cfg.update(workers=2,read_workers=2,rpm=60000,tpm=10000000,
                            authorized_result_storage=True,max_pages=3)
            raw=json.dumps({"observations":[{"target":"page","observation":"A shape is visible",
                       "caption_quote":"","uncertainty":"limited"}],"limitations":[]})
            # The first request throttles, then succeeds. Duplicate page inputs
            # share a cache lock; there must be only two successful API calls.
            throttled=requests.Response()
            throttled.status_code=429
            throttled.headers["retry-after-ms"]="1"
            error=requests.HTTPError(response=throttled)
            pages=[(cid,1),(cid,1),(cid,2)]
            with patch("azure_client.call_visual_structured",
                       side_effect=[error,(raw,{}),(raw,{})]) as api:
                outcomes=list(run_pages(retained,pages,visual_settings=vs,config=cfg,execution=exec_cfg))
                self.assertEqual(sorted(x["status"] for x in outcomes),
                                 ["COMPLETED_UNVERIFIED","COMPLETED_UNVERIFIED","SKIPPED_VERIFIED"])
                self.assertEqual(api.call_count,3)
                replay=list(run_pages(retained,pages,visual_settings=vs,config=cfg,execution=exec_cfg))
                self.assertTrue(all(x["status"]=="SKIPPED_VERIFIED" for x in replay))
                self.assertEqual(api.call_count,3)
                # Standalone cap remains unchanged. Full document selection
                # reuses one reader/network pool for every selected page.
                limited={**exec_cfg,"max_pages":1}
                self.assertEqual(len(list(run_pages(retained,pages,visual_settings=vs,
                                      config=cfg,execution=limited))),1)
                import concurrent.futures
                real_pool=concurrent.futures.ProcessPoolExecutor
                with patch("visual_enrichment.concurrent.futures.ProcessPoolExecutor",
                           wraps=real_pool) as pool:
                    full=list(run_pages(retained,pages,visual_settings=vs,
                              config=cfg,execution=limited,document_pages=len(pages)))
                    self.assertEqual(len(full),3)
                    pool.assert_called_once()
                self.assertTrue(all(x["status"]=="SKIPPED_VERIFIED" for x in full))
                self.assertEqual(api.call_count,3)
                pkt=prepare_page(retained,cid,1,visual_settings=vs,config=cfg)
                path=_cache_path(retained,pkt)
                row=json.loads(path.read_text(encoding="utf-8"))
                row["result"]["result"]["observations"][0]["observation"]="fabricated"
                path.write_text(json.dumps(row),encoding="utf-8")
                bad=list(run_pages(retained,[(cid,1)],visual_settings=vs,config=cfg,execution=exec_cfg))
                self.assertEqual(bad[0]["status"],"FAILED")
                self.assertEqual(api.call_count,3)
            with _local_run_lock(retained):
                with self.assertRaises(RuntimeError):
                    list(run_pages(retained,[(cid,1)],visual_settings=vs,config=cfg,execution=exec_cfg))
            with self.assertRaises(PermissionError):
                list(run_pages(retained,[(cid,1)],visual_settings=vs,config=cfg,
                               execution={**exec_cfg,"authorized_result_storage":False}))

    def test_safe_failure_codes_do_not_expose_provider_content(self):
        import requests
        from visual_enrichment import safe_failure_code
        response=requests.Response()
        response.status_code=401
        self.assertEqual(safe_failure_code(requests.HTTPError("private body",response=response)),"HTTP_401")
        self.assertEqual(safe_failure_code(ValueError("private text")),"ValueError")
        self.assertEqual(safe_failure_code(ValueError("Numeric observation prohibited; retain source quote separately")),
                         "VISUAL_NUMERIC_OBSERVATION")

    def test_retry_limits_and_quota_budget(self):
        import requests
        from visual_enrichment import LocalQuota,_retry_delay
        cfg={"rpm":60,"tpm":100,"estimated_input_tokens_per_page":80,
             "max_request_bytes":1024,"quota_wait_seconds":1,
             "retry_base_seconds":1,"max_retry_delay_seconds":10}
        quota=LocalQuota(cfg)
        with self.assertRaisesRegex(ValueError,"byte budget"):
            quota.reserve("x",1025,10)
        quota.reserve("x",1024,10)
        with self.assertRaises(TimeoutError):
            quota.reserve("y",1024,10)
        response=requests.Response()
        response.status_code=429
        response.headers["retry-after-ms"]="20000"
        self.assertIsNone(_retry_delay(requests.HTTPError(response=response),1,cfg))
        response.headers["retry-after-ms"]="250"
        self.assertEqual(_retry_delay(requests.HTTPError(response=response),1,cfg),.25)
        response.status_code=401
        self.assertIsNone(_retry_delay(requests.HTTPError(response=response),1,cfg))
        self.assertIsNone(_retry_delay(ValueError("invalid model output"),1,cfg))

    def test_adapter_payload_and_truncated_response(self):
        from azure_client import call_visual_structured
        from visual_enrichment import RESPONSE_SCHEMA
        c={"deployment":"d","model":"m","api_base":"https://example.invalid",
           "api_version":"test","api_key":"not-real"}
        response=Mock()
        response.json.return_value={"choices":[{"finish_reason":"stop",
                            "message":{"content":"{}"}}],"usage":{}}
        kw=dict(section="test",deployment="d",model="m",token_parameter="max_completion_tokens",
                max_output_tokens=100,temperature=None,timeout_seconds=20)
        with patch("azure_client._load",return_value=c),patch("azure_client.requests.post",return_value=response) as post:
            call_visual_structured([],RESPONSE_SCHEMA,**kw)
            body=post.call_args.kwargs["json"]
            self.assertEqual(body["max_completion_tokens"],100)
            self.assertNotIn("temperature",body)
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            response.json.return_value["choices"][0]["finish_reason"]="length"
            with self.assertRaisesRegex(ValueError,"Incomplete"):
                call_visual_structured([],RESPONSE_SCHEMA,**kw)
            with self.assertRaisesRegex(ValueError,"differs"):
                call_visual_structured([],RESPONSE_SCHEMA,**{**kw,"model":"different"})


if __name__=="__main__":
    unittest.main()
