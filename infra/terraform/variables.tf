variable "aws_region" {
  type = string
}
variable "name" {
  type = string
  default = "evidenceflow"
}
variable "vpc_id" {
  type = string
}
variable "private_subnet_ids" {
  type = list(string)
}
variable "internal_cidr_blocks" {
  type = list(string)
}
variable "acm_certificate_arn" {
  type = string
}
variable "container_image" {
  type = string
}
variable "database_url_secret_arn" {
  type = string
}
variable "azure_chat_secret_arns" {
  type = map(string)
  default = {}
}
variable "snowflake_secret_arns" {
  type = map(string)
  default = {}
}
variable "source_bucket" {
  type = string
}
variable "source_prefix" {
  type = string
}
variable "checkpoint_bucket_name" {
  type = string
}
variable "oidc_issuer" {
  type = string
}
variable "oidc_audience" {
  type = string
}
variable "oidc_jwks_url" {
  type = string
}
variable "snowflake_metadata_table" {
  type = string
  default = "DEV_RAW.RIMDOCS_CONTROL_STG.ALL_METADATA_JOINED_WITH_IA_CII"
}
variable "snowflake_entitlements_table" {
  type = string
}
variable "max_executor_tasks" {
  type = number
  default = 200
}
variable "task_cpu" {
  type = number
  default = 2048
}
variable "task_memory" {
  type = number
  default = 8192
}

# Exact internal browser origin used for presigned multipart PUT uploads.
# Avoid permissive '*' origins for authenticated enterprise applications.
variable "ui_origin" {
  type = string
  description = "HTTPS origin serving EvidenceFlow (example: https://evidenceflow.internal.example.com)"
  validation {
    condition = startswith(var.ui_origin, "https://")
    error_message = "ui_origin must be an HTTPS origin"
  }
}
