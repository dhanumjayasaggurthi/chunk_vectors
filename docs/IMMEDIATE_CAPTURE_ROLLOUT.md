# Immediate source-capture rollout — frozen release c9b0991

> **Current source handshake (2026-11-28):** No persistent original-document copies, page images, crops, raster/vector reconstructions or other non-text source substitutes. Temporary processing evidence is permitted only within explicitly approved storage, lifetime and disposal conditions. The current implementation does not enforce that lifecycle. No bulk or capture-only rollout under this corpus's text-only agreement. See [authoritative source handshake](SOURCE_EVIDENCE_RETENTION.md) and [current project state](PIPELINE_PROJECT_STATE.md). Older archive/capture guidance below is historical, not permission.

## Decision
Stop feature expansion. Begin controlled real-document processing now **only on approved targets and with derivative retention authorization**. Keep mandatory 15-day original purge unchanged. Capture native text/tables and authorized visual evidence first, defer OCR/LLM/translation/embedding unless the required evidence cannot be recovered later. No original copies. Do not certify purge readiness from a local capture flag.

### Verified release
- Tested code SHA: c9b0991026b16d787e5708ffcb840b09c24dbd9e. [Pinned ZIP](https://github.com/dhanumjayasaggurthi/chunk_vectors/archive/c9b0991026b16d787e5708ffcb840b09c24dbd9e.zip).
- [Rich public and regression workflow 45538196557](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45538196557) and [main/DB 45538196552](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45538196552) SUCCESS. Duplicate rich workflow 45538189405 SUCCESS.
- [Public PDF job 138949918968](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45538196557/job/138949918968): 5 complete PDFs, 393/393 pages pixel-checked, 25 crops, 64 sampled raster/mask checks, 10 vector pages/154 paths, 265 caption candidates, 11/11 mock enrichment cache replays, zero replay renders; 3001 small synthetic pages 2.825s cold, 0.579s replay, four orphan repairs. Latest two-worker evidence-only times 2.683+16.119+18.306+5.307+23.362=65.777s; serial 118.658s. NOT native+DB fleet throughput.
- [Public DOCX job 138949919399](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45538196557/job/138949919399) SUCCESS. [PostgreSQL job 138949918953](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45538196552/job/138949918953) explicitly passed test_two_documents_parallel_publish_and_idempotent_replay including mixed dispatcher, expired and wrong-source cases, replay. Windows 3.11/3.13 and Linux PASS. No actual cloud-provider quality tests.
- User reports two days lost. If all 3.2M remain and 13 days available, 246154 documents/day, 2.85/s at 24h/day; at 20 effective hours/day 3.42/s. Aim initially for >=5/s aggregate on representative real corpus, not a proven requirement/capacity. Actual per-shard deadlines and remaining count govern.

## Three launch prerequisites
1. Approved native PostgreSQL output/profile with existing compatible tables; do not initialize or alter production tables blindly. Preserve existing extraction fingerprint/config unless deliberately versioning. Local validation schema cv_validation_v1 is NOT production.
2. Explicit policy permitting full-page PNG and native image/vector/figure derivatives, storage path, capacity and durable storage procedure. Page derivatives may themselves be prohibited; never set authorization flags based only on urgency. Existing storage capacity must be measured. Public rendered PNG total 797485315 bytes/393 pages (~2.03 MB/page); historical scan alone 577511414 bytes/44 pages (~13.13 MB/page). These are fixture-dependent and omit extra native assets, manifests, DB and replicas. At 20 pages/document and the public sample average, 3.2M documents would require about 130 TB just for PNGs; illustrative not a corpus estimate. 4 TB original size does not imply 4 TB derivative capacity. Do not use selective mode as completeness proof (only 2/393 sample pages native-only); do not lower fidelity silently.
3. Authoritative deadline-ordered, disjoint intake shards with explicit source SHA-256, absolute local/NAS path and UTC purge timestamp. Do not infer deadline from file mtime. If authoritative hashes are absent, generate/obtain them from intake process; hashing can be a bottleneck, do not invent values. For PDF-only direct page_stage fallback, root must already be a disjoint earliest-deadline cohort, and caller must reconcile inventory separately. Do not recursively run against undifferentiated full corpus.

## Immediate Windows launch (after above approvals)
Transfer pinned ZIP into existing validation folder; keep local config.ini, credentials, venv, and existing profiles. Do not transfer source documents to GitHub. Use existing approved native YAML as base, save as a separate local capture profile, e.g. profiles/capture_release.yml. Do not replace schema/table mapping with example values. Verify these extraction settings (merge into existing extraction mapping, no duplicate YAML keys):

~~~yaml
extraction:
  worker_backend: processes
  worker_recycle_documents: 200
  page_batch_size: 32
  profile_stage_timings: true
  visual_processing: preserve_defer
  ocr_enabled: false
  chart_descriptions_enabled: false
  table_summaries_enabled: false
  capture_rich_text: true
  capture_table_structure: true
  retained_evidence:
    root: 'D:\approved-extracted-evidence'
    policy_id: approved-policy-identifier
    renderer: 'EXACT_INSTALLED_PYMUPDF_VERSION'
    authorized_page_derivatives: true
    visual_settings:
      dpi: 300
      workers: 1
      selection_mode: all_pages
      capture_rasters: true
      capture_vectors: true
      capture_figures: true
~~~

Values are starting settings, not production defaults; root, policy ID and renderer MUST be real approved values. All three optional asset/caption flags together must be validated during real pilot; separate public raster/vector sampling and full rendered/figure acceptance do not certify full-corpus semantic completeness. If capture settings differ from previously retained evidence, capture identity changes; maintain same version for replay. Set embedding.enabled=false. Do not enable inline cloud calls.

~~~powershell
$Python = ".\.venv-validation\Scripts\python.exe"
& $Python -c "import fitz; print(fitz.VersionBind)"
$Profile = ".\profiles\capture_release.yml"
& $Python page_stage.py validate --profile $Profile
~~~

Prefer existing deadline dispatcher if all policy permissions are available. Create approved local copy of profiles/retention_batch_example.json with workers=4 (initial), max_documents at least the entire bounded shard count, both authorization flags true only if actually approved. Current dispatcher requires DOCX authorization even for a PDF-only shard; if unavailable use PDF-only cohort fallback below. Inventory must already exist, sorted by (purge_at_utc,path); receipt must be a new path inside approved storage, with parent existing.

~~~powershell
$Inventory = Read-Host "Absolute path to authoritative first deadline-sorted JSONL shard (100 representative real docs)"
$ReceiptDir = Read-Host "Approved receipt directory (must already exist)"
$Receipt = Join-Path $ReceiptDir ("capture-" + [guid]::NewGuid().ToString("N") + ".jsonl")
$BatchProfile = ".\profiles\retention_batch_approved.json"
& $Python retention_batch.py --inventory $Inventory --receipt $Receipt --profile $Profile --docx-profile .\profiles\docx_evidence_example.json --batch-profile $BatchProfile --apply
$Exit = $LASTEXITCODE
Get-Content $Receipt | ForEach-Object { $_ | ConvertFrom-Json } | Group-Object status | Select-Object Name,Count
$Exit
~~~

PDF-only fallback (no DOCX approval needed), only against preselected earliest-deadline cohort, does not enforce individual deadlines or produce dispatcher receipts. Caller must independently reconcile files and local capture results:

~~~powershell
$Cohort = Read-Host "Absolute path to approved earliest-deadline PDF-only cohort"
& $Python page_stage.py run --profile $Profile --root $Cohort --max-docs 100 --doc-workers 4 2>&1 | Tee-Object -FilePath .\capture-first100.log
~~~

Use an approved log destination; page_stage logs paths, so protect access. Do not rerun all native extraction if an identical published fingerprint exists; verified evidence backfill occurs automatically. If fingerprint changes due to native semantics, a reprocess may be necessary. No DDL automatically from these run commands. If existing table incompatible or uninitialized, coordinate approved DBA target rather than create arbitrary new tables.

## Ramp up without waiting on another development cycle
- Start earliest-deadline representative 100 real documents, then next disjoint 1000 and remaining shards immediately if counts, errors, actual throughput, memory/storage and database headroom permit. Use disjoint host assignments, no overlapping shards. Include scanned, text-only, table-rich, multi-panel and very large PDFs; do not judge capacity on small files only. Long documents still occupy a whole worker; assign their own disjoint early-start lane if needed.
- Measure wall-clock documents/s and physical pages/s, bytes written to approved evidence storage, source reads, worker peak RSS, DB load, failures and missed deadlines; verify actual content on samples and reconcile every receipt against authoritative source hash/version. Log failed/overrun/incomplete tasks for immediate remediation; replay with new receipt path and existing checkpoints. On replay same capture identity yields verified skips. Reconciliation requires independent durable store checks; this is not a purge authorization.
- No waiting on AI. Real OCR/LLM provider accuracy, durable storage, fleet-level global reconciliation, end-user citation behavior and 3.2M SLA still unproven. Existing PDF-only fallback is not a global scheduler; no hard cancellation of active jobs. Must have operational supervision now. Do not remove originals manually or extend mandatory purge.

## Return only operational facts
Approved evidence policy/path/capacity; remaining count and actual earliest deadline; target native profile/schema/table; available hosts, cores and RAM; first shard final summary, receipt status counts, elapsed time, actual storage delta. Do not disclose source text, credentials or private document names. If policy/target unavailable, escalation is immediate; native-only capture may still be allowed but cannot recover lost visual evidence. No further feature work until rollout feedback unless a release-blocking defect appears.

## Large-document and database concurrency clarification

See the top production clarification in [PIPELINE_PROJECT_STATE.md](PIPELINE_PROJECT_STATE.md) for the actual 3,000-page handling, document-only advisory locks, per-window and final publication transactions, connection budget, OCR/translation evidence and accuracy limits. Keep one active native profile per target/document; assign disjoint long-document lane and monitor DB waits, storage and real throughput. Do not bypass document locks or disable durability to increase speed. No claim of full automatic OCR/translation/LLM semantic publication. Frozen c9b0991 remains the code release.

## Linux fleet and SmartHub

See [LINUX_FLEET_SMARTHUB.md](LINUX_FLEET_SMARTHUB.md) for new opt-in Linux resource guard (CI pending), per-host profiles, systemd/nohup rollout, metadata rules and SmartHub mapping. Frozen code c9b0991 remains previously verified.
