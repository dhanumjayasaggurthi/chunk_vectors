# Regulatory document ingestion — production deployment

For the high-volume PDF/DOCX retention and retrieval pipeline use **production.py**,
not the historical main.py default. One local config.ini per server owns DB
connection, schema/table names, inventory, storage, authorizations and run limits.
Processing/metadata policy and resource thresholds are external files referenced
from that INI. No application code changes for different server paths or tables.

## 1. Install and verify

Python 3.11 recommended, Linux 8 cores/30 GB. Transfer the approved, tested ZIP
or clone the approved branch into /opt/chunk_vectors. Preserve existing
config.ini and secrets; do not overwrite them. From the repo directory:

~~~bash
cd /opt/chunk_vectors
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt "PyYAML>=6,<7"
.venv/bin/python -m pip check
.venv/bin/python -c "import fitz; print(fitz.VersionBind)"
~~~

Use approved pinned dependency versions across hosts once qualified; requirements
are not a reproducible lockfile. Keep PyMuPDF version identical for capture and
retained OCR. Install outside production if no package access. For new installs
only: copy config.ini.example to config.ini, chmod 600. Existing installs merge
new sections/keys, retaining approved credentials. Never commit config.ini or
GCP keys. Run from repository directory (existing modules and spawned workers
all read local config.ini). No alternative config path or hot reload.

## 2. Configure once

| Configuration | What to edit |
| --- | --- |
| config.ini [POSTGRES] | host, database, user, password, schema, native_table, chunk_table; optional chunk_schema |
| config.ini [DEPLOYMENT] | authoritative inventory, source_root, receipt_dir, policy/profile references, job limits and provider permissions |
| config.ini [RETENTION] | approved evidence root, policy ID, exact renderer version, visual profile and explicit authorizations |
| config.ini [GCP], [AZURE_OPENAI_EMBEDDING] | existing approved provider settings; dimensions from Azure section even when embedding disabled |
| profiles/deployment_policy.example.yml | extraction, chunking, metadata, embedding/OCR quota policies; no table names |
| profiles/retention_batch_linux_8core_30gb.example.json | adaptive CPU/RAM and shard limits; authorization flags overridden by INI |
| profiles/visual_evidence_native.example.json | 300 DPI full-page plus raster/vector/figure derivative capture, workers=1 |
| profiles/docx_evidence_example.json | DOCX budgets |

Copy example policy files for use-case changes and point INI at copies. Table
names in INI override existing profile mappings. Native table has existing
_work/_docs/_errors companions and optional _visual_ocr; chunk table has existing
_status/_failures. Embedding maps same chunk table. No extra deployment tables.
Legacy folder setting only satisfies historical config readers; it does not select
the new tables. Distinct host inventory/source/evidence paths do not change
retrieval profile hash; same table across hosts requires identical processing
policy, table mapping and dimensions. This NEW wrapper generates a retrieval
profile hash separate from capture; previously populated output from manual
profiles may have another hash, which fails closed. Use approved compatible
new versioned table rather than bypass hash checks.

Use authoritative UTF-8 JSONL inventory, sorted by (purge_at_utc,path), one
disjoint shard per server; example shape (illustrative, NOT a valid source hash):

~~~json
{"path":"/approved/input/server01/report.pdf","source_sha256":"REPLACE_WITH_ACTUAL_64_HEX_SHA256","purge_at_utc":"REPLACE_WITH_ACTUAL_UTC_DEADLINE"}
~~~

Source paths must be absolute, under source_root; no mtime-based deadlines.
Trusted inventory is required; sample path checks are not a filesystem sandbox.
Do not duplicate a logical document across mount paths; path-derived native ID
is not a globally stable business ID. Preflight checks inventory syntax/order,
sample readability and expired counts; each actual capture verifies its hash.
Create approved evidence/receipt directories outside source purge tree before
running. Do not set approval flags solely to pass a test. Full-page derivatives
can require far more disk than original PDFs; free-space floor is not a capacity
estimate or runtime storage monitor.

## 3. Cloud-free tests and preflight

~~~bash
.venv/bin/python -m unittest discover -s tests -p test_production.py -v
.venv/bin/python -m unittest discover -s tests -p test_retention_batch.py -v
.venv/bin/python -m unittest discover -s tests -p test_figure_recovery.py -v
.venv/bin/python -m unittest discover -s tests -p test_docx_evidence.py -v
.venv/bin/python -m unittest discover -s tests -p test_deferred_ocr.py -v
.venv/bin/python -m unittest discover -s tests -p test_reuse_materialize.py -v
.venv/bin/python production.py plan
.venv/bin/python production.py preflight --before-init
~~~

Run destructive PostgreSQL tests only in isolated disposable CI database, never
point test DB environment variables at production. Preflight checks installed
renderer, configured policy, authorized directories/free disk, sorted inventory
and sampled readable sources, DB connectivity, pgvector, relation existence,
catalog compatibility, DML grants, profile identity and connection-pool sizing.
It does not create tables, call providers, test writes, hash all files, verify
durability or certify throughput. --before-init permits absent complete table
groups, not partial incompatible groups. DBA installs pgvector and grants
schema/table/sequence privileges. Once authorized, ONE operator initializes
per destination (not every server):

~~~bash
.venv/bin/python production.py init --apply
.venv/bin/python production.py preflight
~~~

Initialization reuses existing additive DDL, never drops/migrates tables.
The two table groups are separate transactions; partial init requires DBA
inspection. No DDL during ordinary native capture. All write commands require
--apply. No provider call during plan/preflight/init/capture/materialize.

## 4. Capture now, then ramp up

First use an actual representative 100-document shard; includes scanned,
table-heavy, multilingual and long PDFs. Update INI inventory for next disjoint
shard, then 1,000 and larger. Never point all servers at same inventory.
Observe receipts and physical pages/sec, storage, CPU/RAM and DB wait/WAL.
Use same code revision and policy across hosts sharing target. The example
guard starts 4/max6 workers, slows admission under CPU pressure and pauses
under memory pressure; active docs drain. No forced cancellation, no thermal
sensor; hard cgroup cap may kill the job, use checkpoint replay. Current
worker granularity is one document; assign huge files early.

Foreground:

~~~bash
.venv/bin/python -u production.py capture --apply
~~~

Linux background (example service user, paths and OS resource controls; requires
authorized systemd access; confirm actual MemTotal and lower limits for shared
host). Each invocation auto-generates a new receipt file:

~~~bash
cd /opt/chunk_vectors
RUN_ID="$(date -u +%Y%m%dT%H%M%S)-$(cat /proc/sys/kernel/random/uuid)"
sudo systemd-run --unit="cv-capture-$RUN_ID" \
  --property=User=ingestion --property=WorkingDirectory=/opt/chunk_vectors \
  --property=CPUAccounting=yes --property=MemoryAccounting=yes \
  --property=CPUQuota=650% --property=MemoryHigh=19G --property=MemoryMax=22G \
  --setenv=OMP_NUM_THREADS=1 --setenv=OPENBLAS_NUM_THREADS=1 \
  --setenv=MKL_NUM_THREADS=1 --setenv=PYTHONUNBUFFERED=1 \
  /opt/chunk_vectors/.venv/bin/python -u production.py capture --apply
sudo journalctl -u "cv-capture-$RUN_ID" -f
~~~

Survives logout, not reboot; replay with same inventory and new receipt if
interrupted. To use nohup (does not enforce cgroup safety unless separately
configured):

~~~bash
umask 077
nohup .venv/bin/python -u production.py capture --apply \
  > /approved/logs/capture-$(date -u +%Y%m%dT%H%M%S).log 2>&1 </dev/null &
echo $!
~~~

Precreate approved logs directory. Do not use kill -9 for routine stopping.
Review final JSON and receipt outcomes LOCAL_CAPTURE_VERIFIED, FAILED,
INCOMPLETE_OR_BUSY, NOT_ADMITTED_DEADLINE, DEADLINE_OVERRUN,
NOT_ADMITTED_RESOURCE. Nonzero exit means not all locally verified. Reconcile
missing receipts against authoritative inventory and rerun incomplete shard
with same source version/identity. No original copies; no purge authorization.

## 5. Prepare retrieval and optional enrichment

Use separate scheduled service/host or coordinated capacity; do not add full
materialization/OCR/embedding worker pools on an already saturated host.
One coordinator per shared chunk target is preferred. Native capture P0.

~~~bash
# Native PDF chunking, no cloud; use actual published doc_id for pilot.
.venv/bin/python production.py materialize --doc-id ACTUAL_DOC_ID --apply
# Full target (bounded by materialize_max_docs in INI).
.venv/bin/python production.py materialize --all-docs --apply

# Optional OCR: existing queue; strict retained-evidence source, no original fallback.
.venv/bin/python production.py ocr-init --apply
.venv/bin/python production.py ocr-discover --doc-id ACTUAL_DOC_ID
.venv/bin/python production.py ocr-enqueue --doc-id ACTUAL_DOC_ID --apply
.venv/bin/python production.py ocr-run --doc-id ACTUAL_DOC_ID --apply
.venv/bin/python production.py ocr-status --doc-id ACTUAL_DOC_ID

# Optional vectors: same existing chunk table.
.venv/bin/python production.py embed-preview
.venv/bin/python production.py embed --apply
~~~

For OCR use explicit authorized_ocr, valid GCP credentials, combined project
quota settings, same renderer/DPI/policy and accessible approved evidence root.
For embeddings use authorized_embedding and real Azure config/quota, matching
dimensions/model. Preview validates existing embedding job without writes or
provider calls (requires provider config). Use --all-docs on OCR only if all
selected retained captures accessible. DOCX logical publication remains opt-in
via reuse_materialize.py docx-preview/docx-run; not integrated in wrapper
materialize (which reads native PDF pages). Translation/vision LLM components
have no production CLI integration here. Deferred OCR/translation/LLM outputs
are not automatically fused into retrieval chunks. No claim of complete
enriched RAG or scientific accuracy. Never wait for optional AI to capture.

Important: materialize_max_docs and ocr_max_docs cap an ordered prefix, not
a moving cursor across separate invocations. For a full target set cap >=
actual corpus count (OCR has existing 100000 CLI scope ceiling here; use
explicit doc IDs/controlled cohorts for larger targets); repeated same cap
revisits prefix. embedding_max_chunks=0 means full target; limited pilot
has distinct existing job identity, not a rolling cursor. Existing checkpoints
prevent successful rewrites but do not imply complete fleet coverage.

## 6. SmartHub and final table

No new retrieval service. Existing SmartHub POST /data-api/{api_id}/{alias}/retrieve
consumes configured chunk table. Response chunk_text maps source_chunk_text;
vector uses chunk_vector, sparse GIN uses source_chunk_text. doc_id is
pipeline ID, chunk_index zero-based, pages nullable for DOCX, metadata JSONB.
Do not expose local file_path, raw evidence or private metadata to unauthorized
consumers. Use the existing portal/registration endpoints and exact registered
embedding model. DBA review example index SQL; substitute actual names from
config.ini (example is not executed by production.py). Build GIN/HNSW after
bulk load where feasible. Start sparse only until correct vectors and ANN
index exist. Validate actual API and ACL against configured target.

See [Linux/SmartHub runbook](docs/LINUX_FLEET_SMARTHUB.md), [example API request
bodies](profiles/smarthub_retrieval.example.json), [index example](profiles/smarthub_indexes.example.sql),
[metadata policy](profiles/deployment_policy.example.yml).

## 7. Release evidence and limitations

[Canonical state](docs/PIPELINE_PROJECT_STATE.md) records actual CI results,
latest commit and unresolved gates. Prior release 5c52815 passed all three
workflows (45544472026, 45544472148, 45544465453), including 393 public PDF pages,
two public DOCX, 3001 synthetic pages, mock enrichment, adaptive guard unit
and PostgreSQL integration. New production entrypoint requires latest CI;
do not assume passing old tests covers new code. Actual 3000-page complex
document performance, 3.2M SLA, durable storage, complete scan/figure/table
semantics, live OCR/translation/LLM quality and SmartHub integration remain
unverified. Local capture is not purge-safe. Keep mandatory purge unchanged.

Historical embedding dashboard/legacy commands: [legacy README](README_LEGACY.md).
Detailed rollout: [immediate capture](docs/IMMEDIATE_CAPTURE_ROLLOUT.md),
[evidence retention](docs/SOURCE_EVIDENCE_RETENTION.md).
