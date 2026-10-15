# EvidenceFlow Enterprise — SRE playbook and acceptance gates

## Proposed SLOs (to negotiate with platform owners)

- API submission p95 < 2 seconds for direct ID lists of 1–5,000 (excludes file upload).
- 99.9% API monthly availability target; separate external OCR/LLM availability from core API.
- No silent loss of accepted IDs: every accepted unique ID ends in a terminal documented state, or remains visibly pending with recoverable state.
- 100% accepted critical extracted values have document page + verified source snippet. Missing or ambiguous values are not accepted automatically.
- Job recovery after an ungraceful worker termination without restarting previously checkpointed page chunks.
- Bulk throughput target **must** specify page-count, scanned-page fraction, document-size distribution, external-service quotas, and completion deadline. Do not promise 1m docs/24h solely on queue capacity.

## Autoscaling

Track SQS oldest-message age, visible messages per executor, completion rate, P95 duration per stage, provider 429s, DB CPU/locks, transient failures, and cost/validated-document. Scale worker slots only below approved Textract and Azure TPM/TPS quotas. Use the interactive lane to protect small user requests, and reserve some executor capacity for bulk to prevent starvation. Terraform has a conservative **scale-out baseline only**; configure quota-aware scale-in and alarms after performance testing.

## Retry and state integrity

- DB stage tasks have a renewable fenced lease, `failures` count, and backoff. SQS messages are acknowledged only after a durable task transition.
- A crash after S3 checkpoint upload but before DB commit produces harmless rework; a crash after DB commit is skipped on retry.
- Textract `ClientRequestToken` is deterministic for the document chunk and extraction profile. Poller stores its operation ID. It never holds compute while OCR is pending.
- User cancellation stops new task claims and prevents document finalization; in-flight API/provider calls may still finish, creating bounded unavoidable costs. Source results already committed remain accessible under authorization.
- **SQS DLQ does not replace the database FAILED status.** Malformed queue messages appear in DLQ; expected permanent failures are persisted against the individual document or job export.

## Runbooks

| Symptom | Diagnose | Remediate |
|---|---|---|
| Queue oldest-message age rises | ECS desired/running count, DB availability, Azure 429s | Scale within approved quotas; remove external throttle; drain intelligently |
| API slow | RDS connection saturation, huge OFFSET, authenticated request volume | Use RDS Proxy/PgBouncer for high worker counts, tighten pools, paginate, async export, add replicas |
| Resolver backlog rises | Snowflake warehouse queue, entitlement joins, 200-ID batch cost | Add resolver capacity; switch to staged set-based join if measured bottleneck |
| 1600-page document stalls | Manifest, page chunk states, Textract operation state, disk limits | Retry only failed stage, reconcile SQS, inspect page count & OCR quota |
| Many `REVIEW_REQUIRED` | Low-text pages, regex miss, conflicting dates, Azure unavailable | Sample evidence and gold labels, fix OCR/rules, never auto-approve |
| Export pending | Export task state, DB query plan, S3 checkpoint writes | Retry export, avoid synchronous million-row HTTP responses |
| Task lease expires repeatedly | Worker OOM/CPU/disk, heartbeat DB errors | Right-size Fargate task, shorten chunk size, inspect logs/traces |
| Input upload COMPLETING forever | Multipart S3 object `head_object`, input_uploads row | Retry `/complete`; reconciliation recognizes already-completed object |
| DB failover | DB recovery, DB pool reconnects, in-flight leases | Keep API returning 503, restore connection, let stage leases expire/reclaim |

## Production acceptance tests (minimum)

1. **Corporate IAM:** issue/revoke authorized and unauthorized OIDC scopes; prove cross-tenant and cross-job access denied. Verify document-level ACLs for source links.
2. **Safety:** use real versions of PBRER PDFs with approval metadata. Validate International Birth Date against inflammation-related IBD mentions.
3. **Gold-set quality:** stratified sample of native, scanned, partially scanned, multilingual, unusual sections, overlapping dates, missing manufacturer, and >1600 pages. Measure field accuracy, false positives, evidence precision/recall, groundedness and human-review rate.
4. **Chaos:** kill workers mid-download, mid-checkpoint, after OCR submission and during final DB commit; restart workers. No dropped or silently duplicated result.
5. **Resumable upload:** disconnect midway through a >100MB CSV multipart upload, upload again, kill API after S3 complete but before DB commit, reconcile. Verify deduped IDs.
6. **Load:** 10k → 100k → 1m IDs across >=50 concurrent users. Track Snowflake warehouse credit, RDS concurrency, SQS age, CPU, Azure tokens/min, Textract pages/min and budget.
7. **Recovery:** perform Multi-AZ DB failover/restore drill; verify restored job/task data and checkpoint S3 references. Approve RPO/RTO.
8. **Compliance:** data-region and retention review, regulatory document classification, cloud vendor use approval, logs/privacy review, incident response and deletion controls.

## Known development gaps (intentionally visible)

- JnJ Snowflake/Azure/S3/IDP/Textract were **not available** for a real end-to-end integration test here. Adapter contracts need integration validation.
- Terraform expects pre-existing corporate RDS, VPC, KMS/storage policy, identity provider, approved ACL table, secrets and image registry. It is a deployment baseline, not a turnkey release.
- Azure summaries are labeled drafts with evidence pages; full semantic claim verification/human sign-off may be required for regulated decisions.
- The 200-ID Snowflake lookup strategy and regional quota/concurrency policy are not benchmarked at million-document load. Bulk staging and token-bucket throttling are measured evolution options.
- Fully automated operational alarms/scale-in, CI/CD vulnerability scanning, strict resource policies, long-term report retention and audit export are organization-specific additions needed for production.