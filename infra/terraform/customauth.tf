# customauth.tf
# =============================================================================
# HA-3 — Email-OTP CUSTOM_AUTH Lambda chain (the FIRST authentication factor for
# onboarding + passkey recovery), mirroring the bird project's custom-auth
# triggers, trimmed to a SINGLE email-OTP round.
#
# Three Cognito triggers, one Lambda each (python3.12, arm64, scales to zero):
#   * DefineAuthChallenge          -> define_auth_lambda/  (state machine)
#   * CreateAuthChallenge          -> create_auth_lambda/  (mint + email OTP)
#   * VerifyAuthChallengeResponse  -> verify_auth_lambda/  (constant-time verify)
#
# Only CreateAuthChallenge sends email, so ONLY its role gets the least-privilege
# SES send policy (ses.tf `ses_send_policy_arn`). Define/Verify get nothing but
# their own log group.
#
# SCOPE: email-OTP ONLY. The bird SECOND factor (TOTP) is DEFERRED — there is no
# DynamoDB TOTP-secret table, no second CUSTOM round, and none of these roles get
# DynamoDB access. A later task adds TOTP if/when required.
#
# DECOUPLED FROM cognito.tf ON PURPOSE. This file does NOT create the user pool
# or set the pool's Lambda-trigger config — that is owned by cognito.tf / the
# passkey cutover. The three function ARNs are exposed as outputs so the
# orchestrator can wire `lambda_config { define_auth_challenge = ... }` at the
# cutover. The invoke permission's source_arn is taken as `var.cognito_user_pool_arn`
# (default "") and the permission is COUNT-GATED so `terraform validate` passes
# with it unset; pin it at apply time without editing cognito.tf.
#
# SELF-CONTAINED: its own variables + outputs live here. It reads only
# `local.name_prefix` / `local.tags` (variables.tf), and from ses.tf:
# `var.ses_from_address`, `local.ses_from_identity_arn`,
# `aws_sesv2_configuration_set.auth`, and `aws_iam_policy.ses_send`.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (own — kept in this file to stay merge-conflict-free).
#
# NOTE: `var.cognito_user_pool_arn` (default "") is the SHARED pool-ARN seam the
# orchestrator pins at the passkey cutover. It is declared once for the whole
# module in HA-2's `invites.tf` (the PreSignUp Lambda needs the same value), so
# we deliberately do NOT redeclare it here — Terraform variables are module-wide
# and a second declaration is a duplicate-variable error. This file just
# *references* `var.cognito_user_pool_arn`. If invites.tf is not present in a
# given checkout, add the declaration here (type=string, default="").
# ---------------------------------------------------------------------------
variable "customauth_otp_ttl_seconds" {
  description = "Email-OTP lifetime in seconds (5 min). Stamped into the challenge session by CreateAuthChallenge and re-checked by VerifyAuthChallengeResponse."
  type        = number
  default     = 300

  validation {
    condition     = var.customauth_otp_ttl_seconds >= 60 && var.customauth_otp_ttl_seconds <= 900
    error_message = "customauth_otp_ttl_seconds must be 60..900 (1–15 min)."
  }
}

variable "customauth_otp_max_attempts" {
  description = "Number of wrong-code attempts before DefineAuthChallenge fails the auth (fail closed)."
  type        = number
  default     = 3

  validation {
    condition     = var.customauth_otp_max_attempts >= 1 && var.customauth_otp_max_attempts <= 10
    error_message = "customauth_otp_max_attempts must be 1..10."
  }
}

variable "customauth_log_retention_days" {
  description = "CloudWatch retention (days) for the three custom-auth Lambda log groups. Explicit + finite so logs never accrue cost forever."
  type        = number
  default     = 30
}

# --- SEC-AUTH-2: cross-session per-email OTP caps (brute-force + email-bomb) ---
# The three triggers share the EXISTING per-IP signup rate-limit table
# (signups.tf `aws_dynamodb_table.signup_ratelimit`, key = `pk` + `ttl`) under a
# distinct key namespace (`otp-send:` / `otp-fail:`), so no new table/infra: just
# a counter env var + Get/UpdateItem IAM on that table (added below). All three
# caps + the window are env-var knobs so they are tunable without a code change.
variable "customauth_otp_ratelimit_window_seconds" {
  description = "Fixed window (seconds) for the cross-session per-email OTP caps. Default 1h."
  type        = number
  default     = 3600

  validation {
    condition     = var.customauth_otp_ratelimit_window_seconds >= 60
    error_message = "customauth_otp_ratelimit_window_seconds must be >= 60."
  }
}

variable "customauth_otp_send_cap" {
  description = "Max OTP emails per email address per window before create_auth stops emailing (email-bomb guard). Fails OPEN on any counter error."
  type        = number
  default     = 5

  validation {
    condition     = var.customauth_otp_send_cap >= 1
    error_message = "customauth_otp_send_cap must be >= 1."
  }
}

variable "customauth_otp_fail_cap" {
  description = "Max wrong-code attempts per email address per window before define_auth stops issuing challenges (brute-force guard). Fails OPEN on any counter error."
  type        = number
  default     = 10

  validation {
    condition     = var.customauth_otp_fail_cap >= 1
    error_message = "customauth_otp_fail_cap must be >= 1."
  }
}

# ---------------------------------------------------------------------------
# Locals.
# ---------------------------------------------------------------------------
locals {
  customauth_cognito_enabled = var.cognito_user_pool_arn != ""

  # The three triggers, keyed by a short slug. `source_dir` is the vendored
  # Lambda package (handler.py + a copy of otp.py); `ses` marks the one function
  # (CreateAuthChallenge) that gets the SES send policy attached.
  customauth_functions = {
    define_auth = {
      name       = "${local.name_prefix}-define-auth"
      source_dir = "${path.module}/define_auth_lambda"
      desc       = "Cognito DefineAuthChallenge: single-round email-OTP state machine (HA-3, first factor)."
      ses        = false
    }
    create_auth = {
      name       = "${local.name_prefix}-create-auth"
      source_dir = "${path.module}/create_auth_lambda"
      desc       = "Cognito CreateAuthChallenge: mint a 6-digit OTP and email it via SES (HA-3, first factor)."
      ses        = true
    }
    verify_auth = {
      name       = "${local.name_prefix}-verify-auth"
      source_dir = "${path.module}/verify_auth_lambda"
      desc       = "Cognito VerifyAuthChallengeResponse: constant-time compare + 5-min expiry check (HA-3, first factor)."
      ses        = false
    }
  }

  # SEC-AUTH-2: least-privilege counter action per trigger on the SHARED signup
  # rate-limit table. create/verify INCREMENT (UpdateItem); define only READS the
  # brute-force counter (GetItem). No table-admin, no Scan, no wildcard.
  customauth_counter_action = {
    define_auth = "dynamodb:GetItem"
    create_auth = "dynamodb:UpdateItem"
    verify_auth = "dynamodb:UpdateItem"
  }
}

# ---------------------------------------------------------------------------
# Deployment artifacts — each source dir zipped locally by the archive provider
# so offline `terraform validate`/`plan` work with no build tooling or AWS.
# Output paths live under the gitignored .terraform/ dir.
# ---------------------------------------------------------------------------
data "archive_file" "customauth" {
  for_each = local.customauth_functions

  type        = "zip"
  source_dir  = each.value.source_dir
  output_path = "${path.module}/.terraform/spec-server-${each.key}.zip"
}

# ---------------------------------------------------------------------------
# Log groups — explicit + finite retention, scoped in each function's IAM policy.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "customauth" {
  for_each = local.customauth_functions

  name              = "/aws/lambda/${each.value.name}"
  retention_in_days = var.customauth_log_retention_days

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Execution roles — one per function. Trust policy allows only the Lambda
# service to assume them.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "customauth_assume" {
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

resource "aws_iam_role" "customauth" {
  for_each = local.customauth_functions

  name               = "${each.value.name}-exec"
  description        = "Execution role for the ${each.key} custom-auth trigger. Least-privilege: only its own CloudWatch log group (+ SES send on create_auth)."
  assume_role_policy = data.aws_iam_policy_document.customauth_assume.json

  tags = local.tags
}

# Per-function log-write policy: open streams + put events in ONLY this
# function's own log group. CreateLogGroup is omitted — the group is precreated
# above with a finite retention.
data "aws_iam_policy_document" "customauth_logs" {
  for_each = local.customauth_functions

  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.customauth[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "customauth_logs" {
  for_each = local.customauth_functions

  name   = "${each.value.name}-logs"
  role   = aws_iam_role.customauth[each.key].id
  policy = data.aws_iam_policy_document.customauth_logs[each.key].json
}

# SEC-AUTH-2 — per-function least-privilege access to the SHARED signup
# rate-limit counter table (signups.tf). create/verify get UpdateItem (atomic
# ADD increment); define gets GetItem (read-only brute-force gate). Scoped to the
# single table ARN — no index, no Scan, no wildcard.
data "aws_iam_policy_document" "customauth_counter" {
  for_each = local.customauth_functions

  statement {
    sid       = "OtpRateLimitCounter"
    effect    = "Allow"
    actions   = [local.customauth_counter_action[each.key]]
    resources = [aws_dynamodb_table.signup_ratelimit.arn]
  }
}

resource "aws_iam_role_policy" "customauth_counter" {
  for_each = local.customauth_functions

  name   = "${each.value.name}-otp-ratelimit"
  role   = aws_iam_role.customauth[each.key].id
  policy = data.aws_iam_policy_document.customauth_counter[each.key].json
}

# SES send — attached to ONLY the CreateAuthChallenge role (the only function
# that emails). Reuses the least-privilege managed policy from ses.tf; Define /
# Verify never get SES.
resource "aws_iam_role_policy_attachment" "customauth_ses" {
  for_each = { for k, v in local.customauth_functions : k => v if v.ses }

  role       = aws_iam_role.customauth[each.key].name
  policy_arn = aws_iam_policy.ses_send.arn
}

# ---------------------------------------------------------------------------
# The three functions (python3.12, arm64, scales to zero).
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "customauth" {
  for_each = local.customauth_functions

  function_name = each.value.name
  description   = each.value.desc

  role          = aws_iam_role.customauth[each.key].arn
  architectures = ["arm64"]
  runtime       = "python3.12"
  handler       = "handler.handler"

  filename         = data.archive_file.customauth[each.key].output_path
  source_code_hash = data.archive_file.customauth[each.key].output_base64sha256

  # Tiny, dependency-light triggers: small memory, short timeout (well under the
  # Cognito 5s trigger budget — SES SendEmail is the only network call).
  memory_size = 128
  timeout     = 5

  environment {
    variables = merge(
      {
        OTP_TTL_SECONDS = tostring(var.customauth_otp_ttl_seconds)
        # SEC-AUTH-2: all three triggers use the shared counter table + window.
        OTP_RATELIMIT_TABLE          = aws_dynamodb_table.signup_ratelimit.name
        OTP_RATELIMIT_WINDOW_SECONDS = tostring(var.customauth_otp_ratelimit_window_seconds)
      },
      # DefineAuthChallenge needs the attempt budget + the brute-force fail cap.
      each.key == "define_auth" ? {
        OTP_MAX_ATTEMPTS = tostring(var.customauth_otp_max_attempts)
        OTP_FAIL_CAP     = tostring(var.customauth_otp_fail_cap)
      } : {},
      # CreateAuthChallenge needs the email-bomb issuance cap.
      each.key == "create_auth" ? {
        OTP_SEND_CAP = tostring(var.customauth_otp_send_cap)
      } : {},
      # CreateAuthChallenge needs the SES send inputs (from ses.tf).
      each.key == "create_auth" ? {
        OTP_FROM_ADDRESS      = var.ses_from_address
        OTP_FROM_IDENTITY_ARN = local.ses_from_identity_arn
        SES_CONFIG_SET        = aws_sesv2_configuration_set.auth.configuration_set_name
      } : {},
    )
  }

  depends_on = [
    aws_cloudwatch_log_group.customauth,
    aws_iam_role_policy.customauth_logs,
  ]

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Invoke permissions — let Cognito call each trigger. COUNT-GATED on
# var.cognito_user_pool_arn so validate passes when the pool ARN is unset; the
# orchestrator pins it at the passkey cutover. source_arn scopes the grant to
# exactly this pool (no wildcard principal-with-open-source).
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "customauth_cognito" {
  for_each = local.customauth_cognito_enabled ? local.customauth_functions : {}

  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.customauth[each.key].function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = var.cognito_user_pool_arn
}

# ---------------------------------------------------------------------------
# Outputs — the three trigger ARNs the orchestrator wires into the pool's
# lambda_config at the passkey cutover.
# ---------------------------------------------------------------------------
output "define_auth_lambda_arn" {
  description = "ARN of the DefineAuthChallenge trigger. Wire into the Cognito pool's lambda_config.define_auth_challenge at the passkey cutover."
  value       = aws_lambda_function.customauth["define_auth"].arn
}

output "create_auth_lambda_arn" {
  description = "ARN of the CreateAuthChallenge trigger. Wire into lambda_config.create_auth_challenge."
  value       = aws_lambda_function.customauth["create_auth"].arn
}

output "verify_auth_lambda_arn" {
  description = "ARN of the VerifyAuthChallengeResponse trigger. Wire into lambda_config.verify_auth_challenge_response."
  value       = aws_lambda_function.customauth["verify_auth"].arn
}
