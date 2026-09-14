from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
import uuid
from pathlib import Path

from .azure_gateway import AzureGateway
from .bounded import bounded_parallel_map, bounded_ordered_map, ContiguousProgress
from .docx_stream import DOCXPageStream, DOCXEnricher
from .enrich import PDFEnricher
from .logging_utils import get_logger
from .metadata import MetadataExtractor, overlap_with_rimdocs
from .pdf_stream import PDFPageStream
from .profile import MAX_GENERATION_ATTEMPTS, processing_fingerprint, validate_runtime
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


class LeaseHeartbeat:
    def __init__(self, store, generation_id, worker_id, lease_seconds, interval):
        self.store = store
        self.generation_id = generation_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval = interval
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._thread = None

    def __enter__(self):
        def run():
            while not self._stop.wait(self.interval):
                try:
                    if not self.store.heartbeat(
                        self.generation_id, self.worker_id, self.lease_seconds
                    ):
                        self._failed.set()
                        return
                except Exception:
                    self._failed.set()
                    return

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def assert_owned(self):
        if self._failed.is_set():
            raise RuntimeError("Generation lease heartbeat failed or ownership was lost")

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1, self.interval))


class MIRPipeline:
    def __init__(self, settings, store=None, gateway=None, vision_ocr=None, governor=None):
        validate_runtime(settings)
        self.settings = settings
        self.store = store or PostgresStore(settings, maxconn=max(8, settings.page_workers + 4))
        self.gateway = gateway or AzureGateway(settings.config_path)
        self.governor = governor or ResourceGovernor(
            settings.vision_concurrency,
            settings.chat_concurrency,
            settings.embedding_concurrency,
        )
        base_ocr = vision_ocr or VisionOCR(settings.config_path).ocr

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

    @staticmethod
    def _validate_page(page):
        """Prevent a generation with silently missing required page content from activating."""
        if page.error:
            raise RuntimeError(f"Page {page.page_number} parsing failed: {page.error}")
        failed = [
            element
            for element in page.elements
            if element.element_type in {"table", "image", "chart"}
            and element.extraction_status == "error"
        ]
        if failed:
            details = "; ".join(
                f"{element.element_type}:{element.raw.get('stage', '')}:{element.raw.get('error', '')}"[:500]
                for element in failed[:5]
            )
            raise RuntimeError(
                f"Page {page.page_number} required extraction failed: {details}"
            )
        if page.needs_full_ocr:
            full_page = next(
                (element for element in page.elements if element.raw.get("full_page_ocr")),
                None,
            )
            if full_page is None or not full_page.text.strip():
                raise RuntimeError(
                    f"Page {page.page_number} requires full-page OCR but no OCR text was produced"
                )

    def _metadata_should_run(self, business_metadata) -> bool:
        """Return whether the structured metadata stage should run.

        required: always run the RimDocs-first overlap/extraction stage.
        optional: run only when authoritative metadata is actually available.
        disabled: skip the structured metadata stage entirely.
        """
        mode = self.settings.metadata_mode
        if mode == "disabled":
            return False
        if mode == "optional":
            return bool(business_metadata)
        return True

    def process_file(
        self,
        file_path,
        *,
        canonical_path=None,
        source_url="",
        source_version=None,
        rimdocs_metadata=None,
        worker_id=None,
    ):
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(file_path)
        ext = path.suffix.lower()
        if ext not in {".pdf", ".docx"}:
            raise ValueError(f"Unsupported document type: {ext}")

        canonical = canonical_path or str(path.resolve()).replace("\\", "/")
        worker_id = worker_id or f"worker-{uuid.uuid4()}"
        file_hash = sha256_file(str(path))
        profile = processing_fingerprint(self.settings)

        doc_id = self.store.ensure_document(canonical, source_url, rimdocs_metadata)
        business_metadata = (
            rimdocs_metadata
            if rimdocs_metadata is not None
            else self.store.get_business_metadata(doc_id)
        )
        generation_id = self.store.ensure_generation(
            doc_id,
            file_hash,
            source_version or file_hash,
            processing_fingerprint=profile,
        )

        if not self.store.claim_generation(
            generation_id,
            worker_id,
            self.settings.lease_seconds,
            MAX_GENERATION_ATTEMPTS,
        ):
            progress = self.store.generation_progress(generation_id)
            if progress["status"] == "ACTIVE":
                return PipelineResult(
                    doc_id,
                    generation_id,
                    progress.get("pages_total") or 0,
                    self.store.generation_chunk_count(generation_id),
                    "ACTIVE",
                )
            if int(progress.get("attempt_count") or 0) >= MAX_GENERATION_ATTEMPTS:
                raise RuntimeError(
                    f"Generation retry limit reached ({MAX_GENERATION_ATTEMPTS} attempts): {generation_id}"
                )
            raise RuntimeError(f"Generation is leased by another worker: {generation_id}")

        try:
            with LeaseHeartbeat(
                self.store,
                generation_id,
                worker_id,
                self.settings.lease_seconds,
                self.settings.heartbeat_seconds,
            ) as heartbeat:
                progress = self.store.generation_progress(generation_id)
                resume_page = max(1, int(progress.get("last_page_completed") or 0) + 1)
                pages_total = int(progress.get("last_page_completed") or 0)

                if ext == ".pdf":
                    stream = PDFPageStream(
                        str(path),
                        doc_id,
                        self.settings.scanned_text_threshold,
                        start_page=resume_page,
                    )
                    enricher = PDFEnricher(
                        str(path),
                        doc_id,
                        self.settings.ocr_render_dpi,
                        self.vision_ocr,
                        self.chart_describer,
                        self.image_describer,
                    )
                    page_source = stream
                else:
                    stream = DOCXPageStream(str(path), doc_id)
                    enricher = DOCXEnricher(
                        str(path), self.vision_ocr, self.image_describer
                    )
                    page_source = (p for p in stream if p.page_number >= resume_page)

                results = bounded_parallel_map(
                    enricher.enrich_page,
                    page_source,
                    self.settings.page_workers,
                    self.settings.max_inflight_pages,
                )
                contiguous = ContiguousProgress(resume_page - 1)
                for page in results:
                    heartbeat.assert_owned()
                    self.store.upsert_page(generation_id, page)
                    self._validate_page(page)
                    pages_total = max(pages_total, page.page_number)
                    last = contiguous.mark(page.page_number)
                    if not self.store.heartbeat(
                        generation_id,
                        worker_id,
                        self.settings.lease_seconds,
                        stage="PAGE_EXTRACTION",
                        last_page=last,
                    ):
                        raise RuntimeError("Lost generation ownership")

                progress = self.store.generation_progress(generation_id)
                pages_total = max(pages_total, int(progress.get("last_page_completed") or 0))
                if not self.store.heartbeat(
                    generation_id,
                    worker_id,
                    self.settings.lease_seconds,
                    stage="PAGE_EXTRACTION_COMPLETE",
                    last_page=pages_total,
                    pages_total=pages_total,
                ):
                    raise RuntimeError("Lost generation ownership")

                header_sigs, footer_sigs = self.store.repeated_header_footer_signatures(generation_id)

                def summarize_table(text, page_start, page_end):
                    if not text:
                        return ""
                    with self.governor.slot(self.governor.chat):
                        return self.gateway.chat_text(
                            [
                                {
                                    "role": "system",
                                    "content": (
                                        "Summarize only the supplied extracted table. Do not infer missing values. "
                                        "Preserve key values, units, groups, and notable comparisons in concise factual prose."
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": f"Pages {page_start}-{page_end}\n\n{text}",
                                },
                            ],
                            max_tokens=500,
                            temperature=0,
                        )

                units = SemanticUnitStream(
                    doc_id,
                    source_url,
                    header_sigs,
                    footer_sigs,
                    None,
                ).from_pages(self.store.iter_pages(generation_id))

                def add_table_summary(unit):
                    if unit.unit_type == "table" and unit.text:
                        summary = summarize_table(unit.text, unit.page_start, unit.page_end)
                        if summary:
                            unit.text += f"\n\n[TABLE SUMMARY]\n{summary}"
                    return unit

                units = bounded_ordered_map(
                    add_table_summary,
                    units,
                    self.settings.table_summary_workers,
                    max(self.settings.table_summary_workers, self.settings.table_summary_workers * 2),
                )
                chunks = Chunker(
                    doc_id,
                    generation_id,
                    self.settings.chunk_target_min_tokens,
                    self.settings.chunk_target_max_tokens,
                    self.settings.chunk_overlap_tokens,
                ).chunks(units)

                batch = []
                count = 0
                for chunk in chunks:
                    batch.append(chunk)
                    if len(batch) >= self.settings.embedding_batch_size:
                        self._embed_and_store(batch)
                        count += len(batch)
                        batch = []
                if batch:
                    self._embed_and_store(batch)
                    count += len(batch)
                if count == 0:
                    raise RuntimeError("No chunks produced")
                self.store.prune_generation_chunks(generation_id, count)

                if not self.store.heartbeat(
                    generation_id,
                    worker_id,
                    self.settings.lease_seconds,
                    stage="CHUNKING",
                ):
                    raise RuntimeError("Lost generation ownership")

                if self._metadata_should_run(business_metadata):
                    authoritative, missing = overlap_with_rimdocs(business_metadata or {})
                    extracted = MetadataExtractor(self.gateway).extract_missing_stream(
                        missing,
                        self.store.iter_metadata_candidate_pages(generation_id, 20),
                    )
                    self.store.upsert_metadata(
                        generation_id,
                        {**authoritative, **extracted}.values(),
                    )
                    metadata_stage = "METADATA"
                else:
                    metadata_stage = "METADATA_SKIPPED"

                if not self.store.heartbeat(
                    generation_id,
                    worker_id,
                    self.settings.lease_seconds,
                    stage=metadata_stage,
                ):
                    raise RuntimeError("Lost generation ownership")

                self.store.activate_generation(generation_id, worker_id)
                return PipelineResult(doc_id, generation_id, pages_total, count, "ACTIVE")
        except Exception as exc:
            try:
                self.store.mark_failed(generation_id, worker_id, str(exc))
            finally:
                log.exception(
                    "Document failed",
                    extra={
                        "doc_id": doc_id,
                        "generation_id": generation_id,
                        "stage": "pipeline",
                    },
                )
            raise

    @staticmethod
    def _is_context_length_error(exc: Exception) -> bool:
        text = str(exc).casefold()
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                text += " " + str(response.text).casefold()
            except Exception:
                pass
            if getattr(response, "status_code", None) != 400:
                return False
        markers = (
            "context length",
            "maximum context",
            "too many tokens",
            "input is too long",
            "token limit",
            "maximum number of tokens",
        )
        return any(marker in text for marker in markers)

    def _embed_and_store(self, batch):
        if self.settings.enable_embeddings:
            normal = [c for c in batch if not c.metadata.get("oversize_atomic")]
            oversized = [c for c in batch if c.metadata.get("oversize_atomic")]

            if normal:
                with self.governor.slot(self.governor.embedding):
                    vectors = self.gateway.embeddings([c.text for c in normal])
                for chunk, vector in zip(normal, vectors):
                    chunk.embedding = vector
                    chunk.metadata["embedding_input"] = "full_chunk"

            for chunk in oversized:
                try:
                    with self.governor.slot(self.governor.embedding):
                        chunk.embedding = self.gateway.embeddings([chunk.text])[0]
                    chunk.metadata["embedding_input"] = "full_chunk"
                except Exception as exc:
                    if not self._is_context_length_error(exc):
                        raise
                    marker = "[TABLE SUMMARY]"
                    if marker not in chunk.text:
                        raise RuntimeError(
                            "Oversize atomic semantic unit exceeds embedding context and has no "
                            "evidence-preserving summary fallback"
                        ) from exc
                    summary = chunk.text.split(marker, 1)[1].strip()
                    if not summary:
                        raise RuntimeError(
                            "Oversize table exceeds embedding context and its summary is empty"
                        ) from exc
                    with self.governor.slot(self.governor.embedding):
                        chunk.embedding = self.gateway.embeddings([summary])[0]
                    chunk.metadata["embedding_input"] = "table_summary_fallback"
                    chunk.metadata["embedding_fallback_reason"] = "context_length"

        self.store.insert_chunk_batch(batch)
