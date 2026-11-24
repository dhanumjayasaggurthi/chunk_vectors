"""Opt-in bounded, resumable PDF extraction into a NEW PostgreSQL page table.

This module reuses the main-branch PDF layout, Vision OCR, and table extraction
components. It never calls embeddings, changes the production source table,
or touches embedding_runtime. Run 'init --apply' only in an approved staging DB.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from config import OCR_RENDER_DPI
from db import get_conn
from profile_store import _safe_identifier, profile_document_lock

LOG = logging.getLogger("epod.page_stage")


class VisualReviewRequired(RuntimeError):
    """The full document is checkpointed but must not be published yet."""

    def __init__(self, review_pages: int):
        super().__init__(f"{review_pages} pages contain unreviewed vector regions")
        self.review_pages = review_pages



def names(profile):
    opts = profile.get("extraction") or {}
    if opts.get("enabled") is not True:
        raise ValueError("Extraction requires extraction.enabled: true")
    schema, table = opts["schema"], opts["table"]
    if len(table) > 45:
        raise ValueError("Stage table name too long for companion tables")
    if profile.get("extracted_content", {}).get("table") == table and (
        profile.get("extracted_content", {}).get("schema") == schema
    ):
        pass  # The published page table IS the extracted_source for rechunking.
    q = lambda name: _safe_identifier(schema) + "." + _safe_identifier(name)
    return dict(
        published=q(table), work=q(table + "_work"),
        docs=q(table + "_docs"), errors=q(table + "_errors"),
        quota_schema="ocr_runtime",
        schema=_safe_identifier(schema), table=table,
    )


def settings(profile):
    opts = profile.get("extraction") or {}
    names(profile)
    result = {
        "page_batch_size":32, "ocr_batch_images":8, "ocr_workers":4,
        "ocr_requests_rpm":1500, "ocr_images_ipm":1500,
        "ocr_max_batch_bytes":6000000, "ocr_max_retries":4,
        "ocr_timeout_seconds":90, "ocr_bbox_detail":"word",
        "ocr_confidence_min":0.60, "max_quota_wait_seconds":600,
        "table_summaries_enabled":False, "chart_descriptions_enabled":True,
        "chart_workers":4,
        "max_window_raster_bytes":160_000_000,
        "max_render_pixels":50_000_000,
        "ocr_enabled":True,
        "capture_rich_text":False,
        "publish_unreviewed_vector_regions":False,
        "approved_image_manifest":"",
        "capture_table_structure":False,
        "max_table_source_json_bytes":8_000_000,
        "visual_processing":"blocking",
        "max_visual_regions_per_page":256,
        "worker_backend":"threads",
        "worker_recycle_documents":0,
        "profile_stage_timings":False,
        "empty_table_policy":"fail",
    }
    result.update(opts)
    return result


def extraction_fingerprint(profile) -> str:
    """Hash only content/accuracy-affecting extraction settings.

    Runtime concurrency, page batch size, request and image RPM, timeouts,
    memory guardrails, or downstream chunk/embedding settings MUST NOT
    force re-OCR of already successful pages.
    """
    from config import (
        OCR_RENDER_DPI, SCANNED_PAGE_TEXT_THRESHOLD,
        TABLE_SETTINGS, TABLE_FORMAT,
    )
    cfg=settings(profile)
    semantic={
        "stage_format_version":4 if cfg["capture_rich_text"] else 3,
        "unclassified_vector_preview_version":1,
        "capture_rich_text":cfg["capture_rich_text"],
        "capture_table_structure":cfg["capture_table_structure"],
        "source_table_format_version":1 if cfg["capture_table_structure"] else 0,
        "ocr_enabled":cfg["ocr_enabled"],
        "ocr_bbox_detail":cfg["ocr_bbox_detail"],
        "ocr_confidence_min":cfg["ocr_confidence_min"],
        "chart_descriptions_enabled":cfg["chart_descriptions_enabled"],
        "table_summaries_enabled":cfg["table_summaries_enabled"],
        "render_dpi":OCR_RENDER_DPI,
        "scanned_threshold":SCANNED_PAGE_TEXT_THRESHOLD,
        "table_settings":TABLE_SETTINGS,
        "table_format":TABLE_FORMAT,
    }
    # Deferred visual preservation changes page content; version it independently.
    if cfg["empty_table_policy"]!="fail":
        semantic["empty_table_policy"]=cfg["empty_table_policy"]
        semantic["unresolved_source_region_format"]=1
    if cfg["visual_processing"] != "blocking":
        semantic["visual_processing"]=cfg["visual_processing"]
        semantic["visual_source_reference_format"]=1
    # The approved-asset manifest changes extraction meaning and must never
    # reuse checkpoints produced under an older approval policy.
    if cfg["approved_image_manifest"]:
        from approved_images import read_approved_image_manifest
        _,manifest_hash=read_approved_image_manifest(cfg["approved_image_manifest"])
        semantic["approved_image_manifest_sha256"]=manifest_hash
    payload=json.dumps(semantic,sort_keys=True,separators=(",",":"),
                       allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_hash(path: str) -> str:
    digest=hashlib.sha256()
    with open(path,"rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024),b""):
            digest.update(block)
    return digest.hexdigest()


def init(profile, *, apply=False):
    n=names(profile)
    cfg=settings(profile)
    if not apply:
        return {"writes":False,"published":n["published"],
                "work":n["work"],"documents":n["docs"],
                "errors":n["errors"]}
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {n['schema']}")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {n['work']} (
                    doc_id text NOT NULL, source_hash text NOT NULL,
                    extraction_hash text NOT NULL, page_number integer NOT NULL,
                    page_end integer NOT NULL,
                    page_width double precision NOT NULL,
                    page_height double precision NOT NULL,
                    page_rotation integer NOT NULL,
                    chunk_text text NOT NULL,
                    elements jsonb NOT NULL, section_title text,
                    file_path text NOT NULL, file_name text NOT NULL,
                    content_sha256 text NOT NULL,
                    PRIMARY KEY (doc_id,source_hash,extraction_hash,page_number)
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {n['published']} (
                    doc_id text NOT NULL, page_number integer NOT NULL,
                    page_end integer NOT NULL,
                    page_width double precision NOT NULL,
                    page_height double precision NOT NULL,
                    page_rotation integer NOT NULL,
                    chunk_text text NOT NULL,
                    elements jsonb NOT NULL, section_title text,
                    file_path text NOT NULL, file_name text NOT NULL,
                    content_sha256 text NOT NULL, source_hash text NOT NULL,
                    extraction_hash text NOT NULL,
                    PRIMARY KEY (doc_id,page_number)
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {n['docs']} (
                    doc_id text PRIMARY KEY, source_hash text NOT NULL,
                    extraction_hash text NOT NULL, page_count integer NOT NULL,
                    completed_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {n['errors']} (
                    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    doc_id text NOT NULL, page_number integer,
                    source_hash text NOT NULL,
                    extraction_hash text NOT NULL,
                    error_code text NOT NULL, error_message text NOT NULL,
                    occurred_at timestamptz NOT NULL DEFAULT now()
                )
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # Rate quota is shared across all machines using this database/project.
    if cfg["ocr_enabled"]:
        from ocr_quota import init_quota, detect_project_id
        init_quota(
            detect_project_id(), rpm=cfg["ocr_requests_rpm"],
            ipm=cfg["ocr_images_ipm"],
        )
    return {"created_or_verified":n["published"],"source_unchanged":True}


def require_initialized(profile):
    n=names(profile)
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            for key in ("work","published","docs","errors"):
                cur.execute("SELECT to_regclass(%s)",(n[key],))
                if cur.fetchone()[0] is None:
                    raise ValueError(
                        "Stage tables not initialized; run init --apply in staging"
                    )
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
            """,(profile["extraction"]["schema"],profile["extraction"]["table"]))
            columns={row[0] for row in cur.fetchall()}
            required={
                "doc_id","page_number","page_end","page_width",
                "page_height","page_rotation","chunk_text","elements",
                "file_path","file_name","source_hash","extraction_hash",
            }
            if not required.issubset(columns):
                raise ValueError(
                    "Stage output schema is outdated; use a new, versioned "
                    "extraction.table for accurate page geometry"
                )
    finally:
        conn.close()
    return n


def record_document_failure(path, profile, n, error):
    """Persist failures before page processing or after a source-change check."""
    doc_id=hashlib.sha256(
        str(Path(path).resolve()).encode("utf-8")
    ).hexdigest()
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {n['errors']} (
                    doc_id,page_number,source_hash,extraction_hash,
                    error_code,error_message
                ) VALUES (%s,NULL,%s,%s,%s,%s)
            """,(
                doc_id,"unavailable",extraction_fingerprint(profile),
                type(error).__name__[:80],str(error)[:1000],
            ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_completed(doc_id, source, fingerprint, n):
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT page_number FROM {n['work']}
                WHERE doc_id=%s AND source_hash=%s AND extraction_hash=%s
            """, (doc_id, source, fingerprint))
            done={int(row[0]) for row in cur.fetchall()}
            cur.execute(f"""
                SELECT source_hash, extraction_hash, page_count
                FROM {n['docs']} WHERE doc_id=%s
            """, (doc_id,))
            published=cur.fetchone()
        return done,published
    finally:
        conn.close()


def _toc_title(toc, page_number):
    title=""
    for entry in sorted(toc,key=lambda item:item.page_number):
        if entry.page_number > page_number:
            break
        title=entry.title
    return title


def _page_row(page, struct, source, extraction_hash):
    from chunker import _page_full_text
    text=_page_full_text(page)
    if not page.is_blank and not text.strip():
        # An image-only page may be indexed later by OCR. Retain its
        # original source references; never invent a textual transcription.
        if not any(
            getattr(el,"visual_asset",None) or getattr(el,"unresolved_region",None)
            for el in page.elements
        ):
            raise ValueError("Nonblank page has no recoverable text or source visual evidence")
    geometry=[]
    for element in page.elements:
        box=element.bbox
        entry={
            "type":getattr(element.element_type,"value",str(element.element_type)),
            "bbox":[box.x0,box.y0,box.x1,box.y1],
            "text":element.text, "confidence":round(float(element.confidence),5),
            "reading_order":element.reading_order,
        }
        if getattr(element, "visual_asset", None):
            if entry["type"] not in ("image","chart") or element.image_bytes:
                raise ValueError("Invalid preserved visual source reference")
            from pdf_processor import validate_visual_asset
            entry["visual_asset"]=validate_visual_asset(
                element.visual_asset, page_number=page.page_number,
            )
        if getattr(element, "unresolved_region", None):
            from pdf_processor import validate_unresolved_region
            if entry["type"]!="vector_region" or element.vector_png:
                raise ValueError("Invalid unresolved table/graphic region")
            entry["unresolved_region"]=validate_unresolved_region(
                element.unresolved_region,page_number=page.page_number
            )
        if getattr(element, "source_table", None) is not None:
            from table_extractor import validate_source_table
            if entry["type"]!="table":
                raise ValueError("Source table attached to non-table element")
            entry["source_table"]=validate_source_table(element.source_table)
        if getattr(element, "approved_asset", None):
            metadata=element.approved_asset
            if (not isinstance(metadata,dict)
                    or metadata.get("approval_status")!="approved"
                    or metadata.get("text") != element.text
                    or element.image_bytes):
                raise ValueError("Approved asset metadata or OCR policy invalid")
            entry["approved_asset"]=metadata
        if getattr(element, "vector_png", b""):
            import base64
            png = element.vector_png
            if not 0 < len(png) <= 128_000 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Invalid bounded vector preview PNG")
            entry["vector_preview"]={
                "format":"png", "dpi":144,
                "sha256":hashlib.sha256(png).hexdigest(),
                "base64":base64.b64encode(png).decode("ascii"),
                "review_required":True,
            }
        if cfg_rich := getattr(element, "runs", None):
            from rich_document import validate_runs
            entry["runs"]=validate_runs(cfg_rich)
            if getattr(element,"semantic_role",""):
                entry["semantic_role"]=element.semantic_role
                entry["heading_level"]=element.heading_level
        if getattr(element, "language", ""):
            entry["language"]=element.language
        if getattr(element,"ocr_regions",None):
            entry["ocr_regions"]=element.ocr_regions
            entry["ocr_render_dpi"]=OCR_RENDER_DPI
        geometry.append(entry)
    payload=json.dumps(geometry,ensure_ascii=False,separators=(",",":"),
                       allow_nan=False)
    return (
        struct.doc_id,source,extraction_hash,
        page.page_number,page.page_number,
        float(page.width),float(page.height),int(getattr(page,"rotation",0)),
        text,payload,
        _toc_title(struct.toc,page.page_number),
        struct.file_path,struct.file_name,
        hashlib.sha256((text+payload).encode("utf-8")).hexdigest(),
    )


def _persist_window(rows, errors, doc_id, source, fingerprint, n):
    """One commit per bounded page window, preserving successfully processed pages."""
    from psycopg2.extras import execute_values
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            if rows:
                execute_values(cur,f"""
                    INSERT INTO {n['work']} (
                        doc_id,source_hash,extraction_hash,page_number,
                        page_end,page_width,page_height,page_rotation,
                        chunk_text,elements,section_title,file_path,file_name,
                        content_sha256
                    ) VALUES %s
                    ON CONFLICT (doc_id,source_hash,extraction_hash,page_number)
                    DO UPDATE SET page_end=EXCLUDED.page_end,
                        page_width=EXCLUDED.page_width,
                        page_height=EXCLUDED.page_height,
                        page_rotation=EXCLUDED.page_rotation,
                        chunk_text=EXCLUDED.chunk_text,
                        elements=EXCLUDED.elements,
                        section_title=EXCLUDED.section_title,
                        file_path=EXCLUDED.file_path,
                        file_name=EXCLUDED.file_name,
                        content_sha256=EXCLUDED.content_sha256
                """,rows,
                    template="(" + ",".join(["%s"]*9 +
                        ["%s::jsonb"] + ["%s"]*4) + ")",
                    page_size=100)
            if errors:
                execute_values(cur,f"""
                    INSERT INTO {n['errors']} (
                        doc_id,page_number,source_hash,extraction_hash,
                        error_code,error_message
                    ) VALUES %s
                """,[
                    (doc_id,pn,source,fingerprint,
                     type(exc).__name__,str(exc)[:1000])
                    for pn,exc in errors.items()
                ],page_size=100)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _publish(doc_id, source, fingerprint, page_count, n, *, allow_unreviewed=False):
    """Atomically swap ONLY complete document pages into the visible table."""
    conn=get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*), MIN(page_number), MAX(page_number)
                FROM {n['work']}
                WHERE doc_id=%s AND source_hash=%s AND extraction_hash=%s
            """, (doc_id,source,fingerprint))
            count, first, last=cur.fetchone()
            if (count, first, last)!=(page_count,1,page_count):
                raise RuntimeError(
                    f"Cannot publish incomplete document: {count}/{page_count} pages"
                )
            # JSONB containment is exact on type. A completed page can still
            # require visual review: preserve work checkpoints and refuse
            # publication until explicitly approved. This check is durable
            # across resumed invocations, not just the current worker state.
            if not allow_unreviewed:
                cur.execute(f"""
                    SELECT COUNT(*) FROM {n['work']}
                    WHERE doc_id=%s AND source_hash=%s AND extraction_hash=%s
                      AND elements @> '[{{"type":"vector_region"}}]'::jsonb
                """,(doc_id,source,fingerprint))
                review_pages=cur.fetchone()[0]
                if review_pages:
                    raise VisualReviewRequired(review_pages)
            cur.execute(f"DELETE FROM {n['published']} WHERE doc_id=%s",(doc_id,))
            cur.execute(f"""
                INSERT INTO {n['published']} (
                    doc_id,page_number,page_end,page_width,page_height,
                    page_rotation,chunk_text,elements,section_title,file_path,
                    file_name,content_sha256,source_hash,extraction_hash
                ) SELECT doc_id,page_number,page_end,page_width,page_height,
                         page_rotation,chunk_text,elements,section_title,
                         file_path,file_name,content_sha256,
                         source_hash,extraction_hash
                FROM {n['work']}
                WHERE doc_id=%s AND source_hash=%s AND extraction_hash=%s
            """,(doc_id,source,fingerprint))
            cur.execute(f"""
                INSERT INTO {n['docs']}
                  (doc_id,source_hash,extraction_hash,page_count,completed_at)
                VALUES (%s,%s,%s,%s,now())
                ON CONFLICT (doc_id) DO UPDATE SET
                    source_hash=EXCLUDED.source_hash,
                    extraction_hash=EXCLUDED.extraction_hash,
                    page_count=EXCLUDED.page_count,completed_at=now()
            """,(doc_id,source,fingerprint,page_count))
            # Remove only the current input's checkpoints. An interrupted
            # earlier extraction still has its own independently keyed work.
            cur.execute(f"""
                DELETE FROM {n['work']}
                WHERE doc_id=%s AND source_hash=%s AND extraction_hash=%s
            """,(doc_id,source,fingerprint))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _images_in_groups(pages, cfg):
    from pdf_processor import ElementType
    units=[]
    errors={}
    for page in pages:
        if page.error or page.is_blank:
            continue
        for element in page.elements:
            if element.element_type not in (ElementType.IMAGE,ElementType.CHART):
                continue
            if getattr(element, "visual_asset", None):
                # Native-only publication is an explicit, provenance-backed
                # state. OCR and chart interpretation are pending, not faked.
                asset=element.visual_asset
                if (cfg["visual_processing"]!="preserve_defer"
                        or element.image_bytes or element.text
                        or asset.get("source")!="original_pdf"
                        or asset.get("status")!="PENDING_OCR_OR_VISUAL_ENRICHMENT"):
                    errors[page.page_number]=ValueError(
                        "Invalid deferred visual reference or quality policy"
                    )
                continue
            if getattr(element, "approved_asset", None):
                if element.text and not element.image_bytes:
                    # Exact, manually approved source-image digest, not a
                    # general skip for small/non-text images.
                    continue
                errors[page.page_number]=ValueError(
                    "Approved image lost its provenance or text"
                )
                continue
            if not element.image_bytes:
                errors[page.page_number]=ValueError("Image region could not render")
                continue
            if len(element.image_bytes) > cfg["ocr_max_batch_bytes"]:
                errors[page.page_number]=ValueError(
                    "Image exceeds configured safe OCR request size"
                )
                continue
            units.append((page,element))
    groups=[]
    current=[]
    size=0
    for pair in units:
        payload=len(pair[1].image_bytes)
        if current and (len(current)>=cfg["ocr_batch_images"] or
                        size+payload>cfg["ocr_max_batch_bytes"]):
            groups.append(current)
            current=[]
            size=0
        current.append(pair)
        size+=payload
    if current:
        groups.append(current)
    return groups,errors


def _ocr_pages(pages, cfg, limiter, *, ocr_gate=None, chart_gate=None):
    """Parallelize API RPCs, NOT PyMuPDF page handles (which aren't thread-safe)."""
    from pdf_processor import ElementType
    from vision_ocr import (
        batch_document_text_detection, _describe_chart_with_gpt4o,
    )
    groups,errors=_images_in_groups(pages,cfg)
    if not groups and not errors and not any(page.needs_ocr for page in pages):
        # High-throughput native path: no thread pool, raster or cloud work.
        return {},0
    if groups and not cfg["ocr_enabled"]:
        for group in groups:
            for page,_ in group:
                errors[page.page_number]=ValueError(
                    "OCR is disabled but page contains image regions"
                )
        return errors,0
    processed=0
    chart_jobs=[]

    def worker(group):
        from contextlib import nullcontext
        with ocr_gate if ocr_gate is not None else nullcontext():
            result=batch_document_text_detection(
                [element.image_bytes for _,element in group],
                limiter=limiter,
                max_retries=cfg["ocr_max_retries"],
                timeout_seconds=cfg["ocr_timeout_seconds"],
                min_confidence=cfg["ocr_confidence_min"],
                bbox_detail=cfg["ocr_bbox_detail"],
            )
        return group,result

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=cfg["ocr_workers"]
    ) as pool:
        future_map={pool.submit(worker,group):group for group in groups}
        for future in concurrent.futures.as_completed(future_map):
            group=future_map[future]
            try:
                _,responses=future.result()
                if len(responses)!=len(group):
                    raise RuntimeError("OCR group response count mismatch")
                for (page,element),response in zip(group,responses):
                    processed+=1
                    if not response.success or response.error:
                        errors[page.page_number]=RuntimeError(
                            "OCR unresolved: " + (response.error or "no result")
                        )
                        continue
                    element.text=response.text
                    element.confidence=response.confidence
                    element.language=getattr(response,"language","") or ""
                    element.ocr_regions=response.regions
                    if (element.element_type==ElementType.IMAGE
                            and not response.text.strip()):
                        # A non-text figure or unreadable regulatory label is
                        # not equivalent to successful extraction. It must be
                        # reviewed or handled by an explicitly approved
                        # visual-description policy before publication.
                        errors[page.page_number]=RuntimeError(
                            "IMAGE_REGION_UNRESOLVED: no text or visual "
                            "description; review required"
                        )
                        element.image_bytes=b""
                        continue
                    if element.element_type==ElementType.CHART:
                        if not cfg["chart_descriptions_enabled"]:
                            errors[page.page_number]=ValueError(
                                "Chart detected but chart descriptions disabled"
                            )
                        else:
                            chart_jobs.append((page,element,response.text))
                    else:
                        # Retain geometry but discard processed PNG raster.
                        element.image_bytes=b""
            except Exception as exc:
                for page,_ in group:
                    errors[page.page_number]=exc

    # Chart interpretation is a separate bounded network pool, NOT a
    # sequential call within the OCR result collector. It never alters
    # the original detected labels or image/region provenance.
    def describe_chart(entry):
        from contextlib import nullcontext
        page,element,labels=entry
        context=" ".join(
            other.text for other in page.elements
            if other is not element and other.text
        )[:500]
        with chart_gate if chart_gate is not None else nullcontext():
            return _describe_chart_with_gpt4o(
                element.image_bytes,context,labels
            )

    if chart_jobs:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg["chart_workers"]
        ) as chart_pool:
            futures={chart_pool.submit(describe_chart,entry):entry
                     for entry in chart_jobs}
            for future in concurrent.futures.as_completed(futures):
                page,element,labels=futures[future]
                try:
                    description=future.result()
                    if not description or not description.strip():
                        raise ValueError("Chart description unavailable")
                    element.text=(
                        "[CHART/GRAPH DESCRIPTION]\n"+description.strip()+
                        ("\n[OCR LABELS]\n"+labels if labels else "")
                    )
                except Exception as exc:
                    errors[page.page_number]=RuntimeError(
                        f"Chart interpretation unresolved: {type(exc).__name__}: {exc}"
                    )
                finally:
                    element.image_bytes=b""

    for page in pages:
        if page.needs_ocr:
            page.raw_text="\n".join(
                el.text for el in page.elements if el.text
            ).strip()
            if not page.raw_text:
                errors[page.page_number]=RuntimeError(
                    "Scanned page has no recoverable OCR text"
                )
            page.needs_ocr=False
    return errors,processed


def _tables(pages, path, cfg):
    """Reuse the existing validated PDF table extractor with one open per window."""
    from table_extractor import (
        extract_tables_for_pages, inject_table_text_into_elements,
        _summarise_table_with_gpt4o,
    )
    candidates={p.page_number:p for p in pages if p.has_tables and not p.error}
    if not candidates:
        return {}
    context={pn:p.raw_text for pn,p in candidates.items()}
    # Strict PDFProcessor already extracted table cells and bboxes using
    # its live pdfplumber page. Avoid another PDF open and second find_tables.
    # Preserve the older extraction fallback for any legacy PageContent input.
    precomputed = {
        pn:getattr(page,"staged_table_results",None)
        for pn,page in candidates.items()
    }
    missing=[pn for pn,results in precomputed.items() if results is None]
    table_map={pn:results for pn,results in precomputed.items()
               if results is not None}
    failures={}
    if missing:
        extracted, failures = extract_tables_for_pages(path,missing,context)
        table_map.update(extracted)
    for pn, results in table_map.items():
        if not results:
            failures[pn]="Detected table has no recoverable cells"
            continue
        if cfg["table_summaries_enabled"]:
            for table in results:
                if table.markdown:
                    table.summary=_summarise_table_with_gpt4o(
                        table.markdown,context.get(pn,""),
                    )
        try:
            inject_table_text_into_elements(
                candidates[pn].elements,results,strict=True,
            )
        except (ValueError, RuntimeError) as exc:
            failures[pn]=str(exc)
    return {pn:RuntimeError(message) for pn,message in failures.items()}


def stage_one(
    file_path, profile, n, limiter=None, *,
    doc_id=None, ocr_gate=None, chart_gate=None,
):
    from pdf_processor import PDFProcessor
    cfg=settings(profile)
    path=Path(file_path).resolve(strict=True)
    if not path.is_file() or path.suffix.casefold()!=".pdf":
        raise ValueError("Stage input must be an existing local PDF")
    doc_id=str(doc_id or hashlib.sha256(
        str(path).encode("utf-8")
    ).hexdigest())
    before=path.stat()
    source_hash_started=time.monotonic()
    source=source_hash(str(path))
    initial_hash_seconds=time.monotonic()-source_hash_started
    fingerprint=extraction_fingerprint(profile)
    from approved_images import read_approved_image_manifest
    approved_images, _ = read_approved_image_manifest(
        cfg["approved_image_manifest"]
    )
    started=time.monotonic()
    with profile_document_lock(doc_id,fingerprint) as acquired:
        if not acquired:
            return {"doc_id":doc_id,"status":"BUSY"}
        done,published=read_completed(doc_id,source,fingerprint,n)
        proc=PDFProcessor(
            str(path),doc_id,path.name,strict=True,
            max_render_pixels=cfg["max_render_pixels"],
            capture_rich_text=cfg["capture_rich_text"],
            approved_image_assets=approved_images,
            capture_table_structure=cfg["capture_table_structure"],
            max_table_source_json_bytes=cfg["max_table_source_json_bytes"],
            preserve_uninterpreted_visuals=cfg["visual_processing"]=="preserve_defer",
            max_visual_regions_per_page=cfg["max_visual_regions_per_page"],
            empty_table_policy=cfg["empty_table_policy"],
        )
        # Header-only read is cheap; don't render completed pages during resume.
        with __import__("fitz").open(str(path)) as pdf:
            page_count=pdf.page_count
            if pdf.is_encrypted:
                raise ValueError("Encrypted PDF")
        if published==(source,fingerprint,page_count):
            return {"doc_id":doc_id,"status":"SKIPPED",
                    "pages":page_count,"elapsed_seconds":0}
        errors_total=0
        pages_processed=0
        images_processed=0
        visuals_deferred=0
        timings={"pdf_parse":0.0,"ocr":0.0,"table_postprocessing":0.0,
                 "checkpoint_sql":0.0,"publish_sql":0.0,
                 "source_verification":0.0}
        parse_started=time.monotonic()
        for window in proc.iter_page_batches(
            cfg["page_batch_size"],completed_pages=done,
            max_window_raster_bytes=cfg["max_window_raster_bytes"],
        ):
            timings["pdf_parse"]+=time.monotonic()-parse_started
            invalid={
                page.page_number:RuntimeError(page.error)
                for page in window.pages if page.error
            }
            active=[page for page in window.pages if not page.error]
            phase_started=time.monotonic()
            ocr_errors,ocr_count=_ocr_pages(
                active,cfg,limiter,ocr_gate=ocr_gate,chart_gate=chart_gate
            )
            timings["ocr"]+=time.monotonic()-phase_started
            invalid.update(ocr_errors)
            phase_started=time.monotonic()
            table_errors=_tables(active,str(path),cfg)
            timings["table_postprocessing"]+=time.monotonic()-phase_started
            invalid.update(table_errors)
            rows=[]
            for page in active:
                if page.page_number in invalid:
                    continue
                try:
                    rows.append(_page_row(page,window,source,fingerprint))
                except Exception as exc:
                    invalid[page.page_number]=exc
            phase_started=time.monotonic()
            _persist_window(rows,invalid,doc_id,source,fingerprint,n)
            timings["checkpoint_sql"]+=time.monotonic()-phase_started
            visuals_deferred+=sum(
                1 for page in active if page.page_number not in invalid
                for element in page.elements if (
                    getattr(element,"visual_asset",None)
                    or getattr(element,"unresolved_region",None)
                )
            )
            # No page data or raster buffers are retained across windows.
            pages_processed+=len(rows)
            images_processed+=ocr_count
            errors_total+=len(invalid)
            elapsed=max(0.001,time.monotonic()-started)
            LOG.info(
                "staged doc=%s successful_pages=%d/%d failed_pages=%d "
                "ocr_images=%d pages_per_min=%.1f",
                doc_id[:12],len(done)+pages_processed,page_count,
                errors_total,images_processed,(pages_processed*60)/elapsed,
            )
            parse_started=time.monotonic()
        # Full content hash protects against a PDF being modified during
        # a long-running extraction, including changes undetected by mtime.
        verification_started=time.monotonic()
        after=path.stat()
        if (before.st_size!=after.st_size
            or before.st_mtime_ns!=after.st_mtime_ns
            or source_hash(str(path))!=source):
            raise RuntimeError("Source PDF changed during extraction; no publication")
        timings["source_verification"]+=time.monotonic()-verification_started
        if errors_total:
            return {
                "doc_id":doc_id,"status":"INCOMPLETE","pages":page_count,
                "new_pages":pages_processed,"failed_pages":errors_total,
                "ocr_images":images_processed,
                "elapsed_seconds":round(time.monotonic()-started,2),
            }
        try:
            publication_started=time.monotonic()
            _publish(
                doc_id,source,fingerprint,page_count,n,
                allow_unreviewed=(cfg["publish_unreviewed_vector_regions"]
                                  or cfg["visual_processing"]=="preserve_defer"),
            )
            timings["publish_sql"]+=time.monotonic()-publication_started
        except VisualReviewRequired as pending:
            return {
                "doc_id":doc_id, "status":"REVIEW_REQUIRED",
                "pages":page_count, "review_pages":pending.review_pages,
                "new_pages":pages_processed, "reused_pages":len(done),
                "elapsed_seconds":round(time.monotonic()-started,2),
            }
        elapsed=max(0.001,time.monotonic()-started)
        return {
            "doc_id":doc_id,"status":"SUCCESS","pages":page_count,
            "new_pages":pages_processed,"reused_pages":len(done),
            "ocr_images":images_processed,
            "elapsed_seconds":round(elapsed,2),
            "new_pages_per_min":round(60.0*pages_processed/elapsed,1),
            "ocr_images_per_min":round(60.0*images_processed/elapsed,1),
            "native_extraction_complete":True,
            "visual_enrichment_complete":visuals_deferred==0,
            "visual_assets_deferred_this_run":visuals_deferred,
            **({"source_hash_initial_seconds":round(initial_hash_seconds,3),
                "stage_timings_seconds":{key:round(value,3)
                                         for key,value in timings.items()}}
               if cfg["profile_stage_timings"] else {}),
        }


def iter_pdf_files(root):
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for file in sorted(files):
            if file.lower().endswith(".pdf"):
                yield str(Path(directory)/file)


def run(profile, *, file_path=None, root=None, max_docs=20, doc_workers=2):
    if (bool(file_path)+bool(root))!=1:
        raise ValueError("Use exactly one of --file or --root")
    if not 1 <= doc_workers <= 32 or not 1 <= max_docs <= 10000000:
        raise ValueError("Invalid worker or document cap")
    import threading
    n=require_initialized(profile)
    cfg=settings(profile)
    # Global per-process caps across ALL document workers. The DB quota
    # independently coordinates combined requests/images across servers.
    backend=cfg["worker_backend"]
    from config import POSTGRES
    if (POSTGRES.get("pool_enabled",False) and backend=="threads"
            and POSTGRES["pool_max_connections"] < doc_workers+1):
        raise ValueError(
            "PostgreSQL pool_max_connections must exceed doc_workers "
            "in thread mode; each document retains an advisory-lock lease"
        )
    if backend=="processes":
        if (cfg["ocr_enabled"] or cfg["chart_descriptions_enabled"]
                or cfg["table_summaries_enabled"]
                or cfg["visual_processing"]!="preserve_defer"):
            raise ValueError(
                "Process workers require native preserve_defer extraction "
                "with OCR, chart descriptions and GPT table summaries disabled"
            )
        import multiprocessing
        executor_type=concurrent.futures.ProcessPoolExecutor
        executor_kwargs={"mp_context":multiprocessing.get_context("spawn")}
        if cfg["worker_recycle_documents"]:
            executor_kwargs["max_tasks_per_child"]=cfg["worker_recycle_documents"]
        ocr_gate=None
        chart_gate=None
    else:
        executor_type=concurrent.futures.ThreadPoolExecutor
        executor_kwargs={}
        ocr_gate=threading.BoundedSemaphore(cfg["ocr_workers"])
        chart_gate=threading.BoundedSemaphore(cfg["chart_workers"])
    limiter=None
    if cfg["ocr_enabled"]:
        from ocr_quota import detect_project_id,PostgresOcrLimiter
        key=hashlib.sha256(
            ("gcp-vision-document-text:"+detect_project_id()).encode("utf-8")
        ).hexdigest()
        limiter=PostgresOcrLimiter(
            key,rpm=cfg["ocr_requests_rpm"],ipm=cfg["ocr_images_ipm"],
            max_wait_seconds=cfg["max_quota_wait_seconds"],
        )
    files=[file_path] if file_path else iter_pdf_files(root)
    counts={"SUCCESS":0,"SKIPPED":0,"BUSY":0,"INCOMPLETE":0,
            "REVIEW_REQUIRED":0,"FAILED":0}
    pending={}
    with executor_type(max_workers=doc_workers,**executor_kwargs) as pool:
        def collect(done):
            for future in done:
                path=pending.pop(future)
                try:
                    result=future.result()
                    LOG.info("FILE %s %s",path,result)
                    status=result["status"]
                except Exception as exc:
                    LOG.exception("PDF staging failed for %s",path)
                    try:
                        record_document_failure(path,profile,n,exc)
                    except Exception:
                        LOG.exception(
                            "Could not persist document-level failure for %s",path
                        )
                        raise  # fail the batch if durable error logging is unavailable
                    status="FAILED"
                counts[status]+=1
        for index, path in enumerate(files):
            if index>=max_docs:
                break
            if backend=="processes":
                future=pool.submit(stage_one,path,profile,n,None)
            else:
                future=pool.submit(
                    stage_one,path,profile,n,limiter,
                    ocr_gate=ocr_gate,chart_gate=chart_gate,
                )
            pending[future]=path
            if len(pending)>=doc_workers*2:
                done,_=concurrent.futures.wait(
                    pending,return_when=concurrent.futures.FIRST_COMPLETED,
                )
                collect(done)
        while pending:
            done,_=concurrent.futures.wait(
                pending,return_when=concurrent.futures.FIRST_COMPLETED,
            )
            collect(done)
    return counts


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("validate","init","preview","run"))
    parser.add_argument("--profile",required=True)
    parser.add_argument("--apply",action="store_true",
                        help="Explicitly create new staging and shared quota tables")
    parser.add_argument("--file")
    parser.add_argument("--root")
    parser.add_argument("--max-docs",type=int,default=20)
    parser.add_argument("--doc-workers",type=int,default=2)
    args=parser.parse_args(argv)
    from processing_profile import load_profile
    profile=load_profile(args.profile)
    cfg=settings(profile)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.command=="validate":
        result={"valid":True,"writes":False,
                "page_table":names(profile)["published"],
                "extraction_fingerprint":extraction_fingerprint(profile),
                "settings":{key:value for key,value in cfg.items()
                            if key not in ("schema","table")}}
    elif args.command=="init":
        result=init(profile,apply=args.apply)
    elif args.command=="preview":
        if not args.file:
            parser.error("--file required for preview")
        import fitz
        with fitz.open(args.file) as doc:
            result={
                "pages":doc.page_count,"encrypted":doc.is_encrypted,
                "table":names(profile)["published"],"writes":False,
            }
    else:
        result=run(
            profile,file_path=args.file,root=args.root,
            max_docs=args.max_docs,doc_workers=args.doc_workers,
        )
    print(json.dumps(result,indent=2))
    return int(result.get("INCOMPLETE",0)>0 or
               result.get("REVIEW_REQUIRED",0)>0 or result.get("FAILED",0)>0)


if __name__=="__main__":
    raise SystemExit(main())
