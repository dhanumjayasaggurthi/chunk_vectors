# MIR-AI source selection and existing S3 configuration compatibility

Branch: `feature/MIR-AI-2026-09-12`

## Purpose

This document defines the source-selection behavior for environments where an
authoritative PostgreSQL control table contains extensionless logical object
IDs while NAS or S3 may contain both PDF and DOCX representations.

It also records the supported S3 credential/configuration pattern so deployment
does not require an infrastructure change.

## Existing S3 architecture is preserved

MIR-AI accepts the existing `[S3]` or `[s3]` keys:

```ini
[s3]
access_key_id =
secret_access_key =
bucket =
prefix =
region = us-east-1
profile =
session_token =
temp_dir = C:\Temp\epod_downloads
```

Credential resolution is:

1. If both `access_key_id` and `secret_access_key` are populated, use the
   explicit static credentials and optional `session_token`.
2. Otherwise, if `profile` is populated, use that AWS profile.
3. Otherwise use the normal boto3 credential chain (environment, task/instance
   role, shared credentials, and other supported AWS mechanisms).
4. If no profile exists and only part of a static credential set is supplied,
   fail with a configuration error rather than attempting an ambiguous login.

A profile therefore continues to work even if an old server config contains
one unused/incomplete static credential field.

No credential values are logged.

## Secrets are not stored in Git

`config.ini` is intentionally ignored by Git. Real PostgreSQL, AWS, Google, and
Azure credentials must be populated on the server or injected through the
existing enterprise secret mechanism.

The repository contains `config.mirai.example.ini` with the exact supported
shape and placeholders. Copy it to `config.ini` on the server and populate
secrets there.

Do **not** add a real credential-bearing `config.ini` to the repository.

## Authoritative object-list mode

When enabled, MIR-AI does not ingest arbitrary PDF/DOCX files discovered under
the NAS root or S3 prefix. It first loads logical IDs from the configured table:

```ini
[MIR_AI]
object_list_enabled = true
object_list_schema = regulatory
object_list_table = mirai_obj_list
object_list_id_column = <actual extensionless-ID column>
```

The column name is intentionally configurable because the approved production
column name must come from the deployed database rather than being invented in
code.

Only IDs returned by that query are eligible for ingestion.

## Format policy

```ini
[MIR_AI]
enable_pdf = true
enable_docx = true
preferred_format = pdf
strict_source_selection = true
object_match_case_sensitive = false
```

Selection behavior:

| PDF enabled | DOCX enabled | Physical sources | Selection |
|---|---|---|---|
| yes | yes | PDF + DOCX | PDF |
| yes | yes | PDF only | PDF |
| yes | yes | DOCX only | DOCX |
| yes | no | PDF + DOCX | PDF |
| yes | no | PDF only | PDF |
| yes | no | DOCX only | `FORMAT_DISABLED` |
| no | yes | PDF + DOCX | DOCX |
| no | yes | DOCX only | DOCX |
| no | yes | PDF only | `FORMAT_DISABLED` |
| no | no | any | startup configuration error |

Old binary `.doc` is not treated as `.docx`.

## Listing order cannot change the decision

S3 and NAS discovery first builds a lightweight candidate map for only the
requested logical IDs. It does not process the first representation it happens
to see.

Therefore an S3 listing that returns:

```text
STUDY-001.docx
...
STUDY-001.pdf
```

still selects `STUDY-001.pdf` when PDF is preferred.

Only one selected representation is downloaded from S3.

## Duplicate and missing handling

If two physical PDFs match one logical object ID, MIR-AI does not guess:

```text
AMBIGUOUS_SOURCE
```

If the object list requests an ID but no PDF/DOCX is found:

```text
SOURCE_NOT_FOUND
```

If a source exists only in a disabled format:

```text
FORMAT_DISABLED
```

These statuses appear in batch output and summary counts.

## Stable logical document identity

When object-list mode is enabled, the database document key is based on the
extensionless logical object ID, not on the physical PDF/DOCX path.

For example:

```text
STUDY-001.pdf
STUDY-001.docx
```

both map to the internal logical document key:

```text
mirai-object:study-001
```

The selected physical file remains the `source_url`, and the source-version
fingerprint includes the selected format and physical path. This prevents a
format-policy change from being incorrectly skipped while avoiding duplicate
logical documents.

## Recommended qualification configuration

For the initial 100-document run:

```ini
[MIR_AI]
enable_pdf = true
enable_docx = true
preferred_format = pdf

object_list_enabled = true
object_list_schema = <actual schema>
object_list_table = mirai_obj_list
object_list_id_column = <actual ID column>

strict_source_selection = true
object_match_case_sensitive = false
```

Use a dedicated MIR-AI UAT output schema. The object-list table may reside in a
different schema; `object_list_schema` controls where it is read from.

## Server deployment sequence

1. Pull `feature/MIR-AI-2026-09-12`.
2. Copy `config.mirai.example.ini` to `config.ini` on the server.
3. Populate real credentials locally; do not commit the populated file.
4. Set the actual object-list schema/table/column.
5. Initialize the MIR-AI schema.
6. Run one document, then ten, then the controlled 100-document qualification.
7. Review `SOURCE_NOT_FOUND`, `FORMAT_DISABLED`, and `AMBIGUOUS_SOURCE` before
   treating the batch as complete.
