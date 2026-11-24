# Regulatory evidence retention: 15-day irreversible-source window

**Priority: P0 release blocker.** The intake PDFs/DOCXs are available for 15 days and may then be purged. A native-page checkpoint, populated OCR queue, text chunk, translation, or chart summary is **not** proof that all source evidence is safely preserved. If an item depends on an original file path after purge, it is *not recoverable*. The original published 778-page PBRER currently has 219 unprocessed OCR source assets and 26 VISUAL_PENDING assets (last user report); this is explicitly **NOT retention ready**.


## Selected policy (user confirmed 2026-11-24)

**Derivative-only. No byte-identical PDF/DOCX originals may be retained after purge.** The archive-copy option described below is historical/not applicable to this corpus. Do not enable it. Extracted page images, original-resolution image regions, vector evidence and other derivatives remain subject to the organization's approved storage and retention policy. In particular, full-page PNGs may constitute a prohibited substitute: explicitly confirm permission before using the pilot below.

The real corrected 10-page inventory now returns 15 distinct numbered caption candidates (1–15), 13 mixed and 2 raster, with zero reported DB/cloud writes. Caption discovery is not proof of figure-region completeness.

`visual_evidence.py` is a bounded PDF derivative pilot. It captures RGB PNG and source-linked manifest per physical page with explicit permission (`--authorized-page-derivatives`, `--policy-id`), 150–400 DPI, 50 pages/call, pixel and byte bounds; original files are never copied. `verify` reads only retained derivatives by capture ID; missing or corrupt pages fail. It does not replace native text/table extraction or native-resolution raster/vector retention. It does not write PostgreSQL, invoke any LLM, assert durable storage or authorize purge. Synthetic source-removal, tamper, rotation and policy tests are included in figure recovery tests. A verified local pilot does not establish fleet readiness.

## Retained figure-caption candidates (opt-in, CI pending)

Set capture_figures=true with all_pages; configure figure_max_distance_pt, figure_padding_pt and max_figure_json_bytes in the visual settings. Existing numbered native caption detector stores source text, caption hash, page geometry and nearby raster/vector hints in the same evidence manifest. read_figures returns unverified candidates after source removal; read_region can retrieve a candidate crop if available. Competing captions, spanning images or overlapping proposals require full-page context; never represent a geometric proposal as a verified figure boundary. English native caption grammar only at present; scanned/unlabelled/multilingual figures require further work. Rendered evidence remains mandatory for this mode. No new tables or cloud calls. Latest acceptance pending.

## Integrated native evidence capture (opt-in, public and PostgreSQL CI passed at 16abe45)

Use the existing extraction profile, retaining its approved schema/table settings. Example only:

~~~yaml
extraction:
  worker_backend: processes
  visual_processing: preserve_defer
  ocr_enabled: false
  chart_descriptions_enabled: false
  table_summaries_enabled: false
  retained_evidence:
    root: /approved/extracted/evidence
    policy_id: approved-policy-identifier
    renderer: "1.26.0"
    authorized_page_derivatives: true
    visual_settings:
      dpi: 300
      workers: 1
      selection_mode: all_pages
      capture_rasters: false
      capture_vectors: false
~~~

Set the root/policy and exact installed renderer to approved actual values (example version is not a current-version claim). Single visual worker is intentional: existing document workers provide parallelism, avoiding nested pools. Evidence semantics are independently versioned, not part of native extraction fingerprint. New native extraction uses the same PDF handle; already published native docs verify/backfill without rewriting native rows. Local evidence report does not certify production durability or purge readiness. For retained OCR, use the same semantic visual settings and renderer. No extra DB table or transaction types; original copies forbidden. Public 393-page and PostgreSQL integration acceptance passed at 16abe45; full production durability/SLA remains unverified.

## Deferred OCR from retained evidence (opt-in, CI pending)

The existing deferred OCR profile accepts a retained_evidence mapping; example shape (replace root, policy, renderer and settings with the exact approved capture configuration):

~~~yaml
deferred_ocr:
  render_dpi: 300
  retained_evidence:
    root: /approved/extracted/evidence
    policy_id: approved-policy-identifier
    renderer: "1.26.0"
    visual_settings:
      dpi: 300
      selection_mode: all_pages
    read_workers: 2
~~~

The renderer version is an example, not a default or current version claim. Set it to the exact capture manifest identity.renderer. Use the exact semantic visual settings (including any raster/vector capture flags and budgets); runtime worker settings can differ. Strict retained mode will not access an original path or archive and does not silently change DPI. New OCR fingerprint prevents overwriting results from original-render mode. Reuse existing queue and quota mechanisms; avoid blindly re-enqueueing prior successful work. Requires approved storage and cloud transmission authorization, tested native geometry, and passing CI before proprietary execution. Existing legacy original PDF render remains unchanged and is not certified thread-safe.

## Vector evidence (opt-in pilot)

Set capture_vectors=true with all_pages and approved derivative policy. Configurable max_vector_paths, max_vector_items, max_vector_json_bytes bound retained renderer-exposed path and clipping/group geometry in the existing manifest. Over-limit/unsupported geometry is explicitly marked as requiring retained PNG fallback. read_vectors retrieves checked path inventory and provenance without original PDF. No numeric interpretation or exact figure segmentation is implied. Default remains false. Full public rendered/region baseline fc97e8f passed 393/393 pages and 25/25 crops; latest vector gate pending.

## Native-resolution raster evidence (opt-in pilot)

Set capture_rasters=true in the existing visual evidence profile only after policy authorizes extracted raster and page derivatives. This mode currently requires all_pages; it preserves native-dimension extracted image bytes and separate soft masks alongside rendered page fallback. Each page manifest records displayed occurrence geometry, content SHA and explicit unsupported/limit status. Identical blobs deduplicate within capture identity. read_raster(root,capture_id,number,occurrence,mask=False,settings=...) retrieves verified bytes after source removal. No new database schema. Inline/unknown objects and compositing fidelity still require rendered fallback; this does not authorize purge or guarantee complete native visual evidence. Benchmark storage cost before enabling.

## Source-independent retained region access (acceptance pending)

The existing visual evidence module exposes read_region(root,capture_id,number,bbox=None,settings=...). It retrieves verified PNG bytes or a crop at the retained DPI with source SHA, physical page, parent image SHA, region SHA and coordinate provenance. bbox is unrotated PyMuPDF points; no original file lookup, database write or persistent duplicate crop. Native-only records return an explicit NO_RETAINED_PIXELS error. This is a building block for OCR/LLM workers, not automatic figure segmentation, source raster fidelity or permission to transmit regulatory data. Full source purge remains mandatory; approved derivative storage is required.

## Selective evidence mode (opt-in, acceptance pending)

Default `selection_mode=all_pages` remains conservative. `native_text_or_render` preserves hashed native text/style/layout for strictly text-only candidates and full PNG fallback for other pages. This is an evidence experiment, not a validated replacement for complete native table/figure extraction. Native-only records require independent native/table reconciliation; do not infer purge safety. Native selection thresholds are profile-controlled, and changing selection mode changes capture identity. Existing runtime tuning remains identity-stable. No original PDF copy and no new DB tables/transactions. Full-page derivative permission is still mandatory because fallback may render any page. See canonical state for public and Windows CI results.

## Configurable bounded parallel capture

Use profiles/visual_evidence_example.json for DPI, workers, batch_pages, max_in_flight, max_pixels and max_png_bytes. Runtime changes require a new invocation, not a code deployment. Hard safety ceilings and authorization/integrity checks are not bypassable. Worker and batch tuning preserves capture identity; DPI changes identity. Profile mode accepts the full document range and internally bounds batches; legacy CLI remains compatible. Set approved root, policy ID and source path explicitly; no originals retained. Example after latest CI/public gate passes and derivative storage is approved:

~~~powershell
& $Python visual_evidence.py capture --file $OriginalPdf --root $EvidenceRoot --page 1 --end-page 10 --profile .\profiles\visual_evidence_example.json --policy-id $PolicyId --authorized-page-derivatives
~~~

Use the same profile with verify, capture ID and page range, no source file. A single invocation owns the document; do not launch nested pools or multiple hosts on the same source without a fleet scheduler. Local verification remains distinct from production durability and mandatory purge. Public acceptance for implementation 04d1032 passed 43/43 selected pages across three real NIST/EMA PDFs, with zero replay rendering; 15/15 Linux figure tests. Extended complete-public-document, 3,001 synthetic page and explicit Windows figure-test gate is pending.

## Local validation runbook — derivative-only pilot

The 2026-11-24 Windows validation run reported 9 passed/3 import errors (optional source_evidence_archive module not copied). The derivative pilot is now self-contained apart from PyMuPDF and the Python standard library. Transfer visual_evidence.py and tests/test_figure_recovery.py from the latest feature branch ZIP; do not enable original archiving. Expected current regression count: 15, including isolated module launch, replay and parallel runtime identity. Verify current CI and local tests before any real-document derivative capture.

~~~powershell
$Python = ".\.venv-validation\Scripts\python.exe"
& $Python visual_evidence.py --help
& $Python -m unittest discover -s tests -p "test_figure_recovery.py" -v
~~~

Only after explicit organizational approval for full-page derivatives and a permitted persistent storage root, set $OriginalPdf to the previously verified 10-page article and set $EvidenceRoot and $PolicyId to the actual approved path and identifier:

~~~powershell
$Capture = & $Python visual_evidence.py capture --file $OriginalPdf --root $EvidenceRoot --page 1 --end-page 10 --profile .\profiles\visual_evidence_example.json --policy-id $PolicyId --authorized-page-derivatives | ConvertFrom-Json
$Capture | Format-List
& $Python visual_evidence.py verify --root $EvidenceRoot --capture-id $Capture.capture_id --page 1 --end-page 10 --profile .\profiles\visual_evidence_example.json
~~~

Expected: verified_pages=10, local_content_verified=true, original_copied=false, original_required_for_verification=false, durable_storage_verified=false, purge_safe=false. Do not delete the original manually; synthetic tests cover removal. The deadline remains mandatory. Real capture, storage approval and complete figure-level evidence are still pending.

## First policy decision (not optional)

Determine whether the purge agreement permits retaining a **byte-identical immutable copy** in the authorized output storage after the intake system deletes its copy. The legal/contractual retention rules apply to any copies, backups, rendered images, source text and derived artifacts as applicable. Do not copy an original or create persistent image snapshots until permitted storage and deletion terms are established.

**If authorized: preserve source bytes before purge.** The best future-proof evidence is a content-addressed immutable PDF/DOCX archive with SHA-256, size, URI, verified retrieval, encrypted access, retention/deletion policy and backup/replication. Keep original source doc_id (path-derived in current extractor) distinct from SHA-256 of source *bytes*. Store the source file locator in protected metadata; do not use local intake paths as durable URLs. Reparse/derivation in later months can operate on the archive. Archive storage must be durable, accessible to OCR/figure workers, and backed up; a file copied to a temporary local directory is not a production archive. Four TB of source bytes is the minimum raw storage budget, excluding replication, indexes, and generated assets.

**If original-byte retention is prohibited:** before purge, extract a bounded versioned evidence bundle of all authoritatively preservable content—native text with page spans/bboxes, exact table source-cell grids and continuity, figures containing raster **and vector** graphics, page renderings of every chart/diagram/microscopy panel at suitable resolution with scale and units, OCR word evidence, figure captions, their narratives and stable section/page linkages. Keep evidence as source-backed artifacts, not compressed descriptions alone. **A derivative-only representation cannot guarantee support for every conceivable future use case.** Record what was irretrievably excluded and prevent false claims of lossless future recovery. Even derivatives may be subject to deletion requirements.

## Evidence caution from original 10-page scientific article (2026-11-24)

A user-verified 10-physical-page article (first printed page 1340) yielded 28 *caption candidates* in the initial native figure inventory. The raw geometric hints repeatedly assigned a full-page embedded image to unrelated figures; this is NOT independently verified figure coverage. Figure inventory now marks spanning page images as page-level evidence instead of attributing them to every caption. **A full-page background image may contain all plotted graph pixels and annotations; it must remain archivable even when no individual figure bbox is verified.** Its existence cannot replace preserving readable rendered source page/figure bytes before purge if exact originals cannot be legally retained. Caption-only discovery misses scanned captions and uncaptioned figures; inventory quality gates must count those separately. No source PDF has been archived or deemed PURGE_SAFE by this code.

## Evidence manifest, per document and version

Minimum source-faithful fields:

| Artifact | Requirements before purge |
| --- | --- |
| Source identity | original document identifier, byte hash, format, ingestion timestamp and authorized retention policy ID |
| Reparse capability | verified archive URI for byte-identical source **if allowed**; else explicit source-unavailable flag and derivative fidelity limitations |
| All pages | complete physical page count; exact native text including reading order, language, text spans, coordinates and page size/rotation; original page-vs-printed labels mapping |
| Original tables | full raw cell grids, row/column indices, multi-page continuation evidence, headers, footnotes and table caption/section links, including null values |
| Figures | every caption-driven figure plus image-only and vector-only candidates, stable figure and panel IDs, source page+bbox, original pixel evidence or full-page/high-resolution diagram snapshot and graph-path evidence |
| Figure content | axes, units, legends, error/confidence interval conventions, panel labels, experimental conditions, source image crop/hash and nearby native caption/discussion; no inferred values without grounded evidence |
| Enrichments | raw GCP Vision OCR text/word polygons/confidence and state (TEXT_READY, VISUAL_PENDING etc.), source page/asset IDs and processing profile/model version; optional Google Translation with source/target language, original text retained |
| Derived insights | independently versioned per-figure and full-table summaries; evidence-linked assertions and explicit unresolved gaps, not ungrounded numerical readings |
| RAG retrieval | searchable text/caption/narrative linked by figure ID, original page, table ID, source artifact URI/hash, section; actual images/graphs must be accessible at query time |
| Integrity | per-artifact byte SHA-256 and durable-storage existence verification; source byte hash; replay-safe IDs; no missing pages, figures, or ambiguous table parts without explicit exceptions |

A **clinical/scientific chart** with a one-word OCR label is not "summarized". An image with no OCR does not imply a blank figure. A native-text caption and the associated plotted image are **distinct evidence** that must remain linked.

## Retention release gate: fail closed

For each document, track three independent readiness states rather than a misleading single "extracted" boolean:

1. **NATIVE_COMPLETE**: every page published under consistent source/extraction version, source table grids intact, sections and IDs reconciled; no silent page skips.
2. **EVIDENCE_DURABLE**: authorized bytes or complete permitted source-faithful artifact package stored in approved, replicated durable storage; every blob verified by checksum, retrieval-tested, and linked to original source version; no unresolved source-location dependencies. **This is mandatory before any deletion.**
3. **ENRICHMENT_COMPLETE**: all expected OCR jobs processed or explicitly pending with recoverable **durable** image evidence; figures inventoried including vector paths and panels; full-table/figure summaries available or explicitly deferred to a post-purge worker that can read the archived evidence. Rich text + OCR-only is not enough.

An overall **PURGE_SAFE** decision requires native and evidence durability, plus enrichment either complete or demonstrably reconstructible from retained authorized evidence. If no original/reconstructible evidence is retained, enrichment must finish *before* purge and the remaining gaps must be reported. Never call an all-vector chart complete merely because an embedded-image OCR count reconciles.

Recommended machine-readable gate output: source/hash/document/page counts; native status; archived URI/bytes/hash/verification; figure inventory count and gaps; expected/processed/pending OCR counts; table counts/headers/continuation integrity; summary coverage by source content type; last successful checksum verification; boolean PURGE_SAFE and list of **blocking reasons**. This report must be an independently verified production artifact, not a guess derived from queue counts.

## Opt-in integrity primitive implemented (not automatically enabled)

`source_evidence_archive.py` provides explicit, byte-exact PDF/DOCX capture and independent full-SHA256 verification on an authorized filesystem. Its actions are deliberately restricted: `capture` needs `--authorized-retention`, `--file` and `--root`; `verify` takes a source SHA-256 and the archive root. Blob locations are content addressed. Manifest creation is atomic; archived content is not overwritten on retries. The current implementation does not use GCP Storage or any cloud credential. The result always reports `durable_storage_verified=false` and `purge_safe=false`: an intact local file is **not** proof of a production backup, replicated object store, authorized retention or complete semantic evidence.

If and only if the organization permits retaining original bytes after the intake copy is deleted, evaluate in an approved storage path (never an ephemeral Downloads/temp folder):

~~~powershell
$Python = '.\.venv-validation\Scripts\python.exe'
$Source = 'C:\path\to\approved\original.pdf'
$ArchiveRoot = 'D:\approved-persistent-evidence'
& $Python source_evidence_archive.py capture --file $Source --root $ArchiveRoot --authorized-retention
# To prove bytes remain identical after intake deletion, use the printed source_sha256:
& $Python source_evidence_archive.py verify --root $ArchiveRoot --sha256 '<actual sha256 from capture>'
~~~

**Production archive is not yet implemented**: current helper supports an explicitly chosen filesystem only. Production requires an approved durable storage adapter/volume, encryption, backup/replication, access controls, retention rules, audit, per-doc original path + SHA mapping, full restoration tests, and an external purge-safe gate. Do not treat this CLI as automatically operational at 3.2M documents.

`deferred_ocr.py` now has an opt-in restoration path: if the original source path no longer exists and `SOURCE_EVIDENCE_ARCHIVE_ROOT` is explicitly configured, it resolves a verified SHA-matching PDF from this content-addressed archive; otherwise it fails closed. A disposable PostgreSQL regression proves the adapter can find the correct bytes after the original source file is removed. This is a local-archive capability, not GCS permission or an authorization to retain files.
## Implementation sequencing — smallest safe architecture

- **Immediately:** clarify authorized retention boundary and choose an approved persistent blob store (object storage/shared durable filesystem). Never presume the available gcpkeys.json has Google Cloud Storage permissions—it has only proven Vision OCR and Translation.
- **Before expanding the 219 remaining OCR assets:** configure a durable byte/source-evidence capture path and test restore after moving the original aside in a disposable validation fixture. Verify retries and full pages/figures can be reproduced from the captured bytes/evidence.
- **During 15-day intake:** native extraction and evidence archive/capture on the critical path; OCR/figure/translation in bounded independent queues with durable source access; page and visual inventories reconcile automatically. Avoid blocking the fast native worker on network OCR, but block deletion readiness on durable evidence completion.
- **After source purge:** freely evolve prompt versions, embedding models and RAG presentation **only to the extent the retained source evidence permits**. Version derived chunks and summaries; do not overwrite original raw text/table/visual evidence.
- **Throughput:** 3.2M/15 days requires ~2.47 documents/sec fleet-wide, or 148/minute. Source ingest size 4 TB /15 days is ~3.1 MB/sec average before replication; per-page rendered figures and table evidence will expand the storage and processing envelope. Benchmark page-level variability, copy verification overhead, and downstream OCR quotas; don't plan purely on average file size.

## Observed engineering baseline (2026-11-24)

Original 778-page PBRER source OCR candidates: 323; cumulative 104 processed = 78 TEXT_READY + 26 VISUAL_PENDING; 219 not yet attempted. The observed 64-asset OCR batch took 6.51s at four workers; a tiny sample, not sustained fleet evidence. User examples include Topiramate figures 1–3 with error bars, regression bands, clinical narrative, and glass durability figures with SEM images, multi-panel vector plots, logarithmic depth profiles and legends. Current embedded-image-only native visual discovery does **not** ensure vector-only figure recall. Read-only caption/vector candidate inventory exists in figure_recovery.py but is not the production retention index or complete figure summary stage.

**User action pending:** decide whether byte-identical originals can be archived as part of the permitted extraction output. Until that legal/storage decision and actual evidence-durability verification, do not mark the corpus purge-safe.
