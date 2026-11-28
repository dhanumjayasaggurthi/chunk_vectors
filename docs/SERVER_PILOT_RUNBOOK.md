# Server pilot and rollout runbook

> **Current milestone:** Six documents / 98 pages / 304 valid 1536-dimension vectors reconciled. Do not repeat setup or those six documents. [Text-only source handshake](SOURCE_EVIDENCE_RETENTION.md) is mandatory; existing image evidence is temporary staging only and automatic disposal is NOT implemented. Current next task is lifecycle qualification before expanding real-source workload.

Application baseline: tested commit `a86ec54e012b640544e7abb39f56dadcf7f2e00c` (internal SCM imports may have different commit IDs). This runbook and templates do not change that application. Check [living state](PIPELINE_PROJECT_STATE.md) first for the latest results and release blockers. The server is not accessible from the assistant; the operator executes commands and shares sanitized aggregate output.

## 1. New server setup

From the approved checkout (not a live legacy working tree):

~~~bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pymupdf==1.28.2 pdfplumber==0.11.10 pdfminer.six==20260107 pillow==12.3.0 tiktoken==0.14.0
python -m pip check
python -c "from native_table_prepass import qualified_runtime; print(qualified_runtime())"
mkdir -p evidence receipts logs secrets
chmod 700 evidence receipts logs secrets
~~~

Python 3.11 is also supported and was used in the disposable PostgreSQL integration. The actual server used Python 3.13.0. Keep system Python unchanged. Check `nproc`, `free -h`, `df -h .` and effective cgroup limits. Use a separate source tree outside evidence/receipt paths. The pilot uses local folders under the checkout and a sibling source directory; production requires approved bounded staging. Never commit or package runtime directories, credentials, INI or document inventories. Add them to the internal Git ignore list.

## 2. Configuration ownership

For NEW servers only, copy config.pilot.ini.example to config.ini, then replace all placeholders with approved local values. Existing servers keep their working INI and profiles. Internal SCM may ignore profiles; transfer the five pilot files and the referenced native visual and DOCX templates separately. Do not overwrite existing profiles or change schema/semantic settings on a populated destination. Keep config.ini mode 600. The template uses a generic isolated schema; the current server already has its own intentionally selected schema.

| Location | Setting |
| --- | --- |
| config.ini POSTGRES | connection, separate native/chunk table names, schema, pool12 per process |
| PATHS / DEPLOYMENT | absolute source root, explicit inventory, receipts |
| RETENTION | absolute evidence path, policy identifier, renderer1.28.2, explicit temporary storage authorization |
| GCP | approved service account path |
| AZURE_OPENAI_CHAT | approved model/deployment alias, API version, endpoint/key |
| AZURE_OPENAI_EMBEDDING | approved text-embedding-3-large or existing deployment, verified1536 dimensions |
| PIPELINE | enable_embeddings=true only when approved |
| ENRICHMENT | optional title/table AI review disabled initially |
| DEPLOYMENT | finish4, translation4, embedding12, materialize4, one OCR reader, overlap enabled, both native acceleration options on new destinations |
| profiles/retention_batch_pilot.json | total process budget8, initial6, document cap16, resource guard |
| profiles/deployment_policy.pilot.yml | semantic v2, 120–800 tokens, overlap80, physical table lines, deterministic metadata, deferred OCR8, embeddings batch128 / token cap24000 |
| profiles/visual_enrichment_pilot.json | set model/deployment EXACTLY to INI values before authorization; cache revision operator-managed, prompt and image policy |
| profiles/visual_enrichment_execution_pilot.json | visual workers6, reader1, approved quota allocation |

The 8-logical-CPU/30-GiB server ran six native processes with two reserved evidence readers. A legacy 16-thread setup is not equivalent to 16 CPU-bound processes. The feature stages overlap across documents but serialize each feature coordinator; embeddings begin after capture and finish. The embedding runtime uses a separate pool of workers+8 connections, in addition to native/coordinator connections. No guarantee of simultaneous utilization.

The pilot profiles intentionally preserve example quota ceilings: OCR1500 RPM/IPM, visual60 RPM/600000 TPM, translation30 RPM, embeddings30 RPM/750000 TPM. Confirm real allocated quotas including shared workloads before enabling. OCR and embedding quota state is shared within the configured DB; translation/visual pacing is coordinator-local, allocate across servers. A single successful probe does not establish quota. All authorization flags in templates stay false. The tested resolver requires page derivative approval even for plan; never bypass approval. Optional AI enrichment is a separate review workflow, not automatically invoked by run. Native metadata is deterministic, optional and may be absent. Table body physical lines are not verified logical records.

## 3. Inventory and preflight

Use a unique, bounded inventory of absolute PDF paths under the configured source root. The path-only JSONL format is for sources with no explicit purge deadline contract; if upstream supplies a hash/deadline, use all three keys and strict deadline/path order. Do not invent deadlines, move originals or create unnecessary copies. Maintain a disjoint shard manifest. Directory mode uses a capped prefix; repeatedly running it does not advance to the next prefix. Census scans the whole selected directory or inventory regardless of pilot cap.

~~~bash
python production.py plan
python production.py census
python production.py preflight --before-init
# Only for new isolated destination, after approval:
python production.py init --apply
python production.py preflight
~~~

Confirm table names, profile hashes, source scope, available memory and storage. Existing initialized tables: skip init. Do not truncate, drop or replace tables to recover. Before any real data run, confirm provider and temporary evidence approval, TTL and disposal ownership. For the current server the first selected inventory had 6 PDFs, 98 pages, max45. The full test set has 13 PDFs, 11639 pages, max3082.

## 4. Execute and recover

For a small approved foreground test:

~~~bash
time python -u production.py run --apply
echo "Exit code: $?"
~~~

Preserve the final JSON and actual receipt path. Current release only auto-embeds after all capture/finish outcomes succeed; if one fails, native receipts and earlier published chunks remain. Recover one failed doc using the full ID read locally from that run's receipt, never a guessed prefix:

~~~bash
python production.py status
python production.py finish --receipt "ACTUAL_RECEIPT.jsonl" --doc-id FULL_ID --apply
echo "Exit code: $?"
# Only after all expected retrieval publications succeed:
python production.py embed --apply --rescan-source
echo "Exit code: $?"
python production.py status
~~~

A repeated VISUAL_CAPTION_MISMATCH was observed on different pages of the initial pilot. The pilot visual profile now requests empty optional caption_quote fields; native source caption text is not replaced. The live quote-free prompt then failed numeric-observation validation. Visual contract v2 adds a bounded corrective request for numeric observations, caption mismatches and unknown targets within the existing quota and attempt limit. A second semantic violation remains terminal. Deploy only after regression validation; retain the working INI and profiles. The v2 contract changes visual cache identity but not native extraction or OCR checkpoints. Existing servers should preserve a local backup and merge only the prompt, keeping their real model/deployment and all other settings. The prompt changes visual request/cache identity; old valid caches remain intact but may not be reused under the new prompt. The live v2 corrective request was also rejected on a numeric chart. Contract v3 replaces the correction schema with closed qualitative choices and exact candidate targets, validates them locally, and records an explicit recovery limitation. It does not verify plotted numerical values. A single-page probe remains deliberately single-attempt; for validated v3 use targeted finish to exercise the bounded correction path. Replace visual_enrichment.py and production.py together only after CI; keep existing settings and receipts. Persistent failure requires diagnosis, not repeated paid retries. No native recapture or YAML change is needed. If a visual caption mismatch persists, inspect `visual-plan` and `visual-probe --doc-id FULL_ID --page N` (read-only without --apply); a probe with --apply makes at most one provider attempt. Never weaken literal-caption validation, blindly repeat requests, drop evidence or change profile mid-run. Failed visual results are not cached. An interrupted uncommitted provider request may repeat on recovery. For terminal embedding failures, fix the cause then use `embed --apply --retry-failed --rescan-source`. Successful vectors are reused; coverage requires every configured destination row to have a vector and matching profile. A successful finish alone is not full end-to-end completion.

## 5. Background execution on Linux

Only for an approved bounded shard, from the repository root; no other active coordinator. Do not edit the live INI/YAML or inventory while it runs. Preserve an approved credential-free configuration snapshot separately. This shell wrapper records the wall time and actual exit code, rather than relying on the native-only elapsed counter.

~~~bash
RUN_DIR="logs/run-$(date -u +%Y%m%dT%H%M%S)-$$"
mkdir -m 700 "$RUN_DIR"
nohup bash -c '
  start=$(date +%s)
  date -u
  .venv/bin/python -u production.py run --apply
  rc=$?
  printf "exit_code=%s\nelapsed_seconds=%s\n" "$rc" "$(($(date +%s)-start))" > "$1/result.txt"
  date -u
  exit "$rc"
' _ "$RUN_DIR" > "$RUN_DIR/console.log" 2>&1 </dev/null &
echo $! > "$RUN_DIR/pid"
echo "$RUN_DIR"
~~~

Keep the printed RUN_DIR; after reconnecting set it to that exact path. Monitor:

~~~bash
tail -f "$RUN_DIR/console.log"
ps -p "$(cat "$RUN_DIR/pid")" -o pid,etime,stat,cmd
cat "$RUN_DIR/result.txt"  # exists after normal command return
python production.py status
python production.py ocr-status
df -h evidence
du -sh evidence
free -h
~~~

Ctrl-C on tail stops only the viewer. Missing result.txt means no recorded exit, not success; verify process and logs. nohup is not an OS service or a guarantee against machine reboot, cgroup eviction or OOM. Use your approved service manager for long-running fleet jobs.

Progress interpretation: native.documents/pages are cumulative stored manifest counts in configured tables; retrieval.documents/chunks are cumulative published counts; OCR statuses reflect current extraction/OCR identity. Record a baseline before each disjoint shard; deltas are useful if destinations are isolated and no other writers. Capture receipt counts are native success, not final vectors. Logs show per-document stage times and provider/embedding runtime progress. Do not sum per-document concurrent rates or use capture-only elapsed as end-to-end time. Use final JSON embedding.coverage for actual vector completion (whole configured retrieval table); a read-only exact query can be expensive, so run it at milestones, not every second:

~~~bash
python - <<'PY'
from production import resolve_configuration
from reuse_materialize import identifiers
from db import get_conn
d = resolve_configuration()
q = d["retrieval"]
table = identifiers(q)["table"]
conn = get_conn()
try:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(f"""SELECT count(*),
            count(*) FILTER (WHERE chunk_vector IS NOT NULL),
            count(*) FILTER (WHERE chunk_vector IS NULL),
            count(*) FILTER (WHERE profile_hash IS DISTINCT FROM %s)
            FROM {table}""", (q["_fingerprint"],))
        total, vectors, missing, foreign = cur.fetchone()
        print(dict(chunks=total, embedded=vectors, missing=missing, foreign_profile=foreign))
finally:
    conn.rollback()
    conn.close()
PY
~~~

Do not equate 100% chunk vectors to 100% input documents; reconcile expected distinct source inventory against native and retrieval completion and all failure outcomes. Use periodic status snapshots (e.g. every few minutes) and avoid expensive repeated full-table scans on large corpora. End-to-end docs/min = fully completed unique documents divided by actual wall minutes (including retries and all provider stages). Check current DB and evidence bytes, peak RSS, CPU, provider calls/429, retry rate and p95 stage latency. No semantic accuracy claim from counts alone.

## 6. Larger workload release gate

Do NOT point the current release at 656k or 1.3M PDFs. Six-doc recovery/vector coverage and per-document reconciliation passed. Manually validate source-grounded text, multilingual translation, table/metadata fidelity and review required visual limitations. Resolve the text-only staging/disposal gate before additional real-source processing. Then qualify the seven larger PDFs in bounded shards (start one outlier at a time if memory/storage risk); qualify >4000-page originals, representative 100-doc and sustained 1000-doc workload, compare same workload against legacy main. Use source census page mix and quotas to size fleet. 656000/10 days = 45.56 complete docs/min fleet-wide (68.33 with 50% headroom); 1.3M/10 days = 90.28 (135.42 with headroom). Neither is demonstrated.

Required before scale: source-team text-only policy, approved temporary derivative staging, retry-safe disposal after verified publication and vector completion, upstream deletion coordination, approved textual/structured/metadata/vector retention, failed-document TTL; no source purge from current local-capture receipts. Current release retains all-page PNGs and other derivatives with no automatic cleanup, and 10-GiB preflight free-space floor is a start check, not a continuous disk budget. Cannot defer mandatory disposal to the 600k review milestone. No full original fidelity guarantee after upstream deletion under text-only policy. Native and finish stages can hold full-document state; measure large-doc memory. No unbounded production shards, no deletion script until safe lifecycle qualification. Assign disjoint source shards, approved storage budget and quotas per server; choose batch max_documents to cover the explicit shard, not the corpus. Keep actual runtime settings and per-shard receipts in approved local storage, not Git. Capture/finish/embedding retries stay within original identities; do not change schema or profile during a job.

## 7. Next bounded qualification (do not launch until source handshake gate passes)

Remaining pages 747,778,918,1104,2364,2548,3082 (11541 total). Keep existing six-document inventory until gate passes. Start with one 747-page PDF to isolate large-document memory/disk behavior, then two concurrent 778+918 to measure overlap; qualify larger outliers with bounded admission and compare actual end-to-end rate against main. This is a proposed order, not a mandatory workload cap or a throughput claim. At ~0.98MiB/page observed, first shard ~732MiB and all remaining ~11GiB before overhead; these are estimates not maxima.

After approval and validated temporary disposal mechanism, create a **new** inventory from the configured source root (no copies, moves or original deletion). For the current test set only, select the unique 747-page PDF, confirm identity locally and verify no existing document in the shard. Example selection script, to be executed only after that gate:

~~~bash
python - <<'PY'
import configparser, json
from pathlib import Path
import pymupdf
cfg=configparser.ConfigParser(interpolation=None)
assert cfg.read("config.ini")
root=Path(cfg["DEPLOYMENT"].get("source_root","").strip() or cfg["PATHS"]["docs_root"]).resolve(strict=True)
chosen=[]
for p in sorted(root.rglob("*")):
    if p.is_symlink() or not p.is_file() or p.suffix.lower()!=".pdf":
        continue
    resolved=p.resolve(strict=True)
    if not resolved.is_relative_to(root):
        continue
    with pymupdf.open(resolved) as pdf:
        if not pdf.is_encrypted and pdf.page_count==747:
            chosen.append(resolved)
assert len(chosen)==1, "Expected exactly one 747-page pilot PDF; inspect locally"
out=Path(cfg["DEPLOYMENT"]["receipt_dir"])/"pilot_747.jsonl"
with out.open("x",encoding="utf-8") as stream:
    stream.write(json.dumps({"path":str(chosen[0])})+"\n")
out.chmod(0o600)
print("New inventory:",out,"documents: 1, pages: 747")
PY
~~~

This path-only inventory is for approved bounded no-explicit-deadline qualification, not a replacement for authoritative deadline/hash inventory. Update only DEPLOYMENT.inventory to its printed path once idle. Run census/plan/status/preflight, verify expected one new doc and baseline six/98/304 with unchanged fingerprints. If automatic embedding enabled and approved, use the timed nohup wrapper above; no init on existing tables. Record actual exit code, new document/vector counts, memory peak, disk high-water mark, provider errors and elapsed wall time; verify approved cleanup and source quality before the next shard. Expected cumulative native/retrieval docs7, pages845 after one successful new document; new chunk count unknown. Use actual vector coverage (including old rows), not only status counts. Never run a larger batch merely because preflight free space passes.

## 8. Conversation handoff

Read the latest `docs/PIPELINE_PROJECT_STATE.md` first, then this runbook and architecture. The user runs commands in a private internal SCM checkout; no server access from assistant. Continue from the newest observed server step rather than replay setup. Preserve measured versus estimated results and update the state after each substantive iteration. Current observed server milestone: six native and materialized docs/98 pages, 304/304 vectors, per-document counts matched, all1536 dims, missing/foreign/bad dims0; source-kind256 unspecified_native,44 OCR,4 visual. Evidence96MiB, free33GiB, available RAM21GiB. Read-only audit complete for counts/dimensions and disk snapshot; semantic source QA outstanding. Next implement/qualify text-only disposal and bounded staging, review pilot content, then approved 747-page shard. Do not repeat completed stages. See latest project state for subsequent updates. No credentials, internal file names, exact source paths or private data in Git.

### Existing simple nohup run (operator reported launch)

For the prior `nohup /usr/bin/time -p .venv/bin/python -u production.py run --apply > logs/full_pilot_run.log 2>&1 </dev/null &` command, check `ps -p "$(cat logs/full_pilot_run.pid)" -o pid,etime,stat,cmd`, `tail -n 50 logs/full_pilot_run.log`, `grep -E '^(real|user|sys) ' logs/full_pilot_run.log`, `python production.py status`, and `df -h evidence; free -h`. Process PID is the time wrapper; verify command, not merely PID existence. Native and retrieval counts are cumulative; prior baseline6 docs/98 pages/304 chunks. For isolated same-corpus run, new native or materialized docs/min = (current stage docs-6)*60/actual elapsed seconds. Fully embedded docs/min requires per-document vector reconciliation; current run auto-embeds only after capture and finish pass. No new job while coordinator active. Simple wrapper does not persist exit status; use result.txt wrapper above for future runs. No evidence removal during active work. CPU/memory admission can throttle/recover, not continuous disk guard or automatic recovery after OOM. If unapproved staging or disk pressure, arrange controlled stop and approved disposal with operator; do not blindly delete artifacts. See source handshake and current state.
