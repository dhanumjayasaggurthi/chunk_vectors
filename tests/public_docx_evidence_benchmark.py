"""Official public DOCX acceptance; no original persisted after temporary test."""
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import hashlib
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docx_evidence import capture,verify,read_evidence,W,STORY

def check(item,cfg,download):
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        source=root/"public.docx"
        with requests.get(item["url"],stream=True,timeout=(download["connect_timeout"],download["read_timeout"])) as response:
            response.raise_for_status()
            size=0
            with source.open("wb") as out:
                for data in response.iter_content(65536):
                    size+=len(data)
                    if size>download["max_download_bytes"]:
                        raise ValueError("Public fixture exceeds download budget")
                    out.write(data)
        expected={}
        asset_sha={}
        with zipfile.ZipFile(source) as z:
            for part in z.namelist():
                if STORY.fullmatch(part):
                    xml=ET.fromstring(z.read(part))
                    for tag in ("p","tbl","tr","tc","t","gridSpan","vMerge"):
                        key="w:"+tag
                        expected[key]=expected.get(key,0)+sum(1 for _ in xml.iter(f"{{{W}}}{tag}"))
                elif part.startswith("word/media/") and not part.endswith("/"):
                    asset_sha[part]=hashlib.sha256(z.read(part)).hexdigest()
        kw=dict(policy_id="public-benchmark",authorized_derivatives=True,config=cfg)
        out=root/"retained"
        cold=capture(source,out,**kw)
        cid=cold["capture_id"]
        assert expected["w:p"]>0 and expected["w:tbl"]>0, expected
        for k,count in expected.items():
            assert cold["counts"].get(k,0)==count,(k,count,cold["counts"].get(k))
        assert capture(source,out,**kw)["skipped_verified"]
        source.unlink()
        post=verify(out,cid,config=cfg)
        data=read_evidence(out,cid,config=cfg)
        for asset in data["assets"]:
            assert asset_sha[asset["part"]]==asset["sha256"]
        assert not list(out.rglob("*.docx"))
        assert not list(out.rglob("*.xml"))
        return {"source":item["name"],"source_bytes":size,"counts":expected,
                "asset_parts":post["asset_parts"],"unique_assets":post["unique_assets"],
                "source_media_parts":len(asset_sha),"chart_parts":post["chart_parts"],
                "story_parts":post["story_parts"],"unresolved_count":len(post["unresolved"]),
                "unrecognized_or_wrapper_tag_count":len(data["unrecognized_or_wrapper_tags"]),
                "capture_seconds":cold["elapsed_seconds"],"verified_after_source_removal":True}

def main():
    config=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cfg=json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    reports=[]
    for item in config["sources"]:
        report=check(item,cfg,config)
        print(json.dumps(report),flush=True)
        reports.append(report)
    Path(sys.argv[3]).write_text(json.dumps(reports,indent=2),encoding="utf-8")

if __name__=="__main__":
    main()
