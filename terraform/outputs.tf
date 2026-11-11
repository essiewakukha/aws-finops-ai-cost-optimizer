output "region" {
  value = var.region
}

output "lambda_function_name" {
  value = aws_lambda_function.report.function_name
}

output "sns_topic_arn" {
  value = aws_sns_topic.report.arn
}

output "schedule" {
  value = "${var.schedule_expression} (${var.schedule_timezone})"
}

output "bedrock_model_id" {
  value = var.bedrock_model_id
}

output "next_step" {
  value = "Check ${var.alert_email} and CONFIRM the SNS subscription, then run: ./scripts/invoke.sh"
}