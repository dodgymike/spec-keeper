# cognito.tf
# =============================================================================
# AUTH-1 — Cognito authentication for the Spec Server.
#
# Replaces the old static API_KEYS bearer auth with real OAuth2/JWT:
#
#   * MACHINE-TO-MACHINE (AI agents): OAuth2 `client_credentials` grant. Each
#     agent is a confidential app client with a generated secret (kept in
#     Secrets Manager, referenced by ARN — never in git/outputs). Agents POST to
#     the token endpoint with client_id/client_secret + the API scopes and get a
#     short-lived JWT access token whose `scope` claim the API Gateway authorizer
#     (AUTH-3) and the app-level validator (AUTH-2) enforce.
#
#   * HUMAN (React UI): OAuth2 Authorization Code + PKCE via the Cognito Hosted
#     UI. Public SPA client (no secret), openid/email/profile scopes.
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
# security bills per monthly active user). The M2M client_credentials agents use
# the machine flow and are UNAFFECTED by MFA either way. See var.enable_mfa.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to AUTH-1; kept in this file on purpose)
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

variable "resource_server_identifier" {
  description = "OAuth2 resource-server identifier (the API's audience/namespace for custom scopes). Becomes the prefix of every custom scope string, e.g. https://api.spec-server/tasks.read."
  type        = string
  default     = "https://api.spec-server"
}

variable "api_scopes" {
  description = "Custom API scopes exposed by the resource server. Each becomes '<resource_server_identifier>/<scope_name>' in the JWT scope claim, enforced by the API Gateway authorizer + app validator."
  type = list(object({
    scope_name        = string
    scope_description = string
  }))
  default = [
    { scope_name = "tasks.read", scope_description = "Read tasks/specs" },
    { scope_name = "tasks.write", scope_description = "Create/update/claim/complete tasks" },
    { scope_name = "projects.admin", scope_description = "Administer projects, epics, reservations" },
  ]
}

variable "agent_clients" {
  description = "AI-agent machine-to-machine clients. One confidential client_credentials app client + one Secrets Manager secret is created per name. These are the agents that call the Spec Server API with a JWT instead of a static key."
  type        = list(string)
  default     = ["spec-keeper", "implementer", "reviewer", "security", "aws-infra"]
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
#
# This gates the HUMAN (PKCE) path only. The M2M client_credentials agents use
# the machine flow and are UNAFFECTED by MFA regardless of this value.
variable "enable_mfa" {
  description = "Prod hardening for the human sign-in path: when true, enable OPTIONAL TOTP MFA on the user pool and set advanced_security_mode = ENFORCED. Default false keeps dev/local MFA-off and cost-free (advanced security is billed per MAU). M2M agents are unaffected either way."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  # Full scope strings ("<identifier>/<scope_name>") granted to the M2M agents.
  # aws_cognito_resource_server exposes exactly this as `scope_identifiers`.
  m2m_scope_identifiers = aws_cognito_resource_server.api.scope_identifiers
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

  # Humans sign in with their email address.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # No self-service open signups by default — humans are invited by an admin.
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
  # Mode choice: OPTIONAL (not ON) when enabled. OPTIONAL lets already-existing
  # users keep signing in while they enroll a TOTP authenticator on their own
  # cadence — flipping straight to "ON" would immediately lock out every user
  # who has not yet registered a factor. Dev/local stays "OFF" (default).
  #
  # Only TOTP (software_token) is enabled — no SMS, so there is no per-message
  # SNS cost and no phone-number attribute requirement. The M2M
  # client_credentials agents use the machine flow and never see an MFA
  # challenge, so they are unaffected by this setting.
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

# Hosted-UI domain (backs both the human Authorization-Code flow and the
# machine token endpoint https://<domain>/oauth2/token).
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.cognito_domain_prefix}-${random_string.cognito_domain_suffix.result}"
  user_pool_id = aws_cognito_user_pool.this.id
}

# ---------------------------------------------------------------------------
# Resource server + custom API scopes (the JWT `scope` claim vocabulary).
# ---------------------------------------------------------------------------
resource "aws_cognito_resource_server" "api" {
  identifier   = var.resource_server_identifier
  name         = "${local.name_prefix}-api"
  user_pool_id = aws_cognito_user_pool.this.id

  dynamic "scope" {
    for_each = var.api_scopes
    content {
      scope_name        = scope.value.scope_name
      scope_description = scope.value.scope_description
    }
  }
}

# ---------------------------------------------------------------------------
# Machine-to-machine app clients (one per AI agent).
# client_credentials only — NO user-auth flows, NO callback URLs. Secret is
# generated and shipped straight to Secrets Manager.
# ---------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "agent" {
  for_each = toset(var.agent_clients)

  name         = "${local.name_prefix}-m2m-${each.key}"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = local.m2m_scope_identifiers
  supported_identity_providers         = ["COGNITO"]

  # Minimal: client_credentials does not use any explicit (user) auth flow.
  explicit_auth_flows = []

  # Short-lived access tokens; agents re-fetch as needed. No refresh tokens
  # are issued for client_credentials.
  access_token_validity = 1
  id_token_validity     = 1
  token_validity_units {
    access_token = "hours"
    id_token     = "hours"
  }

  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}

# ---------------------------------------------------------------------------
# Human SPA client — Authorization Code + PKCE. Public client (no secret).
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
# Secrets Manager: one secret per M2M client holding its generated client
# secret. Only ARNs are exported — secret VALUES never leave AWS.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "agent_client" {
  for_each = toset(var.agent_clients)

  name        = "${local.name_prefix}/cognito/${each.key}"
  description = "Cognito M2M client credentials for the '${each.key}' agent (Spec Server)."

  # Fast recovery in dev; the value is reproducible from Cognito anyway.
  recovery_window_in_days = 7

  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "agent_client" {
  for_each = toset(var.agent_clients)

  secret_id = aws_secretsmanager_secret.agent_client[each.key].id
  secret_string = jsonencode({
    client_id      = aws_cognito_user_pool_client.agent[each.key].id
    client_secret  = aws_cognito_user_pool_client.agent[each.key].client_secret
    token_endpoint = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
    scopes         = local.m2m_scope_identifiers
  })
}

# ---------------------------------------------------------------------------
# Outputs — consumed by AUTH-2 (app JWKS validator) and AUTH-3 (API GW JWT
# authorizer). NO secret values here, only ARNs.
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

output "cognito_resource_server_identifier" {
  description = "Resource-server identifier = the expected JWT `aud`/scope prefix. AUTH-3/AUTH-2 check that granted scopes (e.g. <id>/tasks.write) are present."
  value       = aws_cognito_resource_server.api.identifier
}

output "cognito_api_scope_identifiers" {
  description = "Full custom scope strings (<identifier>/<scope_name>) the API understands. AUTH-3 maps routes to these scopes."
  value       = aws_cognito_resource_server.api.scope_identifiers
}

output "cognito_hosted_ui_domain" {
  description = "Hosted-UI base URL. Human SPA points its OAuth authorize/logout endpoints here; agents POST to <domain>/oauth2/token."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "cognito_token_endpoint" {
  description = "OAuth2 token endpoint. M2M agents POST client_credentials here to mint a JWT."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
}

output "cognito_ui_client_id" {
  description = "Public SPA (human) app client ID for the Authorization Code + PKCE flow."
  value       = aws_cognito_user_pool_client.ui.id
}

output "cognito_m2m_client_ids" {
  description = "Map of agent name -> M2M app client ID. (Client SECRETS are only in Secrets Manager, never output.)"
  value       = { for k, c in aws_cognito_user_pool_client.agent : k => c.id }
}

output "cognito_m2m_secret_arns" {
  description = "Map of agent name -> Secrets Manager secret ARN holding that agent's client_id/client_secret. Grant each agent's execution role secretsmanager:GetSecretValue on its own ARN only."
  value       = { for k, s in aws_secretsmanager_secret.agent_client : k => s.arn }
}
