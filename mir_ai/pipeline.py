from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path

from .azure_gateway import AzureGateway
from .bounded import bounded_parallel_map, bounded_ordered_map, ContiguousProgress
from .diagnostics import classify_exception, traceback_text
from .docx_stream import DOCXPageStream, DOCXEnricher
from .enrich import PDFEnricher
from .logging_utils import get_logger
from .metadata import MetadataExtractor, overlap_with_rimdocs
from .pdf_stream import PDFPageStream
from .profile import processing_fingerprint, validate_runtime
from .resources import ResourceGovernor
from .semantic import SemanticUnitStream, Chunker
from .store import PostgresStore
from .vision import VisionOCR

log = get_logger("mir_ai.pipeline")


def sha256_file(path, block_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(block_size), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class PipelineResult:
    doc_id: str
    generation_id: str
    pages: int
    chunks: int
    status: str


class LeaseOwnershipError(RuntimeError):
    pass


class RequiredExtractionError(RuntimeError):
    def __init__(self, message, *, category="EXTRACTION_ERROR", service="document_parser", operation="", retryable=False, resolution_hint="", error_code="", status_code=None, request_id="", root_cause_status="unconfirmed"):
        super().__init__(message)
        self.error_category=category
        self.service=service
        self.operation=operation
        self.retryable=bool(retryable)
        self.resolution_hint=resolution_hint or "Inspect the reported page/element extraction error and source evidence; correct the dependency/input issue before retrying."
        self.error_code=error_code
        self.status_code=status_code
        self.request_id=request_id
        self.observed_error=message
        self.root_cause_status=root_cause_status


class LeaseHeartbeat:
    def __init__(self, store, generation_id, worker_id, lease_seconds, interval):
        self.store = store
        self.generation_id = generation_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval = interval
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._failure: BaseException | None = None
        self._thread = None

    def __enter__(self):
        def run():
            while not self._stop.wait(self.interval):
                try:
                    if not self.store.heartbeat(self.generation_id, self.worker_id, self.lease_seconds):
                        self._failure = LeaseOwnershipError("Generation lease ownership was lost")
                        self._failed.set()
                        return
                except Exception as exc:
                    self._failure = exc
                    self._failed.set()
                    return
        self._thread = threading.Thread(target=run, daemon=True, name=f"mirai-heartbeat-{self.generation_id[:10]}")
        self._thread.start()
        return self

    def assert_owned(self):
        if self._failed.is_set():
            if self._failure is not None:
                raise LeaseOwnershipError("Generation lease heartbeat failed") from self._failure
            raise LeaseOwnershipError("Generation lease heartbeat failed or ownership was lost")

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1, self.interval))


class MIRPipeline:
    def __init__(self, settings, store=None, gateway=None, vision_ocr=None, governor=None):
        validate_runtime(settings)
        self.settings = settings
        self.store = store or PostgresStore(settings, maxconn=max(8, settings.page_workers + 4))
        self.gateway = gateway or AzureGateway(
            settings.config_path,
            max_retries=settings.api_max_retries,
            timeout_s=settings.api_timeout_s,
            retry_base_s=settings.api_retry_base_seconds,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            embedding_api_version=settings.embedding_api_version,
        )
        self.governor = governor or ResourceGovernor(
            settings.vision_concurrency, settings.chat_concurrency, settings.embedding_concurrency
        )
        base_ocr = vision_ocr or VisionOCR(
            settings.config_path,
            max_retries=settings.api_max_retries,
            timeout_s=settings.api_timeout_s,
            retry_base_s=settings.api_retry_base_seconds,
            raise_on_failure=True,
        ).ocr

        def governed_vision(data):
            with self.governor.slot(self.governor.vision):
                return base_ocr(data)
        self.vision_ocr = governed_vision

        def governed_chart(data, context=""):
            with self.governor.slot(self.governor.chat):
                return self.gateway.describe_chart(data, context)
        self.chart_describer = governed_chart

        def governed_image(data, context=""):
            with self.governor.slot(self.governor.chat):
                return self.gateway.describe_image(data, context)
        self.image_describer = governed_image

    def _event(self, **kwargs):
        recorder = getattr(self.store, "record_event", None)
        if recorder:
            try:
                recorder(**kwargs)
            except Exception:
                log.exception("Event persistence failed", extra={"operation": "record_event", "service": "postgres"})

    @staticmethod
    def _validate_page(page):
        if page.error:
            raise RequiredExtractionError(
                f"Page {page.page_number} parsing failed: {page.error}",
                category="PAGE_PARSE_ERROR", service="document_parser", operation="page_parse", retryable=False,
                resolution_hint="Inspect the reported PDF/DOCX page parsing error. If repeatable on the same page, repair/replace the source document or parser compatibility issue before retrying.",
            )
        failed = [e for e in page.elements if e.element_type in {"table", "image", "chart"} and e.extraction_status == "error"]
        if failed:
            e=failed[0]; raw=e.raw or {}; stage=str(raw.get("stage") or f"{e.element_type}_enrichment"); observed=str(raw.get("error") or "required extractor returned error status")
            category=str(raw.get("error_category") or "REQUIRED_EXTRACTION_FAILED"); service=str(raw.get("service") or "document_parser"); retryable=bool(raw.get("retryable",False)); error_code=str(raw.get("error_code") or ""); resolution=str(raw.get("resolution_hint") or ""); status_code=raw.get("status_code"); request_id=str(raw.get("request_id") or ""); root_status=str(raw.get("root_cause_status") or "unconfirmed")
            # Current page enrichers preserve dependency exception text in raw.error. Parse only
            # explicit structured evidence from that text; never infer an unseen root cause.
            try:
                payload=json.loads(observed)
                if isinstance(payload,dict) and payload.get("service"):
                    service=str(payload.get("service") or service); stage=str(payload.get("operation") or stage); category=str(payload.get("error_category") or category); retryable=bool(payload.get("retryable",retryable)); observed=str(payload.get("message") or observed); root_status="dependency_reported" if payload.get("error_code") else root_status; error_code=str(payload.get("error_code") or error_code)
            except Exception:
                match=re.search(r"(azure_[a-z_]+)\s+([a-z_]+)\s+failed with HTTP\s+(\d+);\s+request_id=([^;]+);\s+response=(.*)",observed,re.I)
                if match:
                    service=match.group(1); stage=match.group(2); status=int(match.group(3)); status_code=status; request_id=match.group(4).strip(); response_text=match.group(5).strip(); retryable=status in {408,409,425,429} or status>=500; category="EXTERNAL_HTTP"; observed=f"HTTP {status}; request_id={request_id}; response={response_text}"
                    try:
                        dep=json.loads(response_text.split("; input_items=",1)[0])
                        if isinstance(dep,dict) and dep.get("code"):
                            error_code=str(dep.get("code"));root_status="dependency_reported"
                    except Exception:
                        pass
                    resolution=("Dependency throttled/server-failed this visual request; bounded retry is permitted and persistent failures should be investigated using the request ID." if retryable else "Permanent dependency HTTP error. Verify the returned gateway code/message and configured endpoint/deployment/API contract before retrying unchanged input.")
            raise RequiredExtractionError(
                f"Page {page.page_number} required extraction failed: {e.element_type} at {stage}: {observed}",
                category=category, service=service, operation=stage, retryable=retryable, error_code=error_code, status_code=status_code, request_id=request_id, root_cause_status=root_status,
                resolution_hint=resolution or "Inspect the exact page/element error and its dependency logs. Retry automatically only when the captured failure is marked transient.",
            )
        if page.needs_full_ocr:
            full_page = next((e for e in page.elements if e.raw.get("full_page_ocr")), None)
            if full_page is None or not full_page.text.strip():
                raw=(full_page.raw if full_page is not None else {}) or {}
                raise RequiredExtractionError(
                    f"Page {page.page_number} requires full-page OCR but no OCR text was produced; extractor_error={raw.get('error','none')}",
                    category="OCR_REQUIRED_CONTENT_MISSING", service="google_vision", operation="full_page_ocr",
                    retryable=bool(raw.get("retryable", False)),
                    resolution_hint="Verify Google Vision availability/credentials and inspect the page image quality. If the page genuinely contains no readable text, review the fail-closed policy before accepting it.",
                )

    def _metadata_should_run(self, business_metadata) -> bool:
        if self.settings.metadata_mode == "disabled": return False
        if self.settings.metadata_mode == "optional": return business_metadata is not None
        return True

    def process_file(self, file_path, *, canonical_path=None, source_url="", source_version=None,
                     rimdocs_metadata=None, worker_id=None, run_id="", logical_object_id=""):
        path = Path(file_path)
        if not path.exists(): raise FileNotFoundError(file_path)
        ext = path.suffix.lower()
        if ext not in {".pdf", ".docx"}: raise ValueError(f"Unsupported document type: {ext}")

        canonical = canonical_path or str(path.resolve()).replace("\\", "/")
        worker_id = worker_id or f"worker-{uuid.uuid4()}"
        file_hash = sha256_file(str(path))
        profile = processing_fingerprint(self.settings)
        started = time.monotonic()
        current_stage = "DISCOVERED"
        doc_id = ""
        generation_id = ""

        try:
            doc_id = self.store.ensure_document(canonical, source_url, rimdocs_metadata)
            business_metadata = rimdocs_metadata if rimdocs_metadata is not None else self.store.get_business_metadata(doc_id)
            generation_id = self.store.ensure_generation(doc_id, file_hash, source_version or file_hash, processing_fingerprint=profile)

            if not self.store.claim_generation(generation_id, worker_id, self.settings.lease_seconds, self.settings.generation_max_attempts):
                progress = self.store.generation_progress(generation_id)
                if progress["status"] == "ACTIVE":
                    return PipelineResult(doc_id, generation_id, progress.get("pages_total") or 0,
                                          self.store.generation_chunk_count(generation_id), "ACTIVE")
                if int(progress.get("attempt_count") or 0) >= self.settings.generation_max_attempts:
                    raise RuntimeError(f"Generation retry limit reached ({self.settings.generation_max_attempts} attempts): {generation_id}")
                raise LeaseOwnershipError(f"Generation is leased by another worker: {generation_id}")

            log.info("Document processing started", extra={
                "run_id": run_id, "logical_object_id": logical_object_id, "doc_id": doc_id,
                "generation_id": generation_id, "stage": current_stage, "canonical_path": canonical,
            })
            self._event(run_id=run_id, logical_object_id=logical_object_id, doc_id=doc_id,
                        generation_id=generation_id, severity="INFO", event_type="DOCUMENT_STARTED",
                        stage=current_stage, message="Document processing started")

            with LeaseHeartbeat(self.store, generation_id, worker_id, self.settings.lease_seconds,
                                self.settings.heartbeat_seconds) as heartbeat:
                progress = self.store.generation_progress(generation_id)
                resume_page = max(1, int(progress.get("last_page_completed") or 0) + 1)
                pages_total = int(progress.get("last_page_completed") or 0)
                if resume_page > 1:
                    self._event(run_id=run_id, logical_object_id=logical_object_id, doc_id=doc_id,
                                generation_id=generation_id, severity="INFO", event_type="DOCUMENT_RESUMED",
                                stage="PAGE_EXTRACTION", page_number=resume_page,
                                message=f"Resuming at page {resume_page}")
                    log.info("Resuming document from durable page checkpoint", extra={
                        "run_id": run_id, "logical_object_id": logical_object_id, "doc_id": doc_id,
                        "generation_id": generation_id, "stage": "PAGE_EXTRACTION", "page": resume_page,
                    })

                current_stage = "PAGE_EXTRACTION"
                if ext == ".pdf":
                    stream = PDFPageStream(str(path), doc_id, self.settings.scanned_text_threshold, start_page=resume_page)
                    enricher = PDFEnricher(str(path), doc_id, self.settings.ocr_render_dpi,
                                          self.vision_ocr, self.chart_describer, self.image_describer)
                    page_source = stream
                else:
                    stream = DOCXPageStream(str(path), doc_id)
                    enricher = DOCXEnricher(str(path), self.vision_ocr, self.image_describer)
                    page_source = (p for p in stream if p.page_number >= resume_page)

                results = bounded_parallel_map(enricher.enrich_page, page_source,
                                               self.settings.page_workers, self.settings.max_inflight_pages)
                contiguous = ContiguousProgress(resume_page - 1)
                for page in results:
                    heartbeat.assert_owned()
                    self.store.upsert_page(generation_id, page)
                    self._validate_page(page)
                    pages_total = max(pages_total, page.page_number)
                    last = contiguous.mark(page.page_number)
                    if not self.store.heartbeat(generation_id, worker_id, self.settings.lease_seconds,
                                                stage=current_stage, last_page=last):
                        raise LeaseOwnershipError("Lost generation ownership during page extraction")
                    if last and last % self.settings.progress_log_every_pages == 0:
                        log.info("Page extraction checkpoint committed", extra={
                            "run_id": run_id, "logical_object_id": logical_object_id, "doc_id": doc_id,
                            "generation_id": generation_id, "stage": current_stage, "page": last,
                        })
                        self._event(run_id=run_id, logical_object_id=logical_object_id, doc_id=doc_id,
                                    generation_id=generation_id, severity="INFO", event_type="CHECKPOINT",
                                    stage=current_stage, page_number=last,
                                    message=f"Committed contiguous page checkpoint {last}")

                progress = self.store.generation_progress(generation_id)
                pages_total = max(pages_total, int(progress.get("last_page_completed") or 0))
                current_stage = "PAGE_EXTRACTION_COMPLETE"
                if not self.store.heartbeat(generation_id, worker_id, self.settings.lease_seconds,
                                            stage=current_stage, last_page=pages_total, pages_total=pages_total):
                    raise LeaseOwnershipError("Lost generation ownership after page extraction")

                header_sigs, footer_sigs = self.store.repeated_header_footer_signatures(generation_id)

                def summarize_table(text, page_start, page_end):
                    if not text: return ""
                    with self.governor.slot(self.governor.chat):
                        return self.gateway.chat_text([
                            {"role":"system","content":"Summarize only the supplied extracted table. Do not infer missing values. Preserve key values, units, groups, and notable comparisons in concise factual prose."},
                            {"role":"user","content":f"Pages {page_start}-{page_end}\n\n{text}"},
                        ], max_tokens=500, temperature=0)

                current_stage = "CHUNKING"
                if not self.store.heartbeat(generation_id, worker_id, self.settings.lease_seconds, stage=current_stage):
                    raise LeaseOwnershipError("Lost generation ownership before chunking")
                units = SemanticUnitStream(doc_id, source_url, header_sigs, footer_sigs, None).from_pages(
                    self.store.iter_pages(generation_id)
                )

                def add_table_summary(unit):
                    if unit.unit_type == "table" and unit.text:
                        summary = summarize_table(unit.text, unit.page_start, unit.page_end)
                        if summary: unit.text += f"\n\n[TABLE SUMMARY]\n{summary}"
                    return unit

                units = bounded_ordered_map(add_table_summary, units, self.settings.table_summary_workers,
                                            max(self.settings.table_summary_workers, self.settings.table_summary_workers*2))
                chunks = Chunker(doc_id, generation_id, self.settings.chunk_target_min_tokens,
                                 self.settings.chunk_target_max_tokens, self.settings.chunk_overlap_tokens).chunks(units)

                batch=[]; count=0
                for chunk in chunks:
                    batch.append(chunk)
                    if len(batch) >= self.settings.embedding_batch_size:
                        self._embed_and_store(batch, run_id, logical_object_id)
                        count += len(batch); batch=[]
                if batch:
                    self._embed_and_store(batch, run_id, logical_object_id); count += len(batch)
                if count == 0: raise RuntimeError("No chunks produced")
                self.store.prune_generation_chunks(generation_id, count)
                if not self.store.heartbeat(generation_id, worker_id, self.settings.lease_seconds,
                                            stage=current_stage):
                    raise LeaseOwnershipError("Lost generation ownership after chunking")

                if self._metadata_should_run(business_metadata):
                    current_stage = "METADATA"
                    authoritative, missing = overlap_with_rimdocs(business_metadata or {})
                    extracted = MetadataExtractor(self.gateway).extract_missing_stream(
                        missing, self.store.iter_metadata_candidate_pages(generation_id, 20)
                    )
                    self.store.upsert_metadata(generation_id, {**authoritative, **extracted}.values())
                else:
                    current_stage = "METADATA_SKIPPED"
                if not self.store.heartbeat(generation_id, worker_id, self.settings.lease_seconds,
                                            stage=current_stage):
                    raise LeaseOwnershipError("Lost generation ownership before activation")

                current_stage = "ACTIVATION"
                self.store.activate_generation(generation_id, worker_id)
                elapsed_ms = int((time.monotonic()-started)*1000)
                log.info("Document activated", extra={
                    "run_id": run_id, "logical_object_id": logical_object_id, "doc_id": doc_id,
                    "generation_id": generation_id, "stage": "COMPLETE", "pages_total": pages_total,
                    "chunks": count, "elapsed_ms": elapsed_ms, "status": "ACTIVE",
                })
                self._event(run_id=run_id, logical_object_id=logical_object_id, doc_id=doc_id,
                            generation_id=generation_id, severity="INFO", event_type="DOCUMENT_ACTIVE",
                            stage="COMPLETE", message="Document generation activated",
                            details={"pages": pages_total, "chunks": count, "elapsed_ms": elapsed_ms})
                return PipelineResult(doc_id, generation_id, pages_total, count, "ACTIVE")
        except Exception as exc:
            diag = classify_exception(exc)
            mark_error = None
            if generation_id and doc_id:
                try:
                    self.store.mark_failed(generation_id, worker_id, str(exc))
                except Exception as db_exc:
                    mark_error = db_exc
                    db_diag = classify_exception(db_exc, service="postgres", operation="mark_failed")
                    log.error("Could not persist generation failure status", extra={
                        "run_id": run_id, "logical_object_id": logical_object_id, "doc_id": doc_id,
                        "generation_id": generation_id, "stage": current_stage,
                        **{k:v for k,v in db_diag.to_dict().items() if v not in (None, "")},
                    }, exc_info=True)
            self._event(run_id=run_id, logical_object_id=logical_object_id, doc_id=doc_id,
                        generation_id=generation_id, severity="ERROR", event_type="DOCUMENT_FAILED",
                        stage=current_stage, message=diag.observed_error, diagnostic=diag,
                        details={"traceback": traceback_text(exc),
                                 "mark_failed_error": str(mark_error) if mark_error else ""})
            log.error("Document failed", extra={
                "run_id": run_id, "logical_object_id": logical_object_id, "doc_id": doc_id,
                "generation_id": generation_id, "stage": current_stage,
                **{k:v for k,v in diag.to_dict().items() if v not in (None, "")},
            }, exc_info=True)
            raise

    @staticmethod
    def _is_context_length_error(exc: Exception) -> bool:
        text = str(exc).casefold(); response=getattr(exc,"response",None)
        if response is not None:
            try: text += " " + str(response.text).casefold()
            except Exception: pass
            if getattr(response,"status_code",None) != 400: return False
        return any(m in text for m in ("context length","maximum context","too many tokens","input is too long","token limit","maximum number of tokens"))

    def _embed_and_store(self, batch, run_id="", logical_object_id=""):
        reusable=set()
        if self.settings.enable_embeddings and hasattr(self.store,"existing_embedded_chunk_ids"):
            reusable=self.store.existing_embedded_chunk_ids(batch[0].generation_id,batch)
            for c in batch:
                if c.chunk_id in reusable: c.metadata["embedding_input"]="persisted_identical_chunk"
        if self.settings.enable_embeddings:
            normal=[c for c in batch if c.chunk_id not in reusable and not c.metadata.get("oversize_atomic")]
            oversized=[c for c in batch if c.chunk_id not in reusable and c.metadata.get("oversize_atomic")]
            if normal:
                try:
                    with self.governor.slot(self.governor.embedding):
                        vectors=self.gateway.embeddings([c.text for c in normal])
                    for c,v in zip(normal,vectors): c.embedding=v; c.metadata["embedding_input"]="full_chunk"
                except Exception as exc:
                    diag=classify_exception(exc,service="azure_embedding",operation="embeddings")
                    self._event(run_id=run_id,logical_object_id=logical_object_id,doc_id=normal[0].doc_id,
                                generation_id=normal[0].generation_id,severity="ERROR",event_type="EMBEDDING_FAILED",
                                stage="CHUNKING",chunk_id=normal[0].chunk_id,message=diag.observed_error,diagnostic=diag,
                                details={"batch_size":len(normal),"chunk_ids":[c.chunk_id for c in normal]})
                    raise
            for c in oversized:
                try:
                    with self.governor.slot(self.governor.embedding): c.embedding=self.gateway.embeddings([c.text])[0]
                    c.metadata["embedding_input"]="full_chunk"
                except Exception as exc:
                    if not self._is_context_length_error(exc): raise
                    marker="[TABLE SUMMARY]"
                    if marker not in c.text: raise RuntimeError("Oversize atomic semantic unit exceeds embedding context and has no evidence-preserving summary fallback") from exc
                    summary=c.text.split(marker,1)[1].strip()
                    if not summary: raise RuntimeError("Oversize table exceeds embedding context and its summary is empty") from exc
                    with self.governor.slot(self.governor.embedding): c.embedding=self.gateway.embeddings([summary])[0]
                    c.metadata["embedding_input"]="table_summary_fallback"; c.metadata["embedding_fallback_reason"]="context_length"
        self.store.insert_chunk_batch(batch)
