data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # "us.amazon.nova-lite-v1:0" (inference profile) -> "amazon.nova-lite-v1:0" (base model)
  base_model_id        = replace(var.bedrock_model_id, "/^(us|eu|apac|global)\\./", "")
  is_inference_profile = local.base_model_id != var.bedrock_model_id
  function_name        = "${var.project}-daily-report"
}

# ---------------------------------------------------------------------------
# SNS: email delivery
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "report" {
  name              = "${var.project}-report"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.report.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# IAM: least privilege (no managed *FullAccess policies)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = "${var.project}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "least-privilege"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadCosts"
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage"]
        Resource = "*" # Cost Explorer doesn't support resource-level permissions
      },
      {
        Sid    = "InvokeOneModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        # Only this model. An inference profile routes to several Regions, so the
        # base model is allowed in any Region, but only through this one profile.
        Resource = concat(
          ["arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/${local.base_model_id}"],
          local.is_inference_profile ? ["arn:${data.aws_partition.current.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_model_id}"] : []
        )
      },
      {
        Sid      = "PublishReport"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.report.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/build/lambda.zip"
  excludes    = ["__pycache__"]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "report" {
  function_name    = local.function_name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 60
  memory_size      = 256
  architectures    = ["arm64"] # Graviton: ~20% cheaper per GB-second

  environment {
    variables = {
      SNS_TOPIC_ARN    = aws_sns_topic.report.arn
      BEDROCK_MODEL_ID = var.bedrock_model_id
      LOOKBACK_DAYS    = tostring(var.lookback_days)
      ANOMALY_RATIO    = tostring(var.anomaly_ratio)
      ANOMALY_MIN_USD  = tostring(var.anomaly_min_usd)
    }
  }

  depends_on = [aws_iam_role_policy.lambda, aws_cloudwatch_log_group.lambda]
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler: daily trigger
# ---------------------------------------------------------------------------
resource "aws_iam_role" "scheduler" {
  name = "${var.project}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.report.arn
    }]
  })
}

resource "aws_scheduler_schedule" "daily" {
  name                         = "${var.project}-daily"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.report.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

# ---------------------------------------------------------------------------
# Monitoring: know when the cost monitor itself breaks
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project}-lambda-errors"
  alarm_description   = "The daily cost report Lambda failed."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.report.function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.report.arn]
}