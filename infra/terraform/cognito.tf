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
  description = "Prod hardening for the human sign-in path: when true, enable OPTIONAL TOTP MFA on the user pool and set advanced_security_mode = ENFORCED. Default false keeps dev/local MFA-off and cost-free (advanced security is billed per MAU)."
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

  # Humans sign in with their email address; agent users use a synthetic email.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # No self-service open signups — humans are invited by an admin; agent users
  # are created by this Terraform (admin create).
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
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

  software_token_mfa_configuration {
    enabled = var.enable_mfa
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
# Human SPA client — Authorization Code + PKCE. Public client (no secret).
# UNCHANGED by AUTH-9.
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

  # Minimal explicit flows: SRP for hosted-UI sign-in + refresh to keep the
  # session. PKCE is applied automatically for public clients on the code flow.
  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

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
