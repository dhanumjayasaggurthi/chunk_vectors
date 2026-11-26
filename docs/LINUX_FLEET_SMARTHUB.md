# Linux fleet and SmartHub contract — 8 cores / 30 GB

## Unified production entrypoint

See [production README](../README.md) and [config.ini.example](../config.ini.example) for single-config table mapping, preflight and managed execution. Prior resource-guard release 5c52815 passed CI; wrapper acceptance pending. The older direct commands below remain reference/compatibility paths.

## Start production capture on frozen release; qualify new guard independently

Frozen c9b0991 remains the tested capture release. The optional new Linux resource guard is not a fleet throughput certificate; new CI pending. No modifications to SmartHub itself. Inspected SmartHub main commit 90d28013fef9192b421871fdc74f9f7d5572e34e: backend/data_api.py, data_api_retrieval.py, data_api_runtime.py, data_api_store.py, data_api_search.example.json, tests/test_data_api_retrieval_pg.py. Actual portal endpoints store versioned contracts/model registration/alias settings in SmartHub's own metadata database. The old search JSON is useful as an example but do not assume automatic import; use existing admin portal or API bodies.

## Per-host resource configuration

One capture coordinator per 8-core host, distinct disjoint authoritative intake JSONL shard(s). Use profiles/retention_batch_linux_8core_30gb.example.json: max 6 spawned workers, start 4; sample host /proc CPU and MemAvailable every 5s, local service cgroup v2 memory.current when available. At CPU >=92% reduce admission one slot per sample, floor 1; healthy CPU <=80%, host >=8 GiB available and group <=17 GiB for 30s ramp one slot at a time. At host <=5 GiB or group >=20 GiB no new documents; allow active documents to complete, resume from one slot after sustained recovery. After 600s continuous paused admission for current row, record NOT_ADMITTED_RESOURCE for this and subsequent nonexpired rows and drain active work; replay with new receipt. No SIGSTOP of workers holding database sessions or transactions. Monitor actual temp/filesystem capacity externally. Metrics missing/error fails the batch visibly; guard optional, old fixed path unchanged. No guarantee an individual document cannot spike/OOM; no per-page preemption, no temperature sensor, no guard on page_stage.py direct fallback or other stages. Python process pools can retain memory; if workers do not drain/release memory the batch exits incomplete. Use bounded shards and fresh processes between shards.

Use independent cgroup OS limits (systemd resource-control docs), not just nohup. Start MemoryHigh=21G, MemoryMax=25G only if actual MemTotal is >=30 GiB and host dedicated; for decimal 30GB (~27.9GiB) use MemoryHigh=19G MemoryMax=22G initially. Reduce further if DB or other workload colocated. CPUQuota=650% caps group including children at ~6.5 cores. MemoryHigh throttles/reclaims; MemoryMax may OOM-kill the service, checkpoint replay required. CPU limit is not thermal control; rely on hardware/kernel thermal safeguards. Use actual MemTotal, available storage, and measured peak RSS to adjust. No nested CPU pools. Set BLAS thread env vars to 1. Do not start multiple unconstrained coordinators on same host. Native worker holds one advisory connection plus short work connection; start pool_min_connections=1, pool_max_connections=2 per native process for native-only host (other same-process workloads may need more). Six workers ~12 plus coordinator; budget across all servers and leave SmartHub/readers/DB admin capacity. No exclusive table locking, no DDL per worker.

## Launch (after approvals and CI; examples only)
Copy same release to /opt/chunk_vectors, per-host config.ini and venv outside source control; service account and paths replace examples. Set source shard via --inventory, not config.ini path guessing. Use existing approved YAML as base, retain exact extraction semantics and native/source table mapping. Keep --profile identical across hosts sharing an output table: current full materialization fingerprint includes configuration including output and extraction root; varying host runtime in batch JSON keeps it out of that fingerprint. Same approved evidence mount path if shared profile. Different target tables can use different YAMLs and same code; SmartHub one alias per target; current API does not fan out one query to multiple aliases. Prefer common target for single corpus unless separation required by ACL/business purpose. Path-derived native doc_id means same file under different mount paths is a different doc_id: upstream disjoint ownership and canonical path or authoritative logical identity migration required; hash equality alone does not globally deduplicate. All source hashes and deadlines authoritative.

Commands for dedicated service on Linux, from /opt/chunk_vectors, using approved paths and account. Prepare local approved batch JSON (authorized_receipt_storage true only with approval; authorized_docx_derivatives false permitted for PDF-only shards, true for authorized DOCX). Ensure receipt directory exists, source profile retained_evidence authorization, actual renderer version, approved storage. Verify profile using page_stage validate and existing initialized native table (no blind init). Use a new receipt per launch, never reuse receipt filename.

~~~bash
cd /opt/chunk_vectors
umask 077
RUN_ID="$(date -u +%Y%m%dT%H%M%S)-$(cat /proc/sys/kernel/random/uuid)"
INV=/approved/intake/server01-shard001.jsonl
PROFILE=/approved/config/capture.yml
BATCH=/approved/config/batch-linux.json
RECEIPT="/approved/receipts/$RUN_ID.jsonl"
sudo systemd-run --unit="cv-capture-$RUN_ID" \
  --property=User=ingestion --property=WorkingDirectory=/opt/chunk_vectors \
  --property=CPUAccounting=yes --property=MemoryAccounting=yes \
  --property=CPUQuota=650% --property=MemoryHigh=19G --property=MemoryMax=22G \
  --setenv=OMP_NUM_THREADS=1 --setenv=OPENBLAS_NUM_THREADS=1 \
  --setenv=MKL_NUM_THREADS=1 --setenv=PYTHONUNBUFFERED=1 \
  /opt/chunk_vectors/.venv/bin/python -u retention_batch.py \
  --inventory "$INV" --receipt "$RECEIPT" --profile "$PROFILE" \
  --docx-profile profiles/docx_evidence_example.json --batch-profile "$BATCH" --apply
sudo journalctl -u "cv-capture-$RUN_ID" -f
~~~

System service survives logout (requires admin authorization). Transient unit does NOT persist reboot or auto-restart; reconcile receipts and rerun incomplete shard with new receipt. A nohup fallback (no independent cgroup limits; prefer systemd, not equivalent protection):

~~~bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
nohup /opt/chunk_vectors/.venv/bin/python -u retention_batch.py \
  --inventory "$INV" --receipt "$RECEIPT" --profile "$PROFILE" \
  --docx-profile profiles/docx_evidence_example.json --batch-profile "$BATCH" --apply \
  >"/approved/logs/$RUN_ID.log" 2>&1 </dev/null &
echo $!
~~~

Use approved log ACLs. Run 100 representative docs then next disjoint 1000; review pages/sec, docs/sec, resource_guard counters, receipts, WAL/locks, free disk and actual bytes. Prioritize very large documents early; no integrated parallel page-range work yet. Start approved native capture now with frozen release if needed; no waiting on SmartHub or optional enrichment.

## English retrieval contract (supersedes legacy mappings below)

For production.py english_v1, the new configured chunk table uses native_text (original source, NULL for model-only observations) and chunk_text (English-target content for display, embeddings and sparse retrieval). SmartHub uses chunk_text, not source_chunk_text. Native text is optional in consumer allowlist. See updated profiles/smarthub_retrieval.example.json and index SQL. Existing legacy tables remain unchanged and require a new configured output table for this contract. Historical mappings below describe legacy profiles only.

## Final storage and API contract

Reuse current configured extraction table (page data, elements JSONB, source/extraction hash), existing _work/_docs/_errors companions, and current configured final chunk table (reuse_materialize.init_output) plus its existing _status/_failures. No extra physical table for each figure, metadata field or worker. OCR results use existing separate queue; translation and embedding have existing mechanisms. Evidence remains in approved derivative storage, not original PDF blobs. Final chunk table fields actually exist: chunk_id text PK, doc_id text, profile_hash, chunk_index zero-based, file_name/file_path, source_chunk_text, chunk_text (composed embedding input), chunk_vector vector(D), embedding_processing_note, nullable page_start/end, section_title, headers, document_metadata/chunk_metadata/content_types/metadata_evidence JSONB. Preserve source text for display/citations; do not confuse chunk_text augmented input with verbatim source. SmartHub uses source_chunk_text as sparse input, chunk_vector as dense input and maps API chunk_text to source_chunk_text. API chunk_source_id is pipeline doc_id (NOT an upstream RIM ID), chunk_number zero-based; physical page number maps page_start, page_end preserved; DOCX pages null, logical spans in chunk_metadata. JSONB field values returned as entire objects; SmartHub response references are physical column or lookup.column, NOT JSON paths. The sample exposes complete JSONB only for approved consumers; audit fields before exposing (including filenames/source metadata). Hide file_path and raw vectors in match response; vector column must be exposed/registered for search and generic API may allow it as authorized, so review key/alias scope. Do not expose work/errors/status or unrestricted evidence root. Raw visual asset bytes are not served by this API; PDF capture_id not uniformly present in final chunk metadata; dedicated evidence access/citation resolution still needs integration. Actual upstream business metadata lookup only if approved existing table has unique key and authoritative mapping; no invented document_id, no new metadata table by default. Metadata filters are physical exposed columns; JSON path filtering not supported by current contract, promote approved frequently-filtered fields into indexed columns only if required and DBA approves (not implemented). Retrieval may see native-only/partial enrichment; do not claim OCR/translation/LLM fused into chunks yet.

Use profiles/regulatory_metadata_api.example.yml: actual deterministic metadata rules label/regex/constant, typed values, first_pages 1..100 for document rules, chunk rules, required/optional, source_columns, inherited metadata and embedding input fields. Missing optional omitted, missing required fails, sample_values never used to fill, conflicts with authoritative source column fail. Currently first matching page/value; label/regex no semantic interpretation; multilingual patterns need configuration; not native DOCX metadata integration (required PDF rules rejected, optional omitted); native capture doesn't execute these rules until materialization. Keep metadata optional during capture, separately evaluate critical fields. New example is preview/materialization profile; not authorized full-page capture config. Metadata extraction accuracy must be sampled, no claim of full RIM fields.

Use profiles/smarthub_retrieval.example.json as separate request body examples (not direct import); create API POST /data-api/create via admin auth, contract POST /data-api/search/contracts, use actual contract ID in alias settings PUT /data-api/{api_id}/tables/{table_id}/search (validate via POST .../search/validate). Use portal to register exact Azure model deployment, dimensions, endpoint identity, query prefix and send_dimensions matching stored vectors. Sparse-only first after completed chunks and GIN expression index; hybrid only after compatible vectors + ANN index, do not use exact scan on millions of chunks. Example profiles/smarthub_indexes.example.sql for DBA review; run outside transactions, index definitions must match SmartHub keyword parser. Build indexes after initial bulk load where possible; concurrent build does not block ordinary DML but can consume significant IO/CPU/RAM. No DDL executed. Verify valid indexes, planner and actual API response in staging; SmartHub has PG retrieval tests but no cross-repo live integration on our target yet. No SmartHub code changed.

POST /data-api/{api_id}/{alias}/retrieve using x-api-key header and example query payload. Use SmartHub existing response contract and MCP tools, no new API service. Current ingestion embedding job only writes stored vectors; configured metadata and source display keep separate. No accuracy guarantee; preserve policy and mandatory purge. Native and evidence capture P0; actual OCR/translation/LLM accuracy and unified enrichment publication, fleet SLA, storage durability, authorized full-page derivatives unresolved.
