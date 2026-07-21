# outputs.tf
# Kept minimal this wave. Later tasks add: DynamoDB table names/ARNs (INFRA-2),
# Cognito pool/client IDs (AUTH-1), API endpoint URL (INFRA-5), CloudFront
# domain (INFRA-6). Never output secrets (client secrets live in Secrets Manager).

output "aws_region" {
  description = "Region the stack is deployed to."
  value       = var.aws_region
}

output "account_id" {
  description = "AWS account ID the stack is deployed to."
  value       = data.aws_caller_identity.current.account_id
}

output "monthly_budget_arn" {
  description = "ARN of the monthly cost budget."
  value       = aws_budgets_budget.monthly.arn
}

output "cost_alerts_topic_arn" {
  description = "SNS topic ARN receiving budget + cost-anomaly alerts."
  value       = aws_sns_topic.cost_alerts.arn
}
