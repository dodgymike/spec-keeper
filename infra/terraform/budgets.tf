# budgets.tf
# Cost guardrails — load-bearing. This service MUST stay cheap and surface any
# surprise spend loudly.
#
#   1. SNS topic + email subscription for all cost alerts.
#   2. AWS Budgets monthly cost budget at var.budget_monthly_usd, alerting at
#      80% and 100% of FORECASTed spend (forecast, so we hear about it before
#      the money is gone).
#   3. AWS Cost Anomaly Detection monitor (whole account, by service) + a daily
#      subscription that pushes anomalies to the same SNS topic.
#
# NOTE: Budgets + Cost Anomaly Detection are global/us-east-1-scoped services.
# Cost data is only available in us-east-1; keep this stack there (default).

# --- SNS topic for cost alerts ------------------------------------------------
resource "aws_sns_topic" "cost_alerts" {
  name = "${local.name_prefix}-cost-alerts"
}

resource "aws_sns_topic_subscription" "cost_alerts_email" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Allow AWS Budgets and Cost Explorer (anomaly detection) to publish to the topic.
data "aws_iam_policy_document" "cost_alerts_publish" {
  statement {
    sid     = "AllowBudgetsPublish"
    effect  = "Allow"
    actions = ["SNS:Publish"]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }

    resources = [aws_sns_topic.cost_alerts.arn]
  }

  statement {
    sid     = "AllowCostAnomalyPublish"
    effect  = "Allow"
    actions = ["SNS:Publish"]

    principals {
      type        = "Service"
      identifiers = ["costalerts.amazonaws.com"]
    }

    resources = [aws_sns_topic.cost_alerts.arn]
  }
}

resource "aws_sns_topic_policy" "cost_alerts" {
  arn    = aws_sns_topic.cost_alerts.arn
  policy = data.aws_iam_policy_document.cost_alerts_publish.json
}

# --- Monthly cost budget ------------------------------------------------------
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_monthly_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert at 80% of the FORECAST for the month.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.cost_alerts.arn]
    subscriber_email_addresses = [var.alert_email]
  }

  # Alert at 100% of the FORECAST for the month.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.cost_alerts.arn]
    subscriber_email_addresses = [var.alert_email]
  }
}

# --- Cost Anomaly Detection ---------------------------------------------------
# Cost Explorer / Cost Anomaly Detection is a us-east-1-only API, so these
# resources are pinned to the us_east_1 aliased provider regardless of the
# stack's primary region (var.aws_region), which may be e.g. eu-west-1.
# Gated off by default: AWS caps dimensional anomaly monitors per account, and
# this account may already have one (e.g. from another stack). The Budget above
# already provides cost alerting; enable this only where a free monitor slot
# exists (set enable_cost_anomaly = true).
resource "aws_ce_anomaly_monitor" "service" {
  count             = var.enable_cost_anomaly ? 1 : 0
  provider          = aws.us_east_1
  name              = "${local.name_prefix}-anomaly-monitor"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "daily" {
  count     = var.enable_cost_anomaly ? 1 : 0
  provider  = aws.us_east_1
  name      = "${local.name_prefix}-anomaly-sub"
  frequency = "DAILY"

  monitor_arn_list = [aws_ce_anomaly_monitor.service[0].arn]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.cost_alerts.arn
  }

  # Only alert on anomalies whose total impact clears this small USD threshold,
  # so day-to-day noise stays quiet on a pennies-scale service.
  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = ["5"]
    }
  }

  depends_on = [aws_sns_topic_policy.cost_alerts]
}
