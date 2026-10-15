# Repository delivery scope

This branch contains the verified architecture/operations documentation and a **small reference scaffold**. It is **not** a mirror of the 37-file enterprise implementation ZIP produced during architecture work.

## Included
- FastAPI demo routes and request validation
- Typed extraction evidence contracts
- Fail-closed page evidence checking
- Restartable atomic local page checkpoints (1,601-page regression)
- Automated unit tests and GitHub Actions CI

## Not included / not certified
- Production Snowflake/S3/Azure adapters from the separate project ZIP
- Distributed SQS/ECS worker, PostgreSQL leases, async Textract workflows
- Full results UI, corporate SSO, tenant authorization
- Live cloud integration, load tests, performance SLOs, regulatory qualification

These must be copied from the separately provided enterprise ZIP, integrated, independently reviewed, and re-tested before production use. In particular, do not deploy this demo API as an authenticated enterprise application. The in-memory jobs are intentionally illustrative and do not run in the background.

## Run
```bash
cd evidenceflow_enterprise
python -m pip install -e '.[dev]'
python -m pytest -q
uvicorn backend.app:app --port 8000
```
