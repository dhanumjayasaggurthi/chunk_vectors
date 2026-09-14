# MIR-AI configuration control plane

This document defines the configuration-driven ingestion controls intended to remain stable when a future administrative UI is added. The UI should update validated configuration values or an equivalent persisted configuration model; ingestion behavior should not depend on editing application code.

## Source switch

`[PATHS] source_type` is the primary source selector:

```ini
[PATHS]
source_type = nas
docs_root = \\server\share\archive
```

or:

```ini
[PATHS]
source_type = s3

[s3]
bucket = ...
prefix = landing/dfhd
region = us-east-1
profile = rimdocs_prod
```

When `[MIR_AI] source_auto_run=true`, `python mir_ai_main.py --config config.ini` uses this configured source. `source_auto_run=false` is the safe default and requires an explicit CLI source.

Explicit `--root`, `--s3`, or `--file` remains an operator override and takes precedence over configured automatic source selection.

## All-files mode versus control-table mode

### All eligible files

```ini
[MIR_AI]
object_list_enabled = false
source_recursive = true

enable_pdf = true
enable_docx = true
preferred_format = pdf
```

No control table is queried. The source scanner discovers eligible documents beneath the configured NAS root or S3 prefix.

The scanner uses the relative path without the final extension as the logical document identity. Therefore:

```text
sub1/ABC001.pdf
sub1/ABC001.docx
```

represent one logical document and PDF is selected when `preferred_format=pdf`, while:

```text
sub1/ABC001.pdf
sub2/ABC001.pdf
```

remain two different logical documents.

### Control-table mode

```ini
[MIR_AI]
object_list_enabled = true
object_list_schema = regulatory
object_list_table = mirai_obj_list
object_list_id_column = file_id
```

Only logical IDs returned by the configured table are eligible. Missing, disabled-format-only, and ambiguous objects are reported explicitly.

## Recursive discovery

```ini
source_recursive = true
```

NAS: recursively walks all child folders.

S3: includes all nested keys under the configured prefix.

With:

```ini
source_recursive = false
```

only files directly under the NAS root, or directly under the S3 prefix, are eligible.

## Format controls

```ini
enable_pdf = true
enable_docx = true
preferred_format = pdf
```

Policy:

- both PDF and DOCX available: preferred format wins;
- only one enabled representation available: that representation is selected;
- representation exists only in a disabled format: `FORMAT_DISABLED`;
- duplicate physical candidates for one logical ID: `AMBIGUOUS_SOURCE`;
- control-table ID has no source: `SOURCE_NOT_FOUND`.

At least one of PDF or DOCX must be enabled.

## Batch limit

```ini
source_max_files = 0
```

`0` means unlimited. Positive values cap selected logical documents. A CLI `--max-files` value overrides the configured value for that run.

## Structured metadata policy

RimDocs/structured metadata is not hard-coded as an unconditional dependency. The behavior is controlled by one setting:

```ini
[MIR_AI]
metadata_mode = optional
rimdocs_jsonl_path =
```

Supported modes:

| `metadata_mode` | Behavior |
|---|---|
| `required` | Authoritative RimDocs input is mandatory. The RimDocs-first overlap workflow runs and only missing fields are sent to metadata extraction. Use for formal MIR-AI requirements qualification or production workflows that require structured metadata. |
| `optional` | If RimDocs input is configured, use it and run the RimDocs-first workflow. If no RimDocs input is configured, continue ingestion and **skip structured metadata extraction**. The system does not pretend all 50 fields are missing. |
| `disabled` | Skip the structured metadata stage entirely, even if a RimDocs path is present. Useful for parsing/OCR/table/chunking/embedding or infrastructure-only runs. |

Examples:

### Full requirements / RimDocs required

```ini
metadata_mode = required
rimdocs_jsonl_path = C:\data\rimdocs.jsonl
```

### Ingest with RimDocs when available, otherwise continue

```ini
metadata_mode = optional
rimdocs_jsonl_path =
```

### Pure ingestion test, no structured metadata stage

```ini
metadata_mode = disabled
rimdocs_jsonl_path =
```

CLI `--rimdocs-jsonl` overrides `rimdocs_jsonl_path` for a batch run. For a single-file run, `--rimdocs-json` supplies the authoritative metadata object.

Changing `metadata_mode` changes the processing fingerprint, so a previously active generation is not incorrectly reused when metadata behavior changes.

## Safe production defaults

Recommended general-purpose server defaults:

```ini
source_auto_run = false
source_recursive = true
source_max_files = 0

object_list_enabled = false

enable_pdf = true
enable_docx = true
preferred_format = pdf

metadata_mode = optional

strict_source_selection = true
```

For a formal MIR-AI requirements qualification run, change only:

```ini
metadata_mode = required
rimdocs_jsonl_path = C:\data\rimdocs.jsonl
```

`source_auto_run=false` prevents a bare command from unexpectedly ingesting an entire NAS tree or S3 prefix. After deployment automation or a future UI supplies an explicit approved start action, it may set or invoke the configured-source mode deliberately.

## Future UI mapping

A future interface can expose these fields without changing ingestion code:

| UI control | Configuration |
|---|---|
| Source | `source_type = nas|s3` |
| NAS root | `docs_root` |
| S3 bucket/prefix | `[s3] bucket`, `prefix` |
| Include subfolders | `source_recursive` |
| Process all vs control list | `object_list_enabled` |
| Control table/schema/column | `object_list_*` |
| PDF enabled | `enable_pdf` |
| DOCX enabled | `enable_docx` |
| Preferred representation | `preferred_format` |
| Batch cap | `source_max_files` |
| Automatic configured-source run | `source_auto_run` |
| Structured metadata policy | `metadata_mode = required|optional|disabled` |
| RimDocs JSONL handover | `rimdocs_jsonl_path` |
| Worker/API limits | concurrency settings |
| Embedding/chunking policy | embedding/chunking settings |

Secrets must remain outside Git. `config.ini` is ignored by the repository; `config.mirai.example.ini` is the deployable template.
