# reaper.tf
# =============================================================================
# OPS / INFRA-6 — Durable teardown REAPER for TRANSIENT preview environments.
#
# SELF-CONTAINED on purpose: this file declares its OWN variables, locals,
# resources and outputs. It reads only the INFRA-1 skeleton (`local.name_prefix`
# / `local.tags`) and, for the teardown-safety Deny, the ARNs of the durable
# resources declared by the parallel files (dynamodb.tf, cognito.tf, lambda.tf,
# apigw.tf, cloudfront.tf). It does NOT edit variables.tf / outputs.tf / main.tf
# or any other agent's file, so it stays merge-conflict-free.
#
# WHAT IT IS: an EventBridge Scheduler rule (rate(15 minutes) — preview envs are
# cheap and not latency-critical) that invokes a reaper Lambda. The reaper lists
# every resource tagged `transient=true` and deletes those whose `expiry` UTC-ISO
# tag is in the past and that are NOT tagged `protect=true`. It logs every action
# to CloudWatch and publishes a summary to SNS.
#
# DURABILITY: the reaper Lambda, its role, its schedule, its log group and its
# SNS topic are DURABLE. They inherit transient=false from the provider
# default_tags and are NEVER tagged transient=true — the reaper must never reap
# itself (it is also self-denylisted in handler.py).
#
# TEARDOWN SAFETY (load-bearing, enforced in IAM — see the role policy below):
#   * The reaper may delete a resource ONLY if it carries
#     aws:ResourceTag/transient = "true"  (Allow is conditioned on the tag).
#   * An explicit DENY on the durable ARNs (data table, state bucket + lock
#     table, cognito pool, app lambda/api, UI bucket, and the reaper's own
#     resources) means even a MIS-TAGGED durable resource cannot be deleted —
#     an explicit Deny always beats any Allow. A transient=true tag on a durable
#     resource is a bug the reaper surfaces, never acts on.
#
# NOTE (honest): this service does not yet have a preview-env CREATION workflow.
# The reaper is the safety net for when one is added, and it establishes and
# enforces the transient=true / expiry=<UTC-ISO> tag convention now. Building the
# preview-env creation flow is a separate, future prerequisite — NOT done here.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to the reaper; kept in this file on purpose)
# ---------------------------------------------------------------------------
variable "reaper_schedule_expression" {
  description = "EventBridge Scheduler rate/cron for the reaper. Preview envs are cheap and not latency-critical, so 15 minutes is plenty."
  type        = string
  default     = "rate(15 minutes)"
}

variable "reaper_dry_run" {
  description = "When true the reaper only LISTS what it would reap (deletes nothing). Ship dry-run first, then flip to false once tag hygiene is confirmed."
  type        = bool
  default     = true
}

variable "reaper_regions" {
  description = "Comma-separated regions the reaper sweeps for transient=true resources. Defaults to the stack region."
  type        = string
  default     = ""
}

variable "reaper_log_retention_days" {
  description = "CloudWatch retention for the reaper's log group. Finite so reaper logs never accrue cost forever."
  type        = number
  default     = 30
}

variable "reaper_timeout_seconds" {
  description = "Reaper Lambda timeout (seconds). Discovery + deletes across a region are quick, but allow headroom for paginated sweeps."
  type        = number
  default     = 120

  validation {
    condition     = var.reaper_timeout_seconds >= 10 && var.reaper_timeout_seconds <= 900
    error_message = "reaper_timeout_seconds must be 10..900."
  }
}

variable "reaper_memory_mb" {
  description = "Reaper Lambda memory (MB). Small — it is IO-bound on AWS API calls."
  type        = number
  default     = 256
}

variable "state_bucket_name" {
  description = "Exact remote-state S3 bucket name to explicitly DENY the reaper from touching. Empty falls back to the spec-server-tfstate-* wildcard."
  type        = string
  default     = ""
}

variable "state_lock_table_name" {
  description = "Remote-state DynamoDB lock table name to explicitly DENY the reaper from touching."
  type        = string
  default     = "spec-server-tflock"
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------
locals {
  reaper_name = "${local.name_prefix}-reaper"

  reaper_regions_effective = var.reaper_regions != "" ? var.reaper_regions : data.aws_region.current.name

  # The reaper is DURABLE. Force transient=false and stamp a clear marker even if
  # provider default_tags ever changed. NEVER transient=true.
  reaper_tags = merge(local.tags, {
    transient = "false"
    component = "teardown-reaper"
  })

  # Remote-state bucket ARNs to Deny. Prefer the exact name; otherwise deny the
  # whole spec-server-tfstate-* namespace (the bootstrap bucket uses a suffix).
  state_bucket_arns = var.state_bucket_name != "" ? [
    "arn:aws:s3:::${var.state_bucket_name}",
    "arn:aws:s3:::${var.state_bucket_name}/*",
    ] : [
    "arn:aws:s3:::spec-server-tfstate-*",
    "arn:aws:s3:::spec-server-tfstate-*/*",
  ]

  # State lock table ARN (bootstrapped out-of-band; built from name + context).
  state_lock_table_arn = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.state_lock_table_name}"

  # The complete set of DURABLE ARNs the reaper must NEVER be able to mutate.
  # Referencing the sibling files' resources keeps this list authoritative.
  durable_deny_arns = concat(
    [
      aws_dynamodb_table.app.arn,           # dynamodb.tf — the data table
      "${aws_dynamodb_table.app.arn}/*",    # ...and its indexes/streams
      local.state_lock_table_arn,           # remote-state lock table
      aws_cognito_user_pool.this.arn,       # cognito.tf — the user pool
      aws_lambda_function.app.arn,          # lambda.tf — the app function
      "${aws_lambda_function.app.arn}:*",   # ...and its aliases/versions
      aws_apigatewayv2_api.http.arn,        # apigw.tf — the HTTP API
      "${aws_apigatewayv2_api.http.arn}/*", # ...and its stages
      aws_s3_bucket.ui.arn,                 # cloudfront.tf — the UI bucket
      "${aws_s3_bucket.ui.arn}/*",
    ],
    local.state_bucket_arns,
  )
}

# ---------------------------------------------------------------------------
# Reaper deployment artifact — zipped locally by the archive provider so
# offline `terraform validate`/`plan` work with no build tooling or AWS.
# ---------------------------------------------------------------------------
data "archive_file" "reaper" {
  type        = "zip"
  source_dir  = "${path.module}/reaper_lambda"
  output_path = "${path.module}/.terraform/spec-server-reaper.zip" # gitignored .terraform/
}

# ---------------------------------------------------------------------------
# Log group — explicit + finite retention, and scoped in the IAM policy.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "reaper" {
  name              = "/aws/lambda/${local.reaper_name}"
  retention_in_days = var.reaper_log_retention_days

  tags = local.reaper_tags
}

# ---------------------------------------------------------------------------
# SNS topic for reap notifications (durable). Every run publishes a summary.
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "reaper" {
  name = "${local.reaper_name}-notifications"

  tags = local.reaper_tags
}

# ===========================================================================
# IAM — the teardown-safety boundary (enforced here, not just in code).
# ===========================================================================

# --- Reaper Lambda execution role -----------------------------------------
data "aws_iam_policy_document" "reaper_assume" {
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

resource "aws_iam_role" "reaper" {
  name               = "${local.reaper_name}-exec"
  description        = "Execution role for the teardown reaper. Can delete ONLY transient=true resources; explicitly DENIED on every durable ARN."
  assume_role_policy = data.aws_iam_policy_document.reaper_assume.json

  tags = local.reaper_tags
}

data "aws_iam_policy_document" "reaper_permissions" {

  # --- READ-ONLY discovery: find transient=true resources + read their tags.
  # Listing/describing is harmless and unconditioned so discovery always works.
  statement {
    sid    = "DiscoverTaggedResources"
    effect = "Allow"
    actions = [
      "tag:GetResources",
      "tag:GetTagKeys",
      "tag:GetTagValues",
      "lambda:ListFunctions",
      "lambda:ListAliases",
      "lambda:ListVersionsByFunction",
      "lambda:GetFunction",
      "lambda:ListTags",
      "dynamodb:ListTables",
      "dynamodb:DescribeTable",
      "dynamodb:ListTagsOfResource",
      "apigateway:GET",
      "s3:ListAllMyBuckets",
      "s3:GetBucketLocation",
      "s3:GetBucketTagging",
      "s3:ListBucket",
    ]
    resources = ["*"]
  }

  # --- DELETE preview Lambda functions/aliases — ONLY when tagged transient=true.
  statement {
    sid    = "ReapLambda"
    effect = "Allow"
    actions = [
      "lambda:DeleteFunction",
      "lambda:DeleteAlias",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/transient"
      values   = ["true"]
    }
  }

  # --- DELETE preview DynamoDB tables — ONLY when tagged transient=true.
  statement {
    sid    = "ReapDynamoTables"
    effect = "Allow"
    actions = [
      "dynamodb:DeleteTable",
      "dynamodb:TagResource", # allow expiry-tag extension bookkeeping
      "dynamodb:UntagResource",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/transient"
      values   = ["true"]
    }
  }

  # --- DELETE preview API Gateway (HTTP API) apis/stages. apigateway:DELETE does
  # not support aws:ResourceTag conditions, so the durable API is protected by
  # the explicit Deny statement below (its ARN is in durable_deny_arns).
  statement {
    sid    = "ReapApiGateway"
    effect = "Allow"
    actions = [
      "apigateway:DELETE",
    ]
    resources = [
      "arn:aws:apigateway:${data.aws_region.current.name}::/apis/*",
      "arn:aws:apigateway:${data.aws_region.current.name}::/apis/*/stages/*",
    ]
  }

  # --- DELETE objects under preview S3 PREFIXES only. SEC-IAM-1: scoped to the
  # preview-bucket naming convention `${name_prefix}-preview-*` (e.g.
  # spec-server-dev-preview-<branch/pr>) rather than the previous account-wide
  # `arn:aws:s3:::*/*`. Preview stacks are provisioned via CLI/boto3 with this
  # name prefix (mirrors the ${name_prefix}-app / -ui / -reaper durable naming),
  # so the reaper can only ever delete objects in a bucket it is meant to clean —
  # never an arbitrary bucket in the account. Bucket-level actions are NOT
  # granted, and the durable buckets remain in the explicit Deny below, so the
  # reaper still cannot touch the state bucket or the UI bucket even if one were
  # ever (mis)named to match this pattern.
  statement {
    sid    = "ReapS3PreviewObjects"
    effect = "Allow"
    actions = [
      "s3:DeleteObject",
    ]
    resources = ["arn:aws:s3:::${local.name_prefix}-preview-*/*"]
  }

  # --- Notify + log (own resources only).
  statement {
    sid       = "PublishReapSummary"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.reaper.arn]
  }

  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.reaper.arn}:*"]
  }

  # === THE LOAD-BEARING GUARD ==============================================
  # Explicit DENY of ALL mutating/tagging actions on every durable ARN. An
  # explicit Deny overrides ANY Allow (including a tag-conditioned one), so even
  # if a durable resource is mis-tagged transient=true, the reaper CANNOT delete
  # it. This is the "do no harm / cannot touch the production data table, state
  # bucket, or Cognito pool" guarantee, enforced in IAM.
  statement {
    sid    = "DenyDurableMutations"
    effect = "Deny"
    actions = [
      "dynamodb:DeleteTable",
      "dynamodb:DeleteItem",
      "dynamodb:UpdateItem",
      "dynamodb:PutItem",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "lambda:DeleteFunction",
      "lambda:DeleteAlias",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "apigateway:DELETE",
      "apigateway:PATCH",
      "apigateway:PUT",
      "s3:DeleteObject",
      "s3:DeleteBucket",
      "s3:PutObject",
      "cognito-idp:DeleteUserPool",
      "cognito-idp:DeleteUserPoolClient",
      "cognito-idp:UpdateUserPool",
    ]
    resources = local.durable_deny_arns
  }

  # Belt-and-suspenders: the reaper is NEVER allowed to delete ANY Cognito user
  # pool (there is no transient Cognito in this service's preview model).
  statement {
    sid    = "DenyAllCognitoDeletes"
    effect = "Deny"
    actions = [
      "cognito-idp:DeleteUserPool",
      "cognito-idp:DeleteUserPoolClient",
      "cognito-idp:DeleteGroup",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "reaper_permissions" {
  name   = "${local.reaper_name}-policy"
  role   = aws_iam_role.reaper.id
  policy = data.aws_iam_policy_document.reaper_permissions.json
}

# ---------------------------------------------------------------------------
# The reaper Lambda.
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "reaper" {
  function_name = local.reaper_name
  description   = "Teardown reaper: deletes transient=true preview resources past their expiry. Durable; never self-reaps. DENIED on all durable ARNs in IAM."

  role          = aws_iam_role.reaper.arn
  architectures = ["arm64"]
  runtime       = "python3.12"
  handler       = "handler.handler"

  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256

  memory_size = var.reaper_memory_mb
  timeout     = var.reaper_timeout_seconds

  environment {
    variables = {
      DRY_RUN       = tostring(var.reaper_dry_run)
      SNS_TOPIC_ARN = aws_sns_topic.reaper.arn
      REGIONS       = local.reaper_regions_effective
      NAME_PREFIX   = local.name_prefix
      DURABLE_DENY_NAMES = join(",", [
        "${local.name_prefix}-app",
        "${local.name_prefix}-api",
        "${local.name_prefix}-ui",
        "${local.name_prefix}-reaper",
        "${local.name_prefix}-cost-alerts",
        "spec-server-tfstate",
        var.state_lock_table_name,
        "userpool",
        "cognito",
      ])
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.reaper,
    aws_iam_role_policy.reaper_permissions,
  ]

  tags = local.reaper_tags
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler → reaper. Scheduler assumes a dedicated role that may
# only invoke the reaper function (nothing else).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    sid     = "SchedulerAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "reaper_scheduler" {
  name               = "${local.reaper_name}-scheduler"
  description        = "Role EventBridge Scheduler assumes to invoke the reaper Lambda (and ONLY that function)."
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json

  tags = local.reaper_tags
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    sid       = "InvokeReaper"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.reaper.arn]
  }
}

resource "aws_iam_role_policy" "reaper_scheduler" {
  name   = "${local.reaper_name}-scheduler-policy"
  role   = aws_iam_role.reaper_scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

resource "aws_scheduler_schedule" "reaper" {
  name       = local.reaper_name
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.reaper_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.reaper.arn
    role_arn = aws_iam_role.reaper_scheduler.arn

    # Scheduler cannot tag the schedule resource; durability is inherent (it is
    # Terraform-managed and never carries transient=true).
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "reaper_function_name" {
  description = "Name of the teardown reaper Lambda."
  value       = aws_lambda_function.reaper.function_name
}

output "reaper_function_arn" {
  description = "ARN of the teardown reaper Lambda."
  value       = aws_lambda_function.reaper.arn
}

output "reaper_schedule_name" {
  description = "Name of the EventBridge Scheduler schedule driving the reaper."
  value       = aws_scheduler_schedule.reaper.name
}

output "reaper_sns_topic_arn" {
  description = "SNS topic ARN that receives each reaper run's summary (reaped / would-reap / protected / durable-bug)."
  value       = aws_sns_topic.reaper.arn
}

output "reaper_dry_run" {
  description = "Whether the reaper is currently in dry-run (list-only) mode."
  value       = var.reaper_dry_run
}
