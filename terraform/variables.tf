variable "project" {
  type    = string
  default = "finops-cost-optimizer"
}

variable "region" {
  description = "Region for Lambda, SNS and Bedrock. Cost Explorer is always called in us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "alert_email" {
  description = "Where the daily report is sent. You must click the confirmation link AWS emails you."
  type        = string
}

variable "bedrock_model_id" {
  description = <<-EOT
    Bedrock model or cross-Region inference profile ID. Default: Amazon Nova Lite
    (cheap, good at structured summaries). To use another model, copy its ID from
    Bedrock console -> Model catalog (or Cross-region inference) and paste it here.
    No code changes are needed: the Lambda uses the model-agnostic Converse API.
  EOT
  type        = string
  default     = "us.amazon.nova-lite-v1:0"
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression."
  type        = string
  default     = "cron(0 8 * * ? *)"
}

variable "schedule_timezone" {
  description = "Timezone for the schedule, e.g. UTC or Africa/Nairobi."
  type        = string
  default     = "UTC"
}

variable "lookback_days" {
  type    = number
  default = 7
}

variable "anomaly_ratio" {
  description = "Flag a service when daily cost is at least this many times its baseline."
  type        = number
  default     = 3
}

variable "anomaly_min_usd" {
  description = "...and the increase is at least this many dollars (filters out cent-level noise)."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  type    = number
  default = 30
}