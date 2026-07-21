# invites.tf
# =============================================================================
# HA-2 — Invite-only human signup: the dedicated invites store + the Cognito
# PreSignUp burn Lambda.
#
# SELF-CONTAINED on purpose (its own variables + outputs). It reads only
# `local.name_prefix` / `local.tags` (variables.tf) and, to grant least-priv
# access, the app-Lambda execution role from iam.tf (READ reference, not an
# edit) — exactly as reaper.tf references sibling resources. Nothing is moved
# into variables.tf / outputs.tf / main.tf, so this stays merge-conflict-free
# with the parallel HA-3 pool work.
#
# IMPORTANT — this file does NOT edit cognito.tf. The PreSignUp trigger is wired
# on the pool at the passkey cutover by the parallel HA-3 agent / orchestrator,
# who reads the `presignup_lambda_arn` OUTPUT below. This file only grants
# `cognito-idp.amazonaws.com` permission to invoke the function (scoped to the
# pool ARN passed via var.cognito_user_pool_arn) so the wiring is ready.
#
# DEDICATED table (NOT the app single-table `${name_prefix}-app`): invites are an
# auth artifact, not a storage-abstraction entity. Key = code_hash: only the
# SHA-256 HASH of the 128-bit code is ever stored (never plaintext), so a table
# dump cannot recover a live code. On-demand billing, PITR, and a TTL attribute
# (`expires_at`, ~14d) garbage-collect spent/expired invites.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to HA-2; kept in this file on purpose)
# ---------------------------------------------------------------------------
variable "cognito_user_pool_arn" {
  description = <<-EOT
    ARN of the Cognito user pool that will invoke the PreSignUp Lambda. Passed in
    (rather than read from cognito.tf) so this file NEVER edits cognito.tf — the
    HA-3 pool cutover owns that file. Empty (default) skips the invoke permission
    so `terraform validate` passes before the pool ARN is known; wire it at the
    passkey cutover alongside the pool's pre_sign_up trigger = presignup_lambda_arn.
  EOT
  type        = string
  default     = ""
}

variable "invite_log_retention_days" {
  description = "CloudWatch retention (days) for the PreSignUp Lambda log group. Finite so logs never accrue cost forever."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------
locals {
  invites_table_name = "${local.name_prefix}-invites"
  presignup_name     = "${local.name_prefix}-presignup"
}

# ---------------------------------------------------------------------------
# The invites table (dedicated; NOT the app single-table store).
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "invites" {
  name         = local.invites_table_name
  billing_mode = "PAY_PER_REQUEST" # on-demand: no idle cost, scales to zero
  hash_key     = "code_hash"

  # Only the key attribute is declared; every other attribute (status,
  # email_binding, approved, created_at, expires_at) is schema-less.
  attribute {
    name = "code_hash"
    type = "S"
  }

  # TTL: DynamoDB garbage-collects spent/expired invites once expires_at passes.
  # The PreSignUp burn ALSO bounds on expires_at in its conditional write, so an
  # expired-but-not-yet-swept invite still cannot be redeemed (TTL delete lags).
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Invites are low-value + short-lived, but PITR is cheap insurance against an
  # accidental bulk delete and mirrors the app table's posture.
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# PreSignUp Lambda deployment artifact — zipped locally by the archive provider
# so offline `terraform validate`/`plan` succeed with no build tooling or AWS.
# ---------------------------------------------------------------------------
data "archive_file" "presignup" {
  type        = "zip"
  source_dir  = "${path.module}/presignup_lambda"
  output_path = "${path.module}/.terraform/spec-server-presignup.zip" # gitignored .terraform/
}

# ---------------------------------------------------------------------------
# Log group — explicit + finite retention, scoped in the IAM policy below.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "presignup" {
  name              = "/aws/lambda/${local.presignup_name}"
  retention_in_days = var.invite_log_retention_days

  tags = local.tags
}

# ---------------------------------------------------------------------------
# PreSignUp Lambda execution role — LEAST PRIVILEGE.
# It only BURNS invites (UpdateItem, conditional) and writes its own logs. It
# needs NO SES (it sends no email) and NO Cognito admin API (approval is by
# group, done by an admin later — not here).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "presignup_assume" {
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

resource "aws_iam_role" "presignup" {
  name               = "${local.presignup_name}-exec"
  description        = "Execution role for the invite-burn PreSignUp Lambda. Least-privilege: conditional UpdateItem on the invites table + its own log group only. No SES, no Cognito admin."
  assume_role_policy = data.aws_iam_policy_document.presignup_assume.json

  tags = local.tags
}

data "aws_iam_policy_document" "presignup_permissions" {
  # Consume an invite: a single conditional UpdateItem on the invites table.
  # Scoped to exactly this table ARN — no GSIs, no wildcards, no other table.
  statement {
    sid       = "BurnInvite"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.invites.arn]
  }

  # Write ONLY into this function's own log group + streams.
  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.presignup.arn}:*"]
  }
}

resource "aws_iam_role_policy" "presignup_permissions" {
  name   = "${local.presignup_name}-policy"
  role   = aws_iam_role.presignup.id
  policy = data.aws_iam_policy_document.presignup_permissions.json
}

# ---------------------------------------------------------------------------
# The PreSignUp function (python3.12, arm64).
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "presignup" {
  function_name = local.presignup_name
  description   = "Cognito PreSignUp trigger: atomically burns a single-use invite code (conditional write, no double-spend), enforces optional email-binding, auto-confirms + auto-verifies email. Adds NO group (pending until an admin approves)."

  role          = aws_iam_role.presignup.arn
  architectures = ["arm64"]
  runtime       = "python3.12"
  handler       = "handler.handler"

  filename         = data.archive_file.presignup.output_path
  source_code_hash = data.archive_file.presignup.output_base64sha256

  memory_size = 128
  timeout     = 10

  environment {
    variables = {
      INVITES_TABLE = aws_dynamodb_table.invites.name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.presignup,
    aws_iam_role_policy.presignup_permissions,
  ]

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Allow the Cognito user pool to invoke the PreSignUp Lambda. Gated on
# var.cognito_user_pool_arn so validate passes before the pool ARN is known; the
# HA-3 cutover supplies it (and sets the pool's pre_sign_up = presignup_lambda_arn).
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "cognito_invoke_presignup" {
  count = var.cognito_user_pool_arn != "" ? 1 : 0

  statement_id  = "AllowCognitoPreSignUpInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.presignup.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = var.cognito_user_pool_arn
}

# ---------------------------------------------------------------------------
# Grant the APP Lambda (iam.tf role) least-priv access to the invites table so
# the admin endpoint (POST/GET /api/v1/admin/invites) can mint + list invites.
# Attached here (referencing the sibling role) rather than editing iam.tf, so
# both files stay conflict-free. The app mints (PutItem), reads (GetItem) and
# lists (Query on the status GSI is not present -> Scan) invites; scoped to this
# table ARN only.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_invites_access" {
  statement {
    sid    = "AppManageInvites"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.invites.arn]
  }
}

resource "aws_iam_role_policy" "app_invites_access" {
  name   = "${local.name_prefix}-app-invites-access"
  role   = aws_iam_role.lambda_exec.id # iam.tf — the app Lambda role (read ref)
  policy = data.aws_iam_policy_document.app_invites_access.json
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "invites_table_name" {
  description = "Name of the dedicated invites DynamoDB table. Wire to the app Lambda as INVITES_TABLE so the admin endpoint can mint/list invites."
  value       = aws_dynamodb_table.invites.name
}

output "invites_table_arn" {
  description = "ARN of the invites DynamoDB table."
  value       = aws_dynamodb_table.invites.arn
}

output "presignup_lambda_arn" {
  description = "ARN of the invite-burn PreSignUp Lambda. The HA-3 pool cutover wires this as the user pool's pre_sign_up trigger (in cognito.tf, which THIS file never edits) and passes the pool ARN back via var.cognito_user_pool_arn so the invoke permission is created."
  value       = aws_lambda_function.presignup.arn
}

output "presignup_lambda_name" {
  description = "Name of the PreSignUp Lambda function."
  value       = aws_lambda_function.presignup.function_name
}
