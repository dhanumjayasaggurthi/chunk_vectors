terraform {
  required_version = ">= 1.6"
  required_providers { aws = {source="hashicorp/aws",version="~> 5.0"} }
}
provider "aws" { region=var.aws_region }
data "aws_caller_identity" "current" {}

# Application input/result/checkpoint bucket; regulated retention policy must be reviewed.
resource "aws_s3_bucket" "checkpoint" { bucket=var.checkpoint_bucket_name }
resource "aws_s3_bucket_public_access_block" "checkpoint" {
  bucket=aws_s3_bucket.checkpoint.id
  block_public_acls=true
  block_public_policy=true
  ignore_public_acls=true
  restrict_public_buckets=true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "checkpoint" {
  bucket=aws_s3_bucket.checkpoint.id
  rule { apply_server_side_encryption_by_default { sse_algorithm="AES256" } }
}
resource "aws_s3_bucket_versioning" "checkpoint" {
  bucket=aws_s3_bucket.checkpoint.id
  versioning_configuration { status="Enabled" }
}
# Browser uploads are direct to private S3 using short-lived presigned URLs.
# CORS must expose ETag so the frontend can resume multipart uploads.
resource "aws_s3_bucket_cors_configuration" "checkpoint" {
  bucket = aws_s3_bucket.checkpoint.id
  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = [var.ui_origin]
    expose_headers  = ["ETag"]
    max_age_seconds = 300
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "checkpoint" {
  bucket=aws_s3_bucket.checkpoint.id
  rule {
    id="abort-incomplete-multipart"
    status="Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation=7 }
  }
}
resource "aws_sqs_queue" "dlq" {
  name="${var.name}-dlq"
  message_retention_seconds=1209600
  sqs_managed_sse_enabled=true
}
resource "aws_sqs_queue" "interactive" {
  name="${var.name}-interactive"
  visibility_timeout_seconds=600
  message_retention_seconds=1209600
  receive_wait_time_seconds=20
  sqs_managed_sse_enabled=true
  redrive_policy=jsonencode({deadLetterTargetArn=aws_sqs_queue.dlq.arn,maxReceiveCount=5})
}
resource "aws_sqs_queue" "tasks" {
  name="${var.name}-tasks"
  visibility_timeout_seconds=600
  message_retention_seconds=1209600
  receive_wait_time_seconds=20
  sqs_managed_sse_enabled=true
  redrive_policy=jsonencode({deadLetterTargetArn=aws_sqs_queue.dlq.arn,maxReceiveCount=5})
}
resource "aws_cloudwatch_log_group" "app" {
  name="/ecs/${var.name}"
  retention_in_days=30
}
resource "aws_ecs_cluster" "main" { name=var.name }

# Deployment assumes an existing, managed, Multi-AZ PostgreSQL cluster with a
# least-privileged application user; its full DATABASE_URL is stored in Secrets Manager.
resource "aws_iam_role" "execution" {
  name="${var.name}-task-execution"
  assume_role_policy=jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Principal={Service="ecs-tasks.amazonaws.com"},Action="sts:AssumeRole"}]})
}
resource "aws_iam_role_policy_attachment" "execution" {
  role=aws_iam_role.execution.name
  policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  role=aws_iam_role.execution.id
  policy=jsonencode({Version="2012-10-17",Statement=[{Effect="Allow",Action=["secretsmanager:GetSecretValue"],Resource=concat([var.database_url_secret_arn],values(var.azure_chat_secret_arns),values(var.snowflake_secret_arns))}]})
}
resource "aws_iam_role" "runtime" {
  name="${var.name}-runtime"
  assume_role_policy=aws_iam_role.execution.assume_role_policy
}
resource "aws_iam_role_policy" "runtime" {
  role=aws_iam_role.runtime.id
  policy=jsonencode({Version="2012-10-17",Statement=[
    {Effect="Allow",Action=["s3:GetObject","s3:GetObjectVersion"],Resource="arn:aws:s3:::${var.source_bucket}/${var.source_prefix}*"},
    {Effect="Allow",Action=["s3:GetObject","s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],Resource="${aws_s3_bucket.checkpoint.arn}/*"},
    {Effect="Allow",Action=["s3:ListBucketMultipartUploads","s3:ListBucket"],Resource=aws_s3_bucket.checkpoint.arn},
    {Effect="Allow",Action=["sqs:SendMessage","sqs:ReceiveMessage","sqs:DeleteMessage","sqs:GetQueueAttributes","sqs:ChangeMessageVisibility"],Resource=[aws_sqs_queue.tasks.arn,aws_sqs_queue.interactive.arn]},
    {Effect="Allow",Action=["textract:StartDocumentTextDetection","textract:GetDocumentTextDetection"],Resource="*"}
  ]})
}

locals {
  shared_env=[
    {name="APP_MODE",value="live"},
    {name="SQS_TASK_URL",value=aws_sqs_queue.tasks.url},
    {name="SQS_INTERACTIVE_URL",value=aws_sqs_queue.interactive.url},
    {name="CHECKPOINT_BUCKET",value=aws_s3_bucket.checkpoint.bucket},
    {name="ALLOWED_S3_BUCKETS",value=var.source_bucket},
    {name="ALLOWED_S3_PREFIXES",value=var.source_prefix},
    {name="SF_METADATA_TABLE",value=var.snowflake_metadata_table},
    {name="SF_ENTITLEMENTS_TABLE",value=var.snowflake_entitlements_table},
    {name="OIDC_ISSUER",value=var.oidc_issuer},
    {name="OIDC_AUDIENCE",value=var.oidc_audience},
    {name="OIDC_JWKS_URL",value=var.oidc_jwks_url},
    {name="MAX_PDF_MB",value="1200"},
    {name="MAX_PDF_PAGES",value="20000"},
    {name="CHUNK_PAGES",value="64"},
    {name="OCR_ENGINE",value="textract_async"}
  ]
  shared_secrets=concat(
    [{name="DATABASE_URL",valueFrom=var.database_url_secret_arn}],
    [for k,arn in var.azure_chat_secret_arns:{name=k,valueFrom=arn}],
    [for k,arn in var.snowflake_secret_arns:{name=k,valueFrom=arn}]
  )
  log_options={awslogs-group=aws_cloudwatch_log_group.app.name,awslogs-region=var.aws_region,awslogs-stream-prefix="service"}
}
resource "aws_ecs_task_definition" "api" {
  family="${var.name}-api"
  requires_compatibilities=["FARGATE"]
  network_mode="awsvpc"
  cpu=1024
  memory=2048
  execution_role_arn=aws_iam_role.execution.arn
  task_role_arn=aws_iam_role.runtime.arn
  container_definitions=jsonencode([{
    name="api",image=var.container_image,essential=true,
    portMappings=[{containerPort=8000,protocol="tcp"}],
    environment=local.shared_env,secrets=local.shared_secrets,
    logConfiguration={logDriver="awslogs",options=local.log_options},
    readonlyRootFilesystem=false
  }])
}
resource "aws_ecs_task_definition" "executor" {
  family="${var.name}-executor"
  requires_compatibilities=["FARGATE"]
  network_mode="awsvpc"
  cpu=var.task_cpu
  memory=var.task_memory
  ephemeral_storage { size_in_gib=30 }
  execution_role_arn=aws_iam_role.execution.arn
  task_role_arn=aws_iam_role.runtime.arn
  container_definitions=jsonencode([{
    name="executor",image=var.container_image,essential=true,
    command=["python","-m","backend.enterprise_worker","--role","executor"],
    environment=local.shared_env,secrets=local.shared_secrets,
    logConfiguration={logDriver="awslogs",options=local.log_options},
    readonlyRootFilesystem=false
  }])
}
resource "aws_ecs_task_definition" "dispatcher" {
  family="${var.name}-dispatcher"
  requires_compatibilities=["FARGATE"]
  network_mode="awsvpc"
  cpu=1024
  memory=2048
  execution_role_arn=aws_iam_role.execution.arn
  task_role_arn=aws_iam_role.runtime.arn
  container_definitions=jsonencode([{
    name="dispatcher",image=var.container_image,essential=true,
    command=["python","-m","backend.enterprise_worker","--role","dispatcher"],
    environment=local.shared_env,secrets=local.shared_secrets,
    logConfiguration={logDriver="awslogs",options=local.log_options}
  }])
}
resource "aws_security_group" "api" {
  name="${var.name}-api"
  vpc_id=var.vpc_id
  ingress {
    from_port=8000
    to_port=8000
    protocol="tcp"
    security_groups=[aws_security_group.alb.id]
  }
  egress {
    from_port=0
    to_port=0
    protocol="-1"
    cidr_blocks=["0.0.0.0/0"]
  }
}
resource "aws_security_group" "alb" {
  name="${var.name}-alb"
  vpc_id=var.vpc_id
  ingress {
    from_port=443
    to_port=443
    protocol="tcp"
    cidr_blocks=var.internal_cidr_blocks
  }
  egress {
    from_port=0
    to_port=0
    protocol="-1"
    cidr_blocks=["0.0.0.0/0"]
  }
}
resource "aws_security_group" "workers" {
  name="${var.name}-workers"
  vpc_id=var.vpc_id
  egress {
    from_port=0
    to_port=0
    protocol="-1"
    cidr_blocks=["0.0.0.0/0"]
  }
}
resource "aws_lb" "internal" {
  name="${var.name}-internal"
  internal=true
  load_balancer_type="application"
  security_groups=[aws_security_group.alb.id]
  subnets=var.private_subnet_ids
}
resource "aws_lb_target_group" "api" {
  name="${var.name}-api"
  port=8000
  protocol="HTTP"
  target_type="ip"
  vpc_id=var.vpc_id
  health_check {
    path="/api/v1/health/ready"
    healthy_threshold=2
    unhealthy_threshold=3
  }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn=aws_lb.internal.arn
  port=443
  protocol="HTTPS"
  ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn=var.acm_certificate_arn
  default_action {
    type="forward"
    target_group_arn=aws_lb_target_group.api.arn
  }
}
resource "aws_ecs_service" "api" {
  name="${var.name}-api"
  cluster=aws_ecs_cluster.main.id
  task_definition=aws_ecs_task_definition.api.arn
  desired_count=2
  launch_type="FARGATE"
  network_configuration {
    subnets=var.private_subnet_ids
    security_groups=[aws_security_group.api.id]
    assign_public_ip=false
  }
  load_balancer {
    target_group_arn=aws_lb_target_group.api.arn
    container_name="api"
    container_port=8000
  }
  depends_on=[aws_lb_listener.https]
}
resource "aws_ecs_service" "dispatcher" {
  name="${var.name}-dispatcher"
  cluster=aws_ecs_cluster.main.id
  task_definition=aws_ecs_task_definition.dispatcher.arn
  desired_count=2
  launch_type="FARGATE"
  network_configuration {
    subnets=var.private_subnet_ids
    security_groups=[aws_security_group.workers.id]
    assign_public_ip=false
  }
}
resource "aws_ecs_service" "executor" {
  name="${var.name}-executor"
  cluster=aws_ecs_cluster.main.id
  task_definition=aws_ecs_task_definition.executor.arn
  desired_count=2
  launch_type="FARGATE"
  network_configuration {
    subnets=var.private_subnet_ids
    security_groups=[aws_security_group.workers.id]
    assign_public_ip=false
  }
}
resource "aws_appautoscaling_target" "executor" {
  max_capacity=var.max_executor_tasks
  min_capacity=2
  resource_id="service/${aws_ecs_cluster.main.name}/${aws_ecs_service.executor.name}"
  scalable_dimension="ecs:service:DesiredCount"
  service_namespace="ecs"
}
resource "aws_appautoscaling_policy" "executor_scale_out" {
  name="${var.name}-scale-out"
  policy_type="StepScaling"
  resource_id=aws_appautoscaling_target.executor.resource_id
  scalable_dimension=aws_appautoscaling_target.executor.scalable_dimension
  service_namespace=aws_appautoscaling_target.executor.service_namespace
  step_scaling_policy_configuration {
    adjustment_type="ChangeInCapacity"
    cooldown=120
    step_adjustment {
      metric_interval_lower_bound=0
      scaling_adjustment=10
    }
  }
}
resource "aws_cloudwatch_metric_alarm" "queue_backlog" {
  alarm_name="${var.name}-queue-backlog"
  namespace="AWS/SQS"
  metric_name="ApproximateNumberOfMessagesVisible"
  dimensions={QueueName=aws_sqs_queue.tasks.name}
  statistic="Average"
  period=60
  evaluation_periods=2
  threshold=500
  comparison_operator="GreaterThanThreshold"
  alarm_actions=[aws_appautoscaling_policy.executor_scale_out.arn]
}
output "internal_ui" { value="https://${aws_lb.internal.dns_name}" }
output "queue_url" { value=aws_sqs_queue.tasks.url }
output "checkpoint_bucket" { value=aws_s3_bucket.checkpoint.bucket }
