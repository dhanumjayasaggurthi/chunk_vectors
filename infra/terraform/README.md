# AWS deployment baseline

This Terraform stack provisions a private HTTPS load balancer, ECS API/dispatcher/executor services, an encrypted checkpoint bucket, an encrypted SQS queue with DLQ, task IAM roles, logging, and an initial scale-out rule. It assumes the corporate network, PostgreSQL Multi-AZ, private connectivity/NAT/VPC endpoints, source S3 bucket, identity provider, image registry, and required Secrets Manager entries already exist.

**Do not apply to production without review.** The starter has no verified corporate-specific Terraform values; queue and model quotas, ALB ingress CIDRs, IAM restrictions, security scans, service scaling, cost budgets, DB roles, and backup/restore tests must be signed off. In particular, the code requires PostgreSQL schema migration (`alembic upgrade head`) before services are started.

For a first controlled environment:

1. Provision a non-superuser PostgreSQL app role in a managed Multi-AZ cluster. Provide the complete SQLAlchemy `DATABASE_URL` in a Secrets Manager secret.
2. Store JnJ Azure deployment fields as individual environment secrets (`AZURE_OPENAI_CHAT_API_BASE`, `_API_KEY`, `_API_VERSION`, `_DEPLOYMENT`, `_MODEL`, and GPT-5 equivalents if used). Store Snowflake credentials and `SNOWFLAKE_PRIVATE_KEY_PEM` as secrets.
3. Provision the source S3 bucket with **versioning enabled** and object access limited to permitted keys.
4. Configure a corporate OIDC resource-server app and a document entitlement table; never expose this app with generic shared tokens.
5. Push the Docker image to ECR, set Terraform variables, run `terraform plan`, obtain approval, then `terraform apply`.
6. Set `ui_origin` to the exact approved internal HTTPS origin. Terraform configures checkpoint-bucket multipart upload CORS and exposes `ETag`. Apply Alembic migrations as a one-time migration task, then deploy services.
7. Set CloudWatch alarms for DLQ size, oldest message age, service error rates, failed OCR, DB connections, LLM throttling, and checkpoint-storage growth. Add scale-in and quota-aware scaling after measurements.

Operational guidance: `task_cpu/task_memory` must be benchmarked on scanned 1600-page PDFs. Some 1.2GB source PDFs may require a larger ephemeral volume. Default ECS ephemeral storage here is 30GB. Ensure private subnets can reach approved Snowflake, Azure, AWS Textract, and S3 endpoints.
