# iam.tf
# =============================================================================
# INFRA-3 — Least-privilege IAM execution role for the app Lambda.
#
# SELF-CONTAINED on purpose (its own resources + outputs; no new variables
# needed). It only reads `local.name_prefix` / `local.tags` from the INFRA-1
# skeleton and the dynamodb.tf resource attributes. Do NOT move anything into
# variables.tf / outputs.tf / main.tf — this stays merge-conflict-free with the
# parallel cloudfront.tf work.
#
# Principle: the role grants EXACTLY the DynamoDB item/query actions the app
# needs, scoped to EXACTLY the one data table ARN + its GSI ARN pattern (from
# dynamodb.tf) and NOTHING wider — no `dynamodb:*`, no `Resource = "*"`. Plus
# write access to only the function's own CloudWatch log group.
#
# Secrets Manager is deliberately NOT granted: the app validates inbound JWTs
# against Cognito's PUBLIC JWKS (AUTH-2) and needs no secret at runtime. The M2M
# client secrets in Secrets Manager (cognito.tf) belong to the *calling agents*,
# not to this server. If a future task makes the server read a secret, add a
# tightly-scoped secretsmanager:GetSecretValue on that exact secret ARN here.
# =============================================================================

# --- Trust policy: only the Lambda service may assume this role. ---
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${local.name_prefix}-api-lambda"
  description        = "Execution role for the Spec Server app Lambda. Least-privilege: scoped DynamoDB item/query on the data table + its own log group only."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = local.tags
}

# --- Permissions policy (inline): DynamoDB data-plane on the exact table + its
# GSIs, and log writes to only this function's log group. ---
data "aws_iam_policy_document" "lambda_permissions" {

  # DynamoDB: the specific item/query actions the storage adapter uses
  # (STORAGE_ABSTRACTION_DEEPDIVE.md §3). No table-admin (Create/DeleteTable),
  # no Scan, no wildcard actions. Scoped to the base table AND its index ARN
  # pattern (Query/GetItem on a GSI targets the "<table-arn>/index/*" resource).
  statement {
    sid    = "DynamoDataPlane"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:TransactWriteItems",
      "dynamodb:ConditionCheckItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      # dynamodb.tf: the one data table...
      aws_dynamodb_table.app.arn,
      # ...and its GSIs (same value as output dynamodb_table_index_arn_pattern).
      "${aws_dynamodb_table.app.arn}/index/*",
    ]
  }

  # CloudWatch Logs: write ONLY into this function's own log group + streams.
  # CreateLogGroup is intentionally omitted — the group is precreated in
  # lambda.tf with an explicit 30-day retention, so the runtime only needs to
  # open streams and put events.
  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.lambda.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_permissions" {
  name   = "${local.name_prefix}-api-lambda-policy"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
output "lambda_role_arn" {
  description = "ARN of the app Lambda's least-privilege execution role."
  value       = aws_iam_role.lambda_exec.arn
}
