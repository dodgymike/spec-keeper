# cognito.tf
# =============================================================================
# AUTH-1 / AUTH-9 — Cognito authentication for the Spec Server.
#
# AUTH-9 REWORK (cost): the original design gave every AI agent its own
# machine-to-machine (client_credentials) app client. Cognito bills ~$6/mo PER
# M2M app client, so 5 agents => ~$30/mo, which blew the $20 budget. This file
# now uses ordinary Cognito USER auth instead, which is MAU-priced and FREE
# below 50k monthly active users. The handful of agent "users" cost nothing.
#
#   * AGENTS (AI coding agents): a single PUBLIC app client (`agents`, no secret)
#     using the USER_PASSWORD / SRP auth flows. Each agent is a Cognito USER with
#     a strong generated permanent password (kept in ONE Secrets Manager secret,
#     referenced by ARN — never in git/outputs). Agents call InitiateAuth
#     (USER_PASSWORD_AUTH) to mint a JWT; authorization is by GROUP membership
#     (spec-admins / spec-writers / spec-readers) carried in the `cognito:groups`
#     claim, which the app (AUTH-2) enforces per route/method.
#
#   * HUMAN (React UI): OAuth2 Authorization Code + PKCE via the Cognito Hosted
#     UI. Public SPA client (no secret), openid/email/profile scopes. UNCHANGED.
#
# There is NO OAuth2 resource server / custom scopes and NO client_credentials
# grant anymore: agent authorization is group-based, not scope-based.
#
# This file is deliberately SELF-CONTAINED (its own variables + outputs) so it
# stays merge-conflict-free with the parallel dynamodb.tf work. It only reuses
# `local.tags` and `local.name_prefix` from the INFRA-1 skeleton. Provider
# default_tags already stamps the mandatory tag set on every taggable resource;
# explicit `tags = local.tags` blocks below are redundant-but-clear.
#
# PROD HARDENING (AUTH-8): set `enable_mfa = true` in the prod tfvars to turn on
# OPTIONAL TOTP MFA on the human sign-in path AND advanced_security_mode=ENFORCED.
# Default is false so dev/local stays frictionless and cost-free (advanced
# security bills per monthly active user). See var.enable_mfa.
#
# HA-1 (passkeys + email-OTP first factor): the pool is upgraded to the
# Cognito ESSENTIALS feature plan and gains NATIVE WebAuthn passkeys + a
# choice-based (USER_AUTH) sign-in flow for HUMANS, mirroring the bird auth
# module. The AGENT path (USER_PASSWORD_AUTH on the `agents` client) is
# deliberately left untouched — agents keep minting JWTs exactly as before.
# The five CUSTOM_AUTH / message trigger Lambdas (pre_sign_up, define/create/
# verify_auth_challenge, custom_message) are HA-2/HA-3's work; this task only
# wires their ARNs as OPTIONAL variables (empty by default => no lambda_config
# block, so HA-1 does NOT depend on HA-2/HA-3 and causes no drift when unset).
#
# COST: ESSENTIALS is a paid Cognito feature plan (~$0.015 / monthly active
# user, i.e. free for the first tier of MAUs, then per-MAU). Agent "users" are
# not human-interactive MAUs; the human side has no users yet. This keeps the
# service well inside the $20 budget at this scale, but it is NOT the old
# free LITE tier — revisit if human MAUs grow.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to AUTH-1/AUTH-9; kept in this file on purpose)
# ---------------------------------------------------------------------------

variable "cognito_domain_prefix" {
  description = "Prefix for the Cognito Hosted-UI domain (https://<prefix>-<suffix>.auth.<region>.amazoncognito.com). Must be DNS-safe: lowercase letters, digits, hyphens. A short random suffix is appended for global uniqueness."
  type        = string
  default     = "spec-server-auth"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,40}$", var.cognito_domain_prefix))
    error_message = "cognito_domain_prefix must be lowercase alphanumeric/hyphen, start alphanumeric, <= 41 chars."
  }
}

variable "agent_clients" {
  description = "AI-agent USERS created in the pool. One Cognito user (with a strong generated permanent password) is created per name; all share the single public `agents` app client and are authorized by GROUP membership. `spec-keeper` and `aws-infra` are placed in spec-admins; every other agent in spec-writers."
  type        = list(string)
  default     = ["spec-keeper", "implementer", "reviewer", "security", "aws-infra"]
}

variable "agent_admins" {
  description = "Subset of var.agent_clients placed in the spec-admins group; everyone else goes to spec-writers. Kept as a variable so the admin set is auditable/overridable without editing the role-map expression."
  type        = list(string)
  default     = ["spec-keeper", "aws-infra"]
}

variable "agent_username_domain" {
  description = "DNS-style domain used to synthesize each agent user's Cognito username/email. The pool signs users in by email (username_attributes = [\"email\"]), so agent slugs like `spec-keeper` become `spec-keeper@<domain>`. Not a real mailbox — agent users never receive email (creation is SUPPRESSed). The app authenticates with the full `username` recorded in the agent-credentials secret."
  type        = string
  default     = "agents.spec-server.internal"
}

variable "ui_callback_urls" {
  description = "Allowed OAuth redirect (callback) URLs for the human React SPA Authorization Code + PKCE flow. Localhost for dev + a prod placeholder."
  type        = list(string)
  default = [
    "http://localhost:5173/auth/callback",
    "https://app.spec-server.example.com/auth/callback",
  ]
}

variable "ui_logout_urls" {
  description = "Allowed sign-out redirect URLs for the human React SPA."
  type        = list(string)
  default = [
    "http://localhost:5173/",
    "https://app.spec-server.example.com/",
  ]
}

# AUTH-8 — prod hardening toggle for the HUMAN sign-in path.
#
# When true (prod):
#   * The user pool enables OPTIONAL TOTP (software-token) MFA — see the
#     mfa_configuration + software_token_mfa_configuration below.
#   * Cognito advanced security (compromised-credential / adaptive risk
#     detection) is switched to ENFORCED.
#
# Kept OFF by default so dev/local stays frictionless AND cost-free: advanced
# security ("Plus" feature plan) is billed per monthly-active-user, so it must
# only ever be on for prod. To harden prod, set `enable_mfa = true` in the prod
# tfvars (see the header comment / infra/README.md prod section).
variable "enable_mfa" {
  description = "Prod hardening for the human sign-in path: when true, enable OPTIONAL TOTP MFA on the user pool and set advanced_security_mode = ENFORCED. Default false keeps dev/local MFA-off and cost-free (advanced security is billed per MAU). NOTE (HA-1): advanced_security_mode=ENFORCED requires the Cognito PLUS feature plan; with user_pool_tier=ESSENTIALS this must stay OFF. Turning enable_mfa on in prod now also requires bumping user_pool_tier to PLUS."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# HA-1 — native WebAuthn passkeys.
# ---------------------------------------------------------------------------

# WebAuthn Relying-Party ID. This is DOMAIN-PINNED: a passkey enrolled against
# one RP-ID cannot be used from another origin, and CHANGING this value RESETS
# (invalidates) every enrolled passkey. It currently points at the deployed
# CloudFront UI host. HA-8 moves the UI to spec.elasticninja.com; flipping this
# to that domain at cutover will require all humans to re-enroll their passkeys
# (there are no human users yet, so today the reset is a no-op).
variable "webauthn_rp_id" {
  description = "WebAuthn Relying-Party ID (the registrable domain the passkey is bound to). Must match the origin the human UI is served from. Domain-pinned: changing it invalidates all enrolled passkeys. Defaults to the current CloudFront UI host; HA-8 will move it to spec.elasticninja.com."
  type        = string
  default     = "do153mulmuok3.cloudfront.net"
}

# ---------------------------------------------------------------------------
# HA-1 — CUSTOM_AUTH / message trigger Lambda ARNs (OPTIONAL seams for HA-2/HA-3).
#
# Each defaults to "" so that, until HA-2/HA-3 create the trigger Lambdas, NO
# lambda_config block is emitted on the pool (see local.any_lambda_trigger and
# the dynamic "lambda_config" block below). That keeps THIS task free of any
# dependency on those Lambdas and prevents Terraform drift when they are unset.
# HA-2/HA-3 wire their functions by setting these in tfvars — no edit to this
# file required.
# ---------------------------------------------------------------------------
variable "presignup_lambda_arn" {
  description = "ARN of the pre_sign_up trigger Lambda (HA-2/HA-3). Empty => not wired."
  type        = string
  default     = ""
}

variable "define_auth_lambda_arn" {
  description = "ARN of the define_auth_challenge trigger Lambda (HA-2/HA-3), used for the email-OTP CUSTOM_AUTH flow. Empty => not wired."
  type        = string
  default     = ""
}

variable "create_auth_lambda_arn" {
  description = "ARN of the create_auth_challenge trigger Lambda (HA-2/HA-3). Empty => not wired."
  type        = string
  default     = ""
}

variable "verify_auth_lambda_arn" {
  description = "ARN of the verify_auth_challenge_response trigger Lambda (HA-2/HA-3). Empty => not wired."
  type        = string
  default     = ""
}

variable "custom_message_lambda_arn" {
  description = "ARN of the custom_message trigger Lambda (HA-2/HA-3), used to render the email-OTP message. Empty => not wired."
  type        = string
  default     = ""
}

# HA-1 — Managed Login (v2) branding. The AWS provider (~> 5.0, pinned 5.100)
# does NOT yet expose `aws_cognito_managed_login_branding`, so the default
# Cognito-provided branding is created via the CLI in a terraform_data local-
# exec (idempotent). Toggle off where the apply environment has no cognito-idp
# CLI access; the pool + WebAuthn config still stand up without it.
variable "enable_managed_login_branding" {
  description = "Create the default Managed Login (v2) branding for the human `ui` client via the AWS CLI (provider lacks the resource in 5.x). Requires cognito-idp:CreateManagedLoginBranding + a recent AWS CLI. Default false: the SPA uses NATIVE WebAuthn via the cognito-idp API, not the hosted Managed Login UI, so branding is unused (and older AWS CLIs reject the command)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  # Per-agent group assignment: admins for the configured admin set, writers
  # for everyone else. spec-readers exists for humans/read-only actors but no
  # agent is placed in it by default.
  agent_group_map = {
    for name in var.agent_clients :
    name => contains(var.agent_admins, name) ? "spec-admins" : "spec-writers"
  }

  # Full synthetic username (email alias) each agent signs in with.
  agent_usernames = {
    for name in var.agent_clients :
    name => "${name}@${var.agent_username_domain}"
  }

  # HA-1 — CUSTOM_AUTH / message trigger ARNs, keyed by their lambda_config
  # attribute name. Empty strings mean "not wired yet" (HA-2/HA-3 fill them).
  lambda_triggers = {
    pre_sign_up                    = var.presignup_lambda_arn
    define_auth_challenge          = var.define_auth_lambda_arn
    create_auth_challenge          = var.create_auth_lambda_arn
    verify_auth_challenge_response = var.verify_auth_lambda_arn
    custom_message                 = var.custom_message_lambda_arn
  }

  # Only emit a lambda_config block once at least one trigger ARN is set, so the
  # default (all empty) pool carries NO lambda_config and stays independent of
  # HA-2/HA-3.
  any_lambda_trigger = length([for arn in values(local.lambda_triggers) : arn if arn != ""]) > 0

  # The JWT audiences accepted by BOTH the API GW authorizer (apigw.tf) and the
  # app-level validator (lambda.tf reads this for COGNITO_AUDIENCE): the public
  # `agents` client id + the human UI client id.
  cognito_agent_audiences = [
    aws_cognito_user_pool_client.agents.id,
    aws_cognito_user_pool_client.ui.id,
  ]
}

# ---------------------------------------------------------------------------
# Global-uniqueness suffix for the Hosted-UI domain (Cognito domain prefixes
# share one global namespace, like S3 bucket names).
# ---------------------------------------------------------------------------
resource "random_string" "cognito_domain_suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

# ---------------------------------------------------------------------------
# User pool
# ---------------------------------------------------------------------------
resource "aws_cognito_user_pool" "this" {
  name = "${local.name_prefix}-pool"

  # HA-1 — ESSENTIALS feature plan. REQUIRED for native WebAuthn passkeys and
  # email-OTP (choice-based USER_AUTH) sign-in. This is a per-MAU-priced paid
  # plan (see the header COST note); it is an in-place UpdateUserPool, not a
  # replacement, so no users/groups are lost when moving off the free plan.
  user_pool_tier = "ESSENTIALS"

  # Humans sign in with their email address; agent users use a synthetic email.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Self-signup is ENABLED but gated by the PreSignUp trigger (HA-2), which burns
  # a single-use invite code — so it is invite-only in practice, not open signup.
  # allow_admin_create_user_only=true would BLOCK the SignUp API entirely and
  # break the invite/passkey join flow ("SignUp is not permitted for this user
  # pool"). Agent users are still created by Terraform via admin_create_user.
  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  # HA-1 — native WebAuthn passkeys. relying_party_id is domain-pinned to the
  # UI host (var.webauthn_rp_id); user_verification=required forces a local
  # gesture (biometric/PIN) so the passkey is a genuine second-factor-grade
  # authenticator, matching the bird module.
  web_authn_configuration {
    relying_party_id  = var.webauthn_rp_id
    user_verification = "required"
  }

  # HA-1 — choice-based first-factor policy. PASSWORD keeps the agents'
  # USER_PASSWORD_AUTH path alive; WEB_AUTHN enables passkey sign-in for humans.
  # EMAIL_OTP is deliberately NOT a first-auth factor here: per the bird QAFIX-1
  # hardening, email-OTP is reachable ONLY via CUSTOM_AUTH (the define/create/
  # verify_auth_challenge Lambdas), never as a standalone first factor.
  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD", "WEB_AUTHN"]
  }

  # HA-1 — recovery is admin-only: self-service ForgotPassword is disabled so a
  # stolen mailbox cannot reset an account. Humans recover via an admin reset;
  # agent users never recover (their passwords live in Secrets Manager).
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  # HA-1 — CUSTOM_AUTH / message trigger Lambdas (email-OTP flow). Emitted ONLY
  # when at least one ARN variable is set, so the default pool has no
  # lambda_config and this task does not depend on HA-2/HA-3. Each attribute is
  # null (omitted) unless its ARN is provided.
  dynamic "lambda_config" {
    for_each = local.any_lambda_trigger ? [1] : []
    content {
      pre_sign_up                    = var.presignup_lambda_arn != "" ? var.presignup_lambda_arn : null
      define_auth_challenge          = var.define_auth_lambda_arn != "" ? var.define_auth_lambda_arn : null
      create_auth_challenge          = var.create_auth_lambda_arn != "" ? var.create_auth_lambda_arn : null
      verify_auth_challenge_response = var.verify_auth_lambda_arn != "" ? var.verify_auth_lambda_arn : null
      custom_message                 = var.custom_message_lambda_arn != "" ? var.custom_message_lambda_arn : null
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # AUTH-8 — MFA for the HUMAN (PKCE) sign-in path, gated by var.enable_mfa.
  #
  # Mode choice: OPTIONAL (not ON) when enabled. OPTIONAL lets existing users
  # keep signing in while they enroll a TOTP authenticator on their own cadence
  # — flipping straight to "ON" would immediately lock out every user who has
  # not yet registered a factor. Dev/local stays "OFF" (default). Only TOTP
  # (software_token) is enabled — no SMS, so no per-message SNS cost.
  mfa_configuration = var.enable_mfa ? "OPTIONAL" : "OFF"

  # Cognito rejects a software_token_mfa_configuration block when MFA is OFF
  # ("can't turn off MFA and configure an MFA together"), so only emit it when
  # MFA is enabled.
  dynamic "software_token_mfa_configuration" {
    for_each = var.enable_mfa ? [1] : []
    content {
      enabled = true
    }
  }

  # AUTH-8 — Cognito advanced security (adaptive/compromised-credential risk
  # detection). ENFORCED only in prod because it is billed per monthly active
  # user under the Cognito "Plus" feature plan; OFF by default keeps dev free.
  user_pool_add_ons {
    advanced_security_mode = var.enable_mfa ? "ENFORCED" : "OFF"
  }

  schema {
    name                     = "email"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  tags = local.tags
}

# Hosted-UI domain (backs the human Authorization-Code flow). Agents do NOT use
# the hosted UI or the /oauth2/token endpoint — they call InitiateAuth directly.
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.cognito_domain_prefix}-${random_string.cognito_domain_suffix.result}"
  user_pool_id = aws_cognito_user_pool.this.id

  # HA-1 — serve the Managed Login (v2) experience, which renders the native
  # passkey / choice-based sign-in pages. Setting the version is an in-place
  # UpdateUserPoolDomain (the `domain` string is unchanged, so the domain is not
  # recreated); confirm this in the plan before applying to the live pool.
  managed_login_version = 2
}

# ---------------------------------------------------------------------------
# HA-1 — Managed Login (v2) branding for the human `ui` client.
#
# The AWS provider (5.100) has no `aws_cognito_managed_login_branding`
# resource, so the DEFAULT Cognito-provided branding is created out-of-band via
# the CLI. This is the documented fallback pattern. The call is idempotent: it
# only creates branding when none exists for the client, so re-applies are
# no-ops. terraform_data (a built-in) is used instead of null_resource to avoid
# pulling in the null provider (versions.tf is owned elsewhere).
# ---------------------------------------------------------------------------
resource "terraform_data" "ui_managed_login_branding" {
  count = var.enable_managed_login_branding ? 1 : 0

  # Re-run if the pool or client identity changes.
  triggers_replace = [
    aws_cognito_user_pool.this.id,
    aws_cognito_user_pool_client.ui.id,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      POOL_ID   = aws_cognito_user_pool.this.id
      CLIENT_ID = aws_cognito_user_pool_client.ui.id
      REGION    = var.aws_region
    }
    command = <<-EOT
      set -euo pipefail
      existing="$(aws cognito-idp describe-managed-login-branding-by-client \
        --region "$REGION" --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
        --query 'ManagedLoginBranding.ManagedLoginBrandingId' --output text 2>/dev/null || true)"
      if [ -z "$existing" ] || [ "$existing" = "None" ]; then
        aws cognito-idp create-managed-login-branding \
          --region "$REGION" --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
          --use-cognito-provided-values >/dev/null
        echo "created default Managed Login branding for client $CLIENT_ID"
      else
        echo "Managed Login branding already present ($existing); leaving as-is"
      fi
    EOT
  }
}

# ---------------------------------------------------------------------------
# Groups — the authorization vocabulary carried in the `cognito:groups` JWT
# claim. Lower precedence = higher priority, so spec-admins wins when a user is
# in multiple groups.
# ---------------------------------------------------------------------------
resource "aws_cognito_user_group" "this" {
  for_each = {
    spec-admins  = { precedence = 0, description = "Full admin: projects/epics/reservations + all task writes/reads." }
    spec-writers = { precedence = 10, description = "Create/claim/complete/update tasks (task write + read)." }
    spec-readers = { precedence = 20, description = "Read-only access to tasks/specs." }
  }

  name         = each.key
  user_pool_id = aws_cognito_user_pool.this.id
  precedence   = each.value.precedence
  description  = each.value.description
}

# ---------------------------------------------------------------------------
# AGENT app client — single PUBLIC client (no secret) shared by all agent users.
# USER_PASSWORD / SRP / REFRESH auth only. NO OAuth/hosted-UI flows, NO
# client_credentials (that grant is what billed per client). MAU-priced -> free
# at this scale.
# ---------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "agents" {
  name         = "${local.name_prefix}-agents"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = false

  # Direct-auth flows for machine agents. No hosted UI, no OAuth code/token
  # endpoints, no client_credentials.
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]
  allowed_oauth_flows_user_pool_client = false
  supported_identity_providers         = ["COGNITO"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}

# ---------------------------------------------------------------------------
# Human SPA client — reworked by HA-1 for the NATIVE WebAuthn / choice-based
# sign-in path. Still a PUBLIC client (no secret). The OAuth code flow is kept
# so the Managed Login (v2) hosted pages can drive passkey / email-OTP sign-in;
# HA-4 reworks the UI to match. There are no human users yet, so changing the
# human flow from hosted-UI PKCE/SRP to native WebAuthn breaks nobody.
# ---------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "ui" {
  name         = "${local.name_prefix}-ui"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = var.ui_callback_urls
  logout_urls   = var.ui_logout_urls

  # HA-1 — native auth flows for humans:
  #   ALLOW_USER_AUTH          -> choice-based sign-in (WEB_AUTHN passkeys, and
  #                               EMAIL_OTP once the CUSTOM_AUTH Lambdas land).
  #   ALLOW_CUSTOM_AUTH        -> the email-OTP CUSTOM_AUTH challenge flow.
  #   ALLOW_REFRESH_TOKEN_AUTH -> keep the session alive.
  # USER_PASSWORD / SRP are intentionally dropped from the HUMAN client (the
  # AGENT client keeps USER_PASSWORD_AUTH — see aws_cognito_user_pool_client.agents).
  explicit_auth_flows = ["ALLOW_USER_AUTH", "ALLOW_CUSTOM_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}

# ---------------------------------------------------------------------------
# Agent USERS — one per var.agent_clients, each with a strong generated
# permanent password and placed in the appropriate group.
# ---------------------------------------------------------------------------

# Strong password per agent. Satisfies the pool password policy (>=12, upper +
# lower + number + symbol). Never emitted as a plain output — only stored in the
# single agent-credentials secret below.
resource "random_password" "agent" {
  for_each = toset(var.agent_clients)

  length           = 24
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "!@#%^*()-_=+" # policy-safe symbols (avoid quotes/backslash)
}

# Setting `password` (not `temporary_password`) makes the provider call
# AdminSetUserPassword with Permanent=true, so no first-login reset is needed.
# message_action = SUPPRESS => no invite email is sent (these are machine users).
resource "aws_cognito_user" "agent" {
  for_each = toset(var.agent_clients)

  user_pool_id   = aws_cognito_user_pool.this.id
  username       = local.agent_usernames[each.key]
  password       = random_password.agent[each.key].result
  message_action = "SUPPRESS"

  attributes = {
    email          = local.agent_usernames[each.key]
    email_verified = "true"
  }
}

resource "aws_cognito_user_in_group" "agent" {
  for_each = toset(var.agent_clients)

  user_pool_id = aws_cognito_user_pool.this.id
  group_name   = aws_cognito_user_group.this[local.agent_group_map[each.key]].name
  username     = aws_cognito_user.agent[each.key].username
}

# ---------------------------------------------------------------------------
# Secrets Manager: ONE secret holding all agent credentials (cost — a single
# secret, not one per agent). JSON shape consumed by scripts/agent_token.py:
#   { "pool_id", "client_id", "region",
#     "users": { "<slug>": { "username", "password", "groups": [...] } } }
# Only the secret ARN is exported — values never leave AWS / never hit git.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "agent_credentials" {
  name        = "${local.name_prefix}/agent-credentials"
  description = "All Spec Server AI-agent Cognito user credentials (username/password + groups) + pool/client/region. Consumed by scripts/agent_token.py to mint JWTs via USER_PASSWORD_AUTH."

  # Fast recovery in dev; values are re-derivable by re-applying (new passwords).
  recovery_window_in_days = 7

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "agent_credentials" {
  secret_id = aws_secretsmanager_secret.agent_credentials.id

  secret_string = jsonencode({
    pool_id   = aws_cognito_user_pool.this.id
    client_id = aws_cognito_user_pool_client.agents.id
    region    = var.aws_region
    users = {
      for name in var.agent_clients : name => {
        username = aws_cognito_user.agent[name].username
        password = random_password.agent[name].result
        groups   = [local.agent_group_map[name]]
      }
    }
  })

  # The full agent roster is provisioned by scripts/enrol_agents.py (the
  # aws_cognito_user provider resource cannot create permanent-password users on
  # this pool — see the script header). That script owns the live secret's
  # `users` map (all enrolled agents, not just var.agent_clients), so Terraform
  # must NOT revert it on the next apply. Terraform still creates the secret and
  # seeds the initial bootstrap agents; the script merges the rest in place.
  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ---------------------------------------------------------------------------
# Outputs — consumed by AUTH-2 (app JWT validator), AUTH-3 (API GW JWT
# authorizer), and scripts/agent_token.py. NO secret values here, only ARNs.
# ---------------------------------------------------------------------------

output "cognito_user_pool_id" {
  description = "Cognito user pool ID."
  value       = aws_cognito_user_pool.this.id
}

output "cognito_issuer_url" {
  description = "OIDC issuer URL. AUTH-3 sets this as the JWT authorizer `issuer`; AUTH-2 uses it to build the discovery/JWKS URL and to validate the token `iss` claim."
  value       = "https://${aws_cognito_user_pool.this.endpoint}"
}

output "cognito_jwks_uri" {
  description = "JWKS URI. AUTH-2's app-level validator fetches signing keys here to verify RS256 JWT signatures."
  value       = "https://${aws_cognito_user_pool.this.endpoint}/.well-known/jwks.json"
}

output "cognito_agents_client_id" {
  description = "Public `agents` app client ID used by all AI-agent users (USER_PASSWORD_AUTH)."
  value       = aws_cognito_user_pool_client.agents.id
}

output "cognito_ui_client_id" {
  description = "Public SPA (human) app client ID for the Authorization Code + PKCE flow."
  value       = aws_cognito_user_pool_client.ui.id
}

output "cognito_agent_audiences" {
  description = "JWT audiences accepted by the API GW authorizer + app validator: [agents client id, UI client id]. apigw.tf and lambda.tf read this."
  value       = local.cognito_agent_audiences
}

output "cognito_agent_credentials_secret_arn" {
  description = "ARN of the single Secrets Manager secret holding ALL agent credentials (username/password/groups + pool/client/region). Grant agent runners secretsmanager:GetSecretValue on THIS ARN only. Values are never output."
  value       = aws_secretsmanager_secret.agent_credentials.arn
}

output "cognito_hosted_ui_domain" {
  description = "Hosted-UI base URL for the human SPA OAuth authorize/logout endpoints. (Agents do not use the hosted UI.)"
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
}
