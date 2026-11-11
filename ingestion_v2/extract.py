"""Opt-in page-range native PDF extraction with atomic local checkpoints.

No database writes, OCR API calls, or changes to legacy ingestion.  The
output is a bounded gzip/JSON artifact per range; failed ranges are
independently retryable.  Multiprocessing is used because PyMuPDF does not
support shared document handles across threads.

For multi-server use, the output directory must be on a correctly configured
shared volume; this standalone prototype does not yet provide cross-host
claims. Do not run two instances against the same document/output prefix.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .plan import ExtractionPolicy, page_ranges


def fingerprint_pdf(path):
    path=Path(path)
    if not path.is_file():
        raise ValueError("Source must be a readable local PDF")
    st=path.stat()
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(4*1024*1024),b""):
            h.update(block)
    return {"sha256":h.hexdigest(),"size":st.st_size,
            "mtime_ns":st.st_mtime_ns}


def pdf_page_count(path):
    import fitz
    with fitz.open(str(path)) as doc:
        if doc.needs_pass:
            raise ValueError("Password-protected PDF is not supported")
        return doc.page_count


def job_identity(source_hash, policy):
    canonical=json.dumps(asdict(policy),sort_keys=True,separators=(",",":"))
    return hashlib.sha256((source_hash+"\x00"+canonical).encode()).hexdigest()


def extract_range(path, start, end, expected_size, expected_mtime_ns,
                  native_text_min_chars):
    """Worker process opens its own PDF handle. Uses no GCP credentials."""
    import fitz
    before=Path(path).stat()
    if (before.st_size,before.st_mtime_ns)!=(expected_size,expected_mtime_ns):
        raise RuntimeError("Source version changed before range extraction")
    pages=[]
    with fitz.open(str(path)) as doc:
        if doc.needs_pass or start<0 or end>doc.page_count or start>=end:
            raise ValueError("Invalid PDF range or encrypted document")
        for page_index in range(start,end):
            page=doc[page_index]
            text_blocks=[]
            for idx, block in enumerate(page.get_text("blocks",sort=True)):
                # block type 0 = text; image bytes are NEVER retained.
                if len(block)<7 or block[6]!=0:
                    continue
                text=block[4]
                if not text or not text.strip():
                    continue
                text_blocks.append({
                    "element_id":f"{page_index+1}:text:{idx}",
                    "page":page_index+1,"kind":"paragraph",
                    "text":text,
                    "bbox":[round(float(v),3) for v in block[:4]],
                    "headings":[],
                })
            character_count=sum(len(e["text"].strip()) for e in text_blocks)
            # A native-text-poor page with graphics is an OCR candidate.
            # No vision API request occurs at this stage.
            has_images=bool(page.get_images(full=False)) if character_count<native_text_min_chars else False
            ocr_candidate=(character_count<native_text_min_chars and has_images)
            pages.append({
                "page":page_index+1,
                "width":float(page.rect.width),"height":float(page.rect.height),
                "rotation":int(page.rotation),
                "native_characters":character_count,
                "ocr_candidate":ocr_candidate,
                "text_blocks":text_blocks,
            })
    after=Path(path).stat()
    if (after.st_size,after.st_mtime_ns)!=(expected_size,expected_mtime_ns):
        raise RuntimeError("Source version changed during range extraction")
    return pages


def _artifact_path(directory, start, end):
    return Path(directory)/f"pages-{start+1:07d}-{end:07d}.json.gz"


def _write_json_gzip_atomic(path, payload):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,tmp=tempfile.mkstemp(prefix=".tmp-",suffix=".json.gz",dir=path.parent)
    try:
        with os.fdopen(descriptor,"wb") as raw:
            with gzip.GzipFile(filename="",mode="wb",fileobj=raw,mtime=0) as gz:
                gz.write(json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _valid_checkpoint(path,identity,start,end):
    if not path.exists():
        return False
    try:
        with gzip.open(path,"rt",encoding="utf-8") as f:
            data=json.load(f)
        return (data.get("job_key")==identity and data.get("range_start")==start
                and data.get("range_end")==end
                and len(data.get("pages",[]))==end-start
                and [p["page"] for p in data["pages"]]==list(range(start+1,end+1)))
    except (OSError,KeyError,ValueError,TypeError):
        return False


def _work_range(path,start,end,stat,policy,identity,folder):
    target=_artifact_path(folder,start,end)
    try:
        if _valid_checkpoint(target,identity,start,end):
            return {"start":start,"end":end,"status":"SKIPPED","path":str(target)}
        pages=extract_range(path,start,end,stat["size"],stat["mtime_ns"],
                            policy.native_text_min_chars)
        _write_json_gzip_atomic(target,{
            "schema_version":1,"job_key":identity,"source_sha256":stat["sha256"],
            "range_start":start,"range_end":end,"pages":pages,
        })
        return {"start":start,"end":end,"status":"SUCCESS","path":str(target),
                "ocr_candidates":sum(bool(p["ocr_candidate"]) for p in pages)}
    except Exception as exc:
        # A failing range does not abort neighboring work. A later rerun
        # retries it. Do not leak entire source text into error records.
        return {"start":start,"end":end,"status":"FAILED",
                "error_code":type(exc).__name__,"message":str(exc)[:400]}


def run_local(path,output_dir,policy=None,max_ranges=None):
    """Bounded in-flight tasks; restart skips verified complete checkpoints."""
    policy=policy or ExtractionPolicy()
    source=fingerprint_pdf(path)
    count=pdf_page_count(path)
    identity=job_identity(source["sha256"],policy)
    ranges=page_ranges(count,policy.range_pages)
    if max_ranges is not None:
        if max_ranges<1:
            raise ValueError("max_ranges must be positive")
        ranges=ranges[:max_ranges]
    directory=Path(output_dir)/identity
    directory.mkdir(parents=True,exist_ok=True)
    manifest={
        "job_key":identity,"source_sha256":source["sha256"],
        "page_count":count,"range_pages":policy.range_pages,
        "selected_ranges":len(ranges),"policy":asdict(policy),
    }
    _write_json_gzip_atomic(directory/"manifest.json.gz",manifest)
    result={"job_key":identity,"page_count":count,"selected_ranges":len(ranges),
            "SUCCESS":0,"SKIPPED":0,"FAILED":0,"ocr_candidates":0,
            "failure_details":[],"output_dir":str(directory)}
    iterator=iter(ranges)
    with concurrent.futures.ProcessPoolExecutor(max_workers=policy.cpu_processes) as pool:
        outstanding={}
        def submit():
            try:
                start,end=next(iterator)
            except StopIteration:
                return False
            future=pool.submit(_work_range,str(path),start,end,source,policy,identity,str(directory))
            outstanding[future]=(start,end)
            return True
        for _ in range(min(len(ranges),max(1,policy.cpu_processes*2))):
            submit()
        while outstanding:
            finished,_=concurrent.futures.wait(
                outstanding,return_when=concurrent.futures.FIRST_COMPLETED)
            for future in finished:
                start,end=outstanding.pop(future)
                try:
                    item=future.result()
                except Exception as exc:
                    item={"status":"FAILED","start":start,"end":end,
                          "error_code":type(exc).__name__,"message":str(exc)[:400]}
                result[item["status"]]+=1
                result["ocr_candidates"]+=item.get("ocr_candidates",0)
                if item["status"]=="FAILED":
                    result["failure_details"].append(item)
                submit()
    return result
