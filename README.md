# chunk_vectors / MIR-AI

The repository contains the existing EPOD ingestion path plus the additive MIR-AI implementation under `mir_ai/` and `mir_ai_main.py`.

For MIR-AI operations start with:

- `docs/MIR_AI_CONFIGURATION_CONTROL_PLANE.md` — config-driven NAS/S3, all-files/control-table, PDF/DOCX and metadata policy.
- `docs/MIR_AI_PRODUCTION_RESILIENCE.md` — retry, resume, detailed diagnostics, ingestion status and preflight.
- `docs/MIR_AI_100_DOCUMENT_QUALIFICATION.md` — 1 -> 10 -> 100 document qualification procedure and accuracy measurement boundary.
- `docs/MIR_AI_OPERATIONS.md` — runtime/operational guidance.
- `docs/MIR_AI_RELEASE_READINESS.md` — release boundaries/open decisions.
- `docs/MIR_AI_REQUIREMENTS_TRACEABILITY.md` — requirements traceability.

Copy `config.mirai.example.ini` to the server as `config.ini` and populate credentials locally. `config.ini` is gitignored and credentials must not be committed.

Typical controlled run:

```powershell
python mir_ai_main.py --config config.ini --init-db
python mir_ai_main.py --config config.ini --plan
python mir_ai_main.py --config config.ini --preflight-only
python mir_ai_main.py --config config.ini
python mir_ai_main.py --config config.ini --status
```

A successful ingestion run is not by itself evidence of the contractual extraction-accuracy target. Formal accuracy requires comparison against accepted labelled/golden truth as described in the qualification runbook.
