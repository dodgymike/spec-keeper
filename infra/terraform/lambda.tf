# lambda.tf
# =============================================================================
# INFRA-3 — The Spec Server app Lambda (Flask via a WSGI adapter).
#
# SELF-CONTAINED on purpose (its own variables + outputs). It reads only
# `local.name_prefix` / `local.tags` (INFRA-1), the dynamodb.tf table resource,
# the cognito.tf user-pool resources, the IAM role from iam.tf, and the JWT
# audience local from apigw.tf. Do NOT move anything into variables.tf /
# outputs.tf / main.tf — keeps this conflict-free with parallel cloudfront.tf.
#
# Cost posture: arm64 (cheaper + faster/watt), small memory default, scales to
# zero (no idle cost), explicit 30-day log retention (never-expire is a cost
# leak). Package small for fast cold starts.
#
# CODE ARTIFACT — PLACEHOLDER. To make `terraform validate`/`plan`/first-`apply`
# work WITHOUT a build pipeline, the function is deployed from a tiny committed
# bootstrap (`lambda_placeholder/wsgi_lambda.py`) zipped by `archive_file`.
# INFRA-4 (build pipeline) + the app's WSGI-handler task produce the REAL
# artifact (Flask wrapped by Mangum/aws-wsgi). Override at deploy time by
# setting `var.lambda_zip_path` to a prebuilt zip; leave it empty to use the
# committed placeholder. Either way `handler = "wsgi_lambda.handler"`.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to INFRA-3; kept in this file on purpose)
# ---------------------------------------------------------------------------

variable "lambda_zip_path" {
  description = "Path to a prebuilt Lambda deployment zip (produced by INFRA-4). Leave empty (default) to build+deploy the committed placeholder bootstrap so offline `terraform validate`/`plan` succeed without a real build."
  type        = string
  default     = ""
}

variable "lambda_memory_mb" {
  description = "App Lambda memory (MB). Small default — this is a light Flask/DynamoDB API. More memory also buys proportional CPU if cold starts need it."
  type        = number
  default     = 512

  validation {
    condition     = var.lambda_memory_mb >= 128 && var.lambda_memory_mb <= 3008
    error_message = "lambda_memory_mb must be between 128 and 3008."
  }
}

variable "lambda_timeout_seconds" {
  description = "App Lambda timeout (seconds). Should sit under the API Gateway 30s integration cap; ~15s is ample for DynamoDB-backed requests."
  type        = number
  default     = 15

  validation {
    condition     = var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 29
    error_message = "lambda_timeout_seconds must be 1..29 (stay under the API Gateway 30s cap)."
  }
}

variable "lambda_log_retention_days" {
  description = "CloudWatch retention for the app Lambda log group. Explicit + finite so logs never accrue cost forever."
  type        = number
  default     = 30
}

# SEC-DOS-2 — per-authenticated-principal (per Cognito `sub`) API throttle. The
# app enforces a per-`sub` fixed-window limit AFTER the JWT is verified, on the
# /api/v1 data-plane, so one token/tenant cannot starve the single stage-wide
# API Gateway budget (apigw.tf default_route_settings) shared across all tokens.
# It reuses the signup-ratelimit DynamoDB table + its existing app-Lambda IAM
# (signups.tf `AppRateLimitCounter` already grants dynamodb:UpdateItem) under a
# distinct `apisub#` key namespace — NO new table/IAM. Generous defaults sit well
# above normal agent usage; the limiter FAILS OPEN (unset table or any DDB error
# => allow) so it can never throttle a legitimate agent.
variable "api_ratelimit_max" {
  description = "Max authenticated /api/v1 requests per verified `sub` per window before a 429 (SEC-DOS-2). Generous by design — well above normal agent usage. Global spec-admins are exempt."
  type        = number
  default     = 120
}

variable "api_ratelimit_window_seconds" {
  description = "The per-`sub` API-throttle fixed window in seconds (SEC-DOS-2)."
  type        = number
  default     = 10
}

# ---------------------------------------------------------------------------
# Deployment artifact: prebuilt zip if provided, else the committed placeholder
# zipped locally by the archive provider (no AWS / no build tooling needed).
# ---------------------------------------------------------------------------
locals {
  use_prebuilt_zip = var.lambda_zip_path != ""

  # Terraform conditionals short-circuit, so the unused branch (which would
  # reference a non-existent file or a count=0 element) is never evaluated.
  lambda_package_path = local.use_prebuilt_zip ? var.lambda_zip_path : data.archive_file.placeholder[0].output_path
  lambda_package_hash = local.use_prebuilt_zip ? filebase64sha256(var.lambda_zip_path) : data.archive_file.placeholder[0].output_base64sha256
}

data "archive_file" "placeholder" {
  count = local.use_prebuilt_zip ? 0 : 1

  type        = "zip"
  source_dir  = "${path.module}/lambda_placeholder"
  output_path = "${path.module}/.terraform/spec-server-placeholder.zip" # under gitignored .terraform/
}

# ---------------------------------------------------------------------------
# Log group — created explicitly (not implicitly by first invocation) so the
# retention is pinned to a finite value and the IAM policy can scope to it.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name_prefix}-api"
  retention_in_days = var.lambda_log_retention_days

  tags = local.tags
}

# ---------------------------------------------------------------------------
# The app function.
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "app" {
  function_name = "${local.name_prefix}-api"
  description   = "Spec Server API (Flask via WSGI adapter) backed by DynamoDB, fronted by API Gateway HTTP API + Cognito JWT."

  role          = aws_iam_role.lambda_exec.arn
  architectures = ["arm64"]
  runtime       = "python3.12"
  handler       = "wsgi_lambda.handler"

  filename         = local.lambda_package_path
  source_code_hash = local.lambda_package_hash

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds

  environment {
    variables = {
      # Storage backend selection (STORAGE_ABSTRACTION_DEEPDIVE.md §5).
      STORAGE_BACKEND = "dynamodb"

      # Target table (dynamodb.tf output `dynamodb_table_name`). DDB_TABLE is the
      # name this task specifies; DYNAMODB_TABLE mirrors it for app/config.py
      # compatibility (§5) — both point at the same table.
      DDB_TABLE      = aws_dynamodb_table.app.name
      DYNAMODB_TABLE = aws_dynamodb_table.app.name

      # Cognito JWT validation inputs (AUTH-2 app-level validator). Issuer/JWKS
      # come from cognito.tf outputs; audience is the set of accepted client_ids
      # (the `agents` + UI clients, shared with the API Gateway JWT authorizer —
      # see cognito.tf `local.cognito_agent_audiences` and apigw.tf).
      COGNITO_ISSUER   = "https://${aws_cognito_user_pool.this.endpoint}"
      COGNITO_JWKS_URI = "https://${aws_cognito_user_pool.this.endpoint}/.well-known/jwks.json"
      COGNITO_AUDIENCE = join(",", local.cognito_agent_audiences)

      # HA-5 — the pool the admin user-lifecycle API (GET/POST /api/v1/admin/
      # users...) manages humans/agents in via cognito-idp admin actions (IAM in
      # iam.tf, scoped to this pool ARN). Unset would make those endpoints 501.
      COGNITO_USER_POOL_ID = aws_cognito_user_pool.this.id

      # HA-2 — the invites table backing POST/GET /api/v1/admin/invites (the
      # PreSignUp burn Lambda reads the same table). Unset => the endpoints 501.
      INVITES_TABLE = aws_dynamodb_table.invites.name

      # ONBOARD-1 — the agent-enrollments table backing the single-use enrollment
      # tokens (ONBOARD-2 mints, ONBOARD-3 redeems). IAM (PutItem/GetItem/
      # UpdateItem/DeleteItem scoped to this table ARN) is in iam.tf.
      AGENT_ENROLLMENTS_TABLE = aws_dynamodb_table.agent_enrollments.name

      # ONBOARD-3 — the redeem response hands a freshly-enrolled agent the client
      # id it authenticates with (USER_PASSWORD_AUTH against the `agents` client).
      # Unset => the recipe returns client_id=null and the new agent cannot mint a
      # token. The other ENROLL_* knobs use their correct app/config.py defaults.
      ENROLL_COGNITO_CLIENT_ID = aws_cognito_user_pool_client.agents.id

      # ISO-6 — flip per-project isolation ON. Guarded by the ISO-7 boot check
      # (requires COGNITO_ISSUER, which is set above). Non-members are scoped out
      # (404 reads / 403 writes); the 19 platform agents were backfilled as
      # spec-server members first, so no lockout.
      PROJECT_ISOLATION_ENFORCED = "true"

      # HA-7 — public request->approve signup queue (signups.tf). The public
      # POST /api/v1/signup enqueues to SQS; GET /api/v1/validate + the admin
      # signups bridge read/write the signups table; the per-IP limiter uses the
      # ratelimit counter. TURNSTILE_SECRET/SIGNUP_PEPPER are optional (see
      # signups.tf vars). SES_* let the approve step email the join link (HA-6).
      SIGNUPS_TABLE             = aws_dynamodb_table.signups.name
      SIGNUP_INTAKE_QUEUE_URL   = aws_sqs_queue.signup_intake.url
      SIGNUP_RATELIMIT_TABLE    = aws_dynamodb_table.signup_ratelimit.name
      SIGNUP_RATELIMIT_MAX      = tostring(var.signup_ratelimit_max)
      SIGNUP_RATELIMIT_WINDOW_S = tostring(var.signup_ratelimit_window_seconds)
      TURNSTILE_SECRET          = var.turnstile_secret
      # SEC-PII-1 — SIGNUP_PEPPER must NEVER be empty (unsalted email hashes are
      # offline dictionary-reversible). A provided var wins (Secrets Manager/prod
      # override); otherwise fall back to the strong in-state pepper generated in
      # security_secrets.tf. NOTE: this MUST equal the signup worker's pepper —
      # apply the same fallback in signups.tf if/when the worker is re-keyed.
      SIGNUP_PEPPER            = var.signup_pepper != "" ? var.signup_pepper : random_password.signup_pepper.result
      SIGNUP_VALIDATE_BASE_URL = var.signup_validate_base_url
      SIGNUP_ENFORCE_ORIGIN    = tostring(var.signup_enforce_origin)
      SIGNUP_ALLOWED_ORIGINS   = var.signup_allowed_origins
      SES_FROM_ADDRESS         = var.ses_from_address
      SES_CONFIG_SET           = aws_sesv2_configuration_set.auth.configuration_set_name

      # SEC-DOS-2 — per-authenticated-principal (per `sub`) API throttle. Keyed
      # on the VERIFIED token `sub`, enforced after auth on the /api/v1 data
      # plane so one token/tenant can't starve the stage-wide API Gateway budget.
      # REUSES the signup-ratelimit counter table (distinct `apisub#` key
      # namespace) — its app-Lambda IAM (signups.tf `AppRateLimitCounter`,
      # dynamodb:UpdateItem) already covers it, so no new table/IAM. FAILS OPEN:
      # unset table or any DDB error => allow (never 429 a legit agent).
      API_RATELIMIT_TABLE    = aws_dynamodb_table.signup_ratelimit.name
      API_RATELIMIT_MAX      = tostring(var.api_ratelimit_max)
      API_RATELIMIT_WINDOW_S = tostring(var.api_ratelimit_window_seconds)

      # SEC-EDGE-1 — origin lock. The app compares the ORIGIN_LOCK_HEADER value on
      # incoming requests to ORIGIN_LOCK_SECRET (a Cloudflare Transform Rule
      # injects it) to reject requests that bypass the CDN. ORIGIN_LOCK_MODE stages
      # the rollout (off => no-op; warn => log; enforce => reject). Secret is
      # generated in security_secrets.tf (in-state, never tfvars/printed).
      ORIGIN_LOCK_SECRET = random_password.origin_lock.result
      ORIGIN_LOCK_MODE   = var.origin_lock_mode
      ORIGIN_LOCK_HEADER = "X-Origin-Lock"
    }
  }

  # Ensure the log group (with finite retention) exists before the function so
  # the runtime writes into the retention-bounded group, not an auto-created one.
  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda_permissions,
  ]

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "lambda_function_name" {
  description = "Name of the Spec Server app Lambda function."
  value       = aws_lambda_function.app.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Spec Server app Lambda function."
  value       = aws_lambda_function.app.arn
}
