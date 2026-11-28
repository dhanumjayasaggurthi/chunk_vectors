# Source-team handshake — text-only retention and irreversible source deletion

**Authoritative decision as of 2026-11-28. P0 production release gate.**
The user reconfirmed the source-team understanding: original documents will eventually be deleted and the ingestion system must not retain images, copies of documents, pages or other non-text substitutes. Keep this requirement for the initial approximately 656,000 PDFs and the overall 1.3M-document objective. It supersedes the earlier derivative-only proposal that contemplated durable page/figure evidence and all archive-based rollout instructions. It is not permissible to postpone compliance until 600k documents have been ingested.

## 1. Retention boundary

| Artifact | Target rule |
| --- | --- |
| Original PDF/DOCX bytes, archives, copied documents, embedded files | No retained copies; read the authoritative input in place. Never enable the source archive helper or SOURCE_EVIDENCE_ARCHIVE_ROOT for this corpus. |
| Page PNG/JPEG, crops, screenshots, raster images/masks, renderings, recoverable vector drawings or other document substitutes | No persistent retention, including in DB/JSON/base64, object storage, backups, snapshots, packaging, logs or caches. |
| Temporary in-process bytes or explicitly approved staging files | Only during processing, with strict capacity, access, TTL and disposal controls; no durable backup. Written approval for staging location, duration and provider handling must be confirmed by the operator. |
| Source-language clear text, English translation, textual table cells/headers, textual OCR, textual visual interpretation | Intended output; preserve provenance, fidelity, language and uncertainty. Do not embed image bytes or reconstructed document representations. |
| Metadata, page/section references, content hashes, operational receipts and embedding vectors | Necessary derived search/audit data, not source copies; confirm allowed fields, sensitive metadata, access and retention with the source/data owner. Text-only does not itself establish approval for all derived data. |

No storage/API permission is inferred from a working credential, a successful probe, a technical policy ID or an authorization flag. No external sharing of confidential files. Cloud transient processing/retention and logging must be covered by approved enterprise provider terms.

## 2. Source intake and completion handshake

The prior source window was described as 15 days; actual per-document expiry and any revised agreement must come from the source owner. Do not fabricate UTC deadlines from file modification times. Confirm authoritative logical ID/path, source byte SHA-256, expiry, disjoint assignment and approved output policy. Where deadlines are provided the existing strict inventory supports absolute path, source_sha256 and purge_at_utc (UTC), sorted by deadline/path. The path-only inventory used for the isolated pilot has no verified expiry contract and is not a bulk deadline guarantee.

The following are proposed operational milestones, not implemented acknowledgment API/status enum names:

1. Intake accepted: source identity/version, authorized scope, deadline, bounded resource reservation.
2. Native extraction complete: expected physical page coverage and verified source hash; not completion.
3. Required enrichment complete: selected OCR, visual processing, translation, table continuity, metadata, source-linked text; missing/ambiguous facts explicitly marked, quality gates satisfied. Distinguish verified source text from unverified AI observations. Do not claim chart digitization from qualitative summaries.
4. Retrieval and vectors complete: per-document atomic publication, expected and actual chunk counts reconciled, every required vector current and dimension-correct, no missing/foreign chunks, durable textual outputs and review obligations reconciled.
5. Non-text disposal verified: only authorized temporary artifacts belonging to that exact source version and capture are removed; no active readers, retries or uncommitted output depend on them. Reconcile filesystem, DB payloads, local caches, logs, provider/backup policies and orphan files. Persist only approved textual audit information.
6. Notify the source owner of actual processing outcome, unresolved gaps and disposal status through the agreed channel. Upstream retains authority over deletion and its deadline. Do not automatically delete source files or delay mandatory source deletion on pipeline failure. Failed/incomplete jobs must be escalated before expiry; no success acknowledgment merely because capture or vector count passed.

The current receipt values LOCAL_CAPTURE_VERIFIED, purge_safe=false and durable_storage_verified=false are accurate and must not be repurposed as source-deletion approval. No upstream acknowledgment endpoint, retention TTL, secure disposal mechanism or exact SLA has been supplied or implemented. File unlink alone is not a claim of secure erasure on snapshots/backups; infrastructure controls must be part of the agreement.

## 3. P0 engineering work before larger real-source ingestion

Current server implementation b965877 uses all-page 300-DPI temporary evidence and immutable visual response caches for recovery, with no automatic evidence disposal. The six-doc pilot is functionally complete (6/98 pages, 304/304 vectors); approximately 96 MiB evidence remains. Do not delete manually before approved audit/recovery/disposal procedure. If the permitted staging expiry is approaching, notify the owner and follow its existing disposal process, not an indefinite hold. Do not run more real-source documents without approved temporary budget/TTL and disposal path.

Use existing production coordinator and manifests; avoid a new service or database table unless concrete need arises. First inventory all persistence routes (page evidence, raster/vector/figure manifests, OCR assets, database JSONB, work/error rows, response caches, DOCX media, temp directories and logs). Separate durable text/provenance from transient render bytes. Implement bounded staging admission in bytes as well as worker/page limits, including disk floor during execution, large single-document handling, cleanup on success and bounded failed-job expiry, crash/restart orphan recovery, no concurrent reader deletion, source/version fencing, idempotent cleanup and explicit audit. Keep a dry-run inventory/eligibility mode, require explicit apply, and never remove originals or unrelated directories. Do not use broad rm -rf. Do not introduce configuration keys into live templates until implemented/validated. Keep existing DB schema and fingerprints unless a migration is truly needed; cleanup must not force native re-extraction or silently lose successful vectors.

Text-only retrieval after upstream purge cannot recover pixels, exact plotted values, image details, form appearance or missing source context. Make this irreversibility explicit to source/product owners. Required chart/figure information must be extracted, checked and published as permitted text before disposal; if the agreed quality cannot be reached, record the gap rather than claim complete original fidelity. DOCX logical/media path needs independent text-only qualification.

Acceptance: disposable synthetic and public original fixtures with mixed native/scanned/chart/table/form content, failure between capture and publish, crash/restart, duplicate receipt, changed hash/version, active reader, invalid path/symlink, partial vector completion, deadline failure, retry and orphan cleanup. Assert durable text/table/vector coverage unchanged, no retained disallowed bytes, no source modifications, no false deletion-safe flag. Exercise PostgreSQL transactional recovery and cross-platform tests; verify actual approved server with bounded inventory. Separate functional/quality/storage/sustained throughput results. Then use the current [server runbook](SERVER_PILOT_RUNBOOK.md) to qualify 747-page first outlier, bounded concurrent larger shards, >4000-page public original and 100/1000 representative documents.

## 4. Historical evidence design (superseded for this corpus)

Previous work implemented source-independent PNG/crop/raster/vector evidence, DOCX derivatives, deadline-ordered capture and an opt-in byte archive. These are capabilities, not permission. They are useful as temporary staging mechanisms only where authorized; do not follow old permanent archive/derivative instructions. The complete prior document remains in [historical Git revision f70ed99](https://github.com/dhanumjayasaggurthi/chunk_vectors/blob/f70ed991e4e5d501c9b6b5106e94f7db00308dc3/docs/SOURCE_EVIDENCE_RETENTION.md). See [project state](PIPELINE_PROJECT_STATE.md) for CI history, actual server measurements and unresolved fidelity gates.

**Open source-owner decisions:** actual deadline feed, approved staging bytes/location/TTL, failure disposal and escalation policy, allowed text/metadata/vector/audit fields and retention duration, provider handling, backup/snapshot exclusions and authorized acknowledgment mechanism. Treat these as required before full rollout, not as already approved.
