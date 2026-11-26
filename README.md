# chunk_vectors — production quick start

Same operating model as main: configure, run, monitor, restart. No Docker.
Existing main.py and start_embed.sh remain for legacy jobs; do not redirect
legacy tables into the new schema contract.

## 1. Install and configure

Use your existing virtual environment; install requirements (PyMuPDF 1.28.2
qualified by CI, not 2.2 yet).

~~~bash
python -m pip install -r requirements.txt
python -m pip check
python -c "import pymupdf; print(pymupdf.VersionBind)"
~~~

Merge missing keys from [config.ini.example](config.ini.example) into your
existing config.ini. Set DB schema/native_table/chunk_table once in [POSTGRES],
source directory in [PATHS] docs_root (or [DEPLOYMENT] source_root),
approved evidence/receipt directories and retention policy/renderer.
No manually prepared inventory or deadline required. Keep each server's
source directory disjoint. The default batch profile caps one run at 10000
files; use max_documents=100 for the first test, then set the cap to cover
the full assigned folder. Repeated limited runs revisit the same prefix.
Set actual approved GCP and Azure credentials, model/deployment and combined
quota profiles. Explicit provider and storage authorization flags required.
Visual LLM example config is not a real deployment. Do not send proprietary
content to unapproved providers. Metadata rules remain in the policy YAML.

## 2. Preflight and initialize once

~~~bash
python production.py preflight --before-init
python production.py init --apply
~~~

Preflight does not write tables or call providers. DBA installs pgvector.
Initialize only the approved target. With authorized_ocr=true init creates
the existing OCR queue too. No Docker. No new table types.

## 3. Run

~~~bash
python -u production.py run --apply
~~~

New run command captures in parallel, then uses successful capture receipts
to perform OCR, selected visual LLM enrichment and publish native + OCR +
labeled visual observation chunks in the same retrieval table; DOCX native
logical publication also uses same output. Automatic embeddings only when [PIPELINE] enable_embeddings=true AND authorized_embedding=true; separate embed command otherwise. With overlap_enrichment=true, native processes and one document enrichment coordinator run concurrently, using flushed capture receipts as a disk backlog. Existing concurrent OCR/vision requests stay bounded; reserve reader processes from the native worker budget (default 5 native max + 1 reader). CPU/memory admission protection applies to capture and enrichment, but active work is not preempted. No measured fleet SLA. English retrieval requires authorized_translation=true. GCP translates native/OCR/model text to English; original extracted text goes into native_text (NULL for model-only observations), English into chunk_text, vectors and SmartHub search use chunk_text. No mixed native metadata in English text, no duplicate translation JSON. Provider failure prevents publication; no native-language fallback. Language identifiers, scientific names and quoted terms may remain. English target is not a certified translation quality score. Scanned table cell reconstruction remains unresolved; real OCR/LLM
accuracy and automatic visual coverage still need qualification. Native-only
capture remains available via capture --apply. Native-only materialize is disabled for the English contract. Existing legacy output tables are not migrated: configure a new chunk_table if needed. The run fails or marks
incomplete if selected enrichment fails, and does not silently publish a
new incomplete enriched version. Predominantly scanned pages use complete retained page OCR, with configured JPEG request compression only if needed; the lossless retained source is unchanged. To finish after originals are unavailable use production.py finish --receipt /approved/receipts/ACTUAL.jsonl --apply. Replay reuses verified checkpoints and
provider results, except a crash between provider response and cache commit
may cause a repeated call. Never manually delete originals.

For background execution (Linux):

~~~bash
nohup python -u production.py run --apply > ingestlogs.log 2>&1 </dev/null &
echo $! > ingest.pid
tail -f ingestlogs.log
~~~

Use [systemd limits](docs/PRODUCTION_REFERENCE.md#4-capture-now-then-ramp-up)
for hard OS-level protection; replace capture with run if authorized.
No Docker. Only one coordinator per server. The application guard starts
four workers, max six, stops new admission under memory pressure.
Tested release 2826ad0 passed all public and regression workflows, including overlap and embedding switch tests. [CI](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45888915276) · [PostgreSQL](https://github.com/dhanumjayasaggurthi/chunk_vectors/actions/runs/45888915322). For Windows preflight use profiles/retention_batch_example.json instead of the Linux resource profile. Restore the Linux adaptive profile on servers. Real provider quality and sustained fleet capacity remain to be measured. No Docker.

[Reference](docs/PRODUCTION_REFERENCE.md) · [Canonical state](docs/PIPELINE_PROJECT_STATE.md) ·
[SmartHub and metadata](docs/LINUX_FLEET_SMARTHUB.md) · [Legacy README](README_LEGACY.md)
