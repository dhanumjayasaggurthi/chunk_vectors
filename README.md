# chunk_vectors — high-volume regulatory ingestion

Same operating model as main: **configure, run, monitor, restart**.

Use the new retention workflow for the 3.2M-document workload. Existing
main.py and start_embed.sh remain available for their existing jobs; do not
switch a live legacy job's tables or configuration to this workflow.

**Tested release:** 835caf6 — [public and regression CI](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45546634181),
[PostgreSQL acceptance](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45546634191/job/138977721925).
No new application changes in this README update.

## 1. Install and configure

From the checkout directory (same convention as main):

~~~bash
python -m pip install -r requirements.txt "PyYAML>=6,<7"
python -m pip check
python -c "import fitz; print(fitz.VersionBind)"
~~~

Use your existing virtual environment. Copy/merge missing keys from
[config.ini.example](config.ini.example) into your local config.ini; keep
existing DB and provider credentials. For a new install only, copy the entire
example. Do not commit config.ini or keys.

The settings you normally change per server are:

~~~ini
[POSTGRES]
schema = your_approved_schema
native_table = regulatory_pages_v1
chunk_table = regulatory_chunks_v1

[DEPLOYMENT]
source_root = /data/pdfs/server01
inventory = /data/intake/server01.jsonl
receipt_dir = /data/receipts/server01

[RETENTION]
root = /data/evidence
policy_id = YOUR_APPROVED_POLICY_ID
renderer = YOUR_INSTALLED_PYMUPDF_VERSION
authorized_page_derivatives = false
authorized_receipt_storage = false
~~~

This is an excerpt, not a complete config.ini. Keep the other existing
settings from the example, including POSTGRES.folder, Azure dimensions and
the default policy file references. Set authorization flags true only after
actual approval. For DOCX capture, also obtain DOCX derivative approval.

Table names are set once. The application derives checkpoint/status tables
and the embedding mapping automatically. Leave the supplied profiles unchanged
for the first run; metadata rules can be edited later in
[deployment_policy.example.yml](profiles/deployment_policy.example.yml).

**Source inventory:** provide the existing authoritative deadline-sorted
JSONL shard (absolute path, source_sha256, purge_at_utc in UTC). Start with
100 representative files. The old main crawler only needs docs_root, but the
mandatory retention deadline requires an authoritative inventory; do not infer
deadlines from file modification times. Each server must have its own disjoint
shard. Source and receipt/evidence directories must already exist; evidence
must be outside the source purge tree. Confirm approved storage capacity:
full-page PNG derivatives may be much larger than the source.

## 2. Check and initialize once

~~~bash
python production.py preflight --before-init
python production.py init --apply
~~~

Initialize once per approved destination, not every server. The existing
DDL is reused; no drops or migrations. For an already initialized target,
just run python production.py preflight. pgvector must be installed by the DBA.
Preflight is read-only, checks DB, configuration, storage and sample source
readability, and does not call cloud services.

## 3. Run and monitor

Start in the foreground for the first 100 documents:

~~~bash
python -u production.py capture --apply
~~~

For a background job using the same pattern as start_embed.sh:

~~~bash
nohup python -u production.py capture --apply > ingestlogs.log 2>&1 < /dev/null &
echo $! > ingest.pid
tail -f ingestlogs.log
~~~

Only one capture coordinator per server. Check ps -fp "$(cat ingest.pid)"
before starting another. nohup survives logout, not reboot. Use the
[systemd command](docs/PRODUCTION_REFERENCE.md#4-capture-now-then-ramp-up)
for sustained fleet jobs: it additionally caps CPU and memory across workers.
The default Linux profile starts four workers, increases to six, and pauses
new documents under memory pressure; active documents still run, so the
application guard alone cannot guarantee no OOM. No new table creation or
cloud calls during capture. Each run generates a new receipt.

Review receipt outcomes and actual throughput/storage growth. Rerun the same
command after a failure: verified source/page checkpoints are reused. No
--force needed. Move to the next disjoint shard by changing inventory in
config.ini. Do not restart a completed shard expecting new work. Never delete
originals manually or extend the mandatory purge. Local verification is not
durable storage certification or purge authorization.

## 4. Retrieval after native capture

Do this on a separate capacity allocation, not instead of urgent capture:

~~~bash
python production.py materialize --all-docs --apply
~~~

Set materialize_max_docs to cover the intended target (default 10000; repeated
bounded invocations revisit the same ordered prefix). This is native PDF chunk
publication. SmartHub reads the configured chunk_table. GCP OCR, DOCX logical
publication and embeddings have separate explicit commands in the reference.
Deferred OCR/translation/LLM output is not yet automatically fused into final
chunks; native-only retrieval is not full enriched retrieval.

## Reference (only when needed)

- [Full production commands, tests, resource limits, recovery and optional stages](docs/PRODUCTION_REFERENCE.md)
- [SmartHub registration and metadata configuration](docs/LINUX_FLEET_SMARTHUB.md)
- [Engineering state and verified tests](docs/PIPELINE_PROJECT_STATE.md)
- [Existing main-branch workflow and embedding launcher](README_LEGACY.md)

Actual fleet throughput, evidence durability and live OCR/translation/LLM
accuracy are not certified by CI. Start approved real capture and measure
representative 100-document, then 1000-document workloads immediately.
