# signups.tf
# =============================================================================
# HA-7 — Public request->approve signup queue (bird Path A), BACKEND + INFRA.
#
# The heavy public self-service path, decoupled behind SQS so the public
# POST /api/v1/signup intake does ZERO existence work (the enumeration-privacy
# crux): the app Lambda enqueues a branch-free message; an async worker Lambda
# does all state-dependent work (Cognito existence check, write the `requested`
# row, SES the single-use magic link). GET /api/v1/validate redeems the link;
# the admin bridge approves (email-validated only) and provisions synchronously
# by minting an HA-2 invite + SES-ing the join link (that provisioning lives in
# the app Lambda, not here).
#
# SELF-CONTAINED on purpose (its own variables + outputs). It reads only
# `local.name_prefix` / `local.tags` (variables.tf), the app Lambda role
# (iam.tf `aws_iam_role.lambda_exec`, READ reference), the Cognito pool
# (cognito.tf `aws_cognito_user_pool.this`, READ reference — same as iam.tf),
# and from ses.tf: `aws_iam_policy.ses_send`, `var.ses_from_address`,
# `aws_sesv2_configuration_set.auth`. Nothing is moved into variables.tf /
# outputs.tf / main.tf, so this stays merge-conflict-free.
#
# DEFERRED (documented, not built): the S3 WORM audit bucket + peppered ip/ua
# fingerprints and their Secrets-Manager pepper. `SIGNUP_PEPPER` below is an
# OPTIONAL plain var (default "") used to HMAC the email_hash; unset => the app
# and worker fall back to a plain SHA-256 (fine for dev). Keep the SQS
# decoupling + Turnstile + rate-limit + magic-link — the core of "the queue".
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (own — kept in this file to stay merge-conflict-free)
# ---------------------------------------------------------------------------
variable "turnstile_secret" {
  description = "Cloudflare Turnstile server-side secret. When non-empty the app verifies POST /signup tokens server-side (a failed/absent token is dropped as a bot). Empty (default) => the Turnstile check is skipped so local/dev works. Sensitive."
  type        = string
  default     = ""
  sensitive   = true
}

variable "signup_pepper" {
  description = "Optional pepper for the email_hash HMAC (defeats offline dictionary reversal of a leaked table). MUST be identical for the app Lambda and the worker (both read this var). Empty (default) => a plain SHA-256 hash (acceptable for dev; set a strong random value in prod). Sensitive."
  type        = string
  default     = ""
  sensitive   = true
}

variable "signup_ratelimit_max" {
  description = "Max POST /signup requests per source IP per window before a 429 (in-app floor; the CDN edge is the durable limiter)."
  type        = number
  default     = 5
}

variable "signup_ratelimit_window_seconds" {
  description = "The fixed per-IP rate-limit window in seconds."
  type        = number
  default     = 60
}

variable "signup_validate_base_url" {
  description = "Base URL the magic-link validation link is built from (e.g. https://spec.elasticninja.com). The worker appends /validate?token=<link>. Empty => the worker uses a relative link (dev)."
  type        = string
  default     = ""
}

variable "signup_resend_cap" {
  description = "Per-email resend cap the worker enforces async (never an enumeration oracle)."
  type        = number
  default     = 3
}

variable "signup_enforce_origin" {
  description = "When true, POST /signup requires the Origin/Referer to match signup_allowed_origins. Off by default (dev)."
  type        = bool
  default     = false
}

variable "signup_allowed_origins" {
  description = "Comma-separated exact origin allow-list for POST /signup (used only when signup_enforce_origin=true), e.g. https://spec.elasticninja.com."
  type        = string
  default     = ""
}

variable "signup_log_retention_days" {
  description = "CloudWatch retention (days) for the signup worker Lambda log group. Finite so logs never accrue cost forever."
  type        = number
  default     = 30
}

variable "signup_dlq_max_receive_count" {
  description = "Deliveries attempted before a poison intake message is moved to the DLQ."
  type        = number
  default     = 5
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------
locals {
  signups_table_name   = "${local.name_prefix}-signups"
  signup_rl_table_name = "${local.name_prefix}-signup-ratelimit"
  signup_worker_name   = "${local.name_prefix}-signup-worker"
}

# ---------------------------------------------------------------------------
# The signups table (dedicated; NOT the app single-table store). The plaintext
# email lives ONLY as an SSE-KMS attribute VALUE — never a key or GSI segment;
# the key is email_hash. GSI1 drives the admin "list by status, newest-first".
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "signups" {
  name         = local.signups_table_name
  billing_mode = "PAY_PER_REQUEST" # on-demand: no idle cost, scales to zero
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  # GSI1 status index: gsi1pk = STATUS#<status>, gsi1sk = created_at (numeric).
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "N"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  # TTL garbage-collects unvalidated `requested` rows (7d) and used/expired token
  # items (24h). The redeem path ALSO bounds on expires_at in its conditional
  # write, so an expired-but-unswept token can never be redeemed (TTL lags).
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# The per-IP fixed-window rate-limit counter table (ephemeral; no PITR needed).
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "signup_ratelimit" {
  name         = local.signup_rl_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# SQS intake queue + DLQ — the decoupling boundary. The public intake never
# blocks on existence work: it SendMessages here; the worker drains it.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "signup_intake_dlq" {
  name                      = "${local.name_prefix}-signup-intake-dlq"
  message_retention_seconds = 1209600 # 14d — max, so poison messages are inspectable
  sqs_managed_sse_enabled   = true

  tags = local.tags
}

resource "aws_sqs_queue" "signup_intake" {
  name                       = "${local.name_prefix}-signup-intake"
  visibility_timeout_seconds = 180    # >= worker timeout, so a slow record isn't double-delivered
  message_retention_seconds  = 345600 # 4d
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.signup_intake_dlq.arn
    maxReceiveCount     = var.signup_dlq_max_receive_count
  })

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Worker Lambda deployment artifact — zipped locally by the archive provider so
# offline `terraform validate`/`plan` succeed with no build tooling or AWS. The
# source dir vendors a copy of app/signup.py as signup.py (mirrors the bird
# "common/ copied into each lambda zip" packaging).
# ---------------------------------------------------------------------------
data "archive_file" "signup_worker" {
  type        = "zip"
  source_dir  = "${path.module}/signup_worker_lambda"
  output_path = "${path.module}/.terraform/spec-server-signup-worker.zip"
}

resource "aws_cloudwatch_log_group" "signup_worker" {
  name              = "/aws/lambda/${local.signup_worker_name}"
  retention_in_days = var.signup_log_retention_days

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Worker execution role — LEAST PRIVILEGE. It: reads/writes the signups table,
# ListUsers on exactly this Cognito pool (existence check), sends SES (attached
# below), consumes the intake SQS queue, and writes its own logs. Nothing else.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "signup_worker_assume" {
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

resource "aws_iam_role" "signup_worker" {
  name               = "${local.signup_worker_name}-exec"
  description        = "Execution role for the signup intake worker Lambda. Least-privilege: signups-table item ops, ListUsers on the one pool, SES send, consume the intake SQS queue, own log group."
  assume_role_policy = data.aws_iam_policy_document.signup_worker_assume.json

  tags = local.tags
}

data "aws_iam_policy_document" "signup_worker_permissions" {
  # Signups table: conditional create + read + transition. Base table + its GSI
  # (the worker itself does not Query, but scoping to both is harmless and future
  # -proof). No table-admin, no Scan, no wildcard.
  statement {
    sid    = "SignupsTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [
      aws_dynamodb_table.signups.arn,
      "${aws_dynamodb_table.signups.arn}/index/*",
    ]
  }

  # Cognito existence check — ListUsers filtered by email, scoped to EXACTLY the
  # one pool ARN (never "*"). This is the async, off-the-observable-path branch.
  statement {
    sid       = "CognitoExistenceCheck"
    effect    = "Allow"
    actions   = ["cognito-idp:ListUsers"]
    resources = [aws_cognito_user_pool.this.arn]
  }

  # Consume the intake queue (the event-source mapping polls on the role's behalf)
  # + read the DLQ redrive target attributes.
  statement {
    sid    = "ConsumeIntakeQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.signup_intake.arn]
  }

  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.signup_worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "signup_worker_permissions" {
  name   = "${local.signup_worker_name}-policy"
  role   = aws_iam_role.signup_worker.id
  policy = data.aws_iam_policy_document.signup_worker_permissions.json
}

# SES send — reuse the HA-6 least-privilege managed policy (ses.tf). The worker
# emails the single-use magic link to the requester.
resource "aws_iam_role_policy_attachment" "signup_worker_ses" {
  role       = aws_iam_role.signup_worker.name
  policy_arn = aws_iam_policy.ses_send.arn
}

# ---------------------------------------------------------------------------
# The worker function (python3.12, arm64, scales to zero).
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "signup_worker" {
  function_name = local.signup_worker_name
  description   = "Signup intake worker: drains the intake SQS queue, checks Cognito ListUsers by email, writes the `requested` row + SES magic link for new emails (or a you-already-have-an-account notice), off the observable HTTP path (enumeration-privacy crux)."

  role          = aws_iam_role.signup_worker.arn
  architectures = ["arm64"]
  runtime       = "python3.12"
  handler       = "handler.handler"

  filename         = data.archive_file.signup_worker.output_path
  source_code_hash = data.archive_file.signup_worker.output_base64sha256

  memory_size = 256
  timeout     = 30

  environment {
    variables = {
      SIGNUPS_TABLE            = aws_dynamodb_table.signups.name
      SIGNUP_PEPPER            = var.signup_pepper
      SIGNUP_USER_POOL_ID      = aws_cognito_user_pool.this.id
      SIGNUP_VALIDATE_BASE_URL = var.signup_validate_base_url
      SIGNUP_RESEND_CAP        = tostring(var.signup_resend_cap)
      SES_FROM_ADDRESS         = var.ses_from_address
      SES_CONFIG_SET           = aws_sesv2_configuration_set.auth.configuration_set_name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.signup_worker,
    aws_iam_role_policy.signup_worker_permissions,
  ]

  tags = local.tags
}

# Event-source mapping: SQS -> worker, with partial-batch-failure reporting so a
# single poison/failing record is retried/DLQ'd without reprocessing (and
# re-emailing) the rest of the batch.
resource "aws_lambda_event_source_mapping" "signup_worker" {
  event_source_arn                   = aws_sqs_queue.signup_intake.arn
  function_name                      = aws_lambda_function.signup_worker.arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 5
  function_response_types            = ["ReportBatchItemFailures"]
}

# ---------------------------------------------------------------------------
# Grant the APP Lambda (iam.tf role) least-priv access to the signup resources
# so the public intake can enqueue, validate can redeem, and the admin bridge
# can list/approve/reject + provision. Attached here (referencing the sibling
# role) rather than editing iam.tf, so both files stay conflict-free.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "app_signup_access" {
  # Enqueue the branch-free intake message.
  statement {
    sid       = "AppEnqueueIntake"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.signup_intake.arn]
  }

  # Redeem tokens (validate) + list/approve/reject/provision (admin bridge).
  statement {
    sid    = "AppSignupsTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.signups.arn,
      "${aws_dynamodb_table.signups.arn}/index/*",
    ]
  }

  # Per-IP rate-limit counter for the public routes (atomic fixed-window incr).
  statement {
    sid       = "AppRateLimitCounter"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.signup_ratelimit.arn]
  }
}

resource "aws_iam_role_policy" "app_signup_access" {
  name   = "${local.name_prefix}-app-signup-access"
  role   = aws_iam_role.lambda_exec.id # iam.tf — the app Lambda role (read ref)
  policy = data.aws_iam_policy_document.app_signup_access.json
}

# The app Lambda now SES-emails the approve join link, so it needs the HA-6 send
# policy too (reused, least-privilege — scoped to the identity + config set).
resource "aws_iam_role_policy_attachment" "app_ses_send" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.ses_send.arn
}

# ---------------------------------------------------------------------------
# Outputs — wire these into the app Lambda's environment (lambda.tf).
# ---------------------------------------------------------------------------
output "signups_table_name" {
  description = "Name of the signups DynamoDB table. Wire to the app Lambda as SIGNUPS_TABLE."
  value       = aws_dynamodb_table.signups.name
}

output "signup_ratelimit_table_name" {
  description = "Name of the per-IP rate-limit counter table. Wire to the app Lambda as SIGNUP_RATELIMIT_TABLE."
  value       = aws_dynamodb_table.signup_ratelimit.name
}

output "signup_intake_queue_url" {
  description = "URL of the SQS intake queue. Wire to the app Lambda as SIGNUP_INTAKE_QUEUE_URL."
  value       = aws_sqs_queue.signup_intake.url
}

output "signup_intake_queue_arn" {
  description = "ARN of the SQS intake queue (worker event source)."
  value       = aws_sqs_queue.signup_intake.arn
}

output "signup_intake_dlq_url" {
  description = "URL of the SQS intake DLQ (poison messages land here)."
  value       = aws_sqs_queue.signup_intake_dlq.url
}

output "signup_worker_lambda_name" {
  description = "Name of the signup intake worker Lambda."
  value       = aws_lambda_function.signup_worker.function_name
}
