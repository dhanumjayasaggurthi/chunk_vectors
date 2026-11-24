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
