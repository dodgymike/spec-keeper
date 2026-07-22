# apigw.tf
# =============================================================================
# INFRA-3 (edge) + AUTH-3 (JWT authorizer) — API Gateway HTTP API in front of
# the app Lambda, with a Cognito JWT authorizer.
#
# SELF-CONTAINED on purpose (its own variables + outputs + the shared JWT
# audience local). Reads only `local.name_prefix`/`local.tags` (INFRA-1), the
# lambda.tf function, and the cognito.tf user-pool + app-client resources. Do
# NOT move anything into variables.tf / outputs.tf / main.tf — keeps this
# conflict-free with parallel cloudfront.tf.
#
# Why HTTP API (not REST): cheaper per request, native JWT authorizer, lower
# latency — the right fit for a small always-cheap service (cost posture).
#
# -----------------------------------------------------------------------------
# ROUTE / AUTH SPLIT
# -----------------------------------------------------------------------------
# PUBLIC (no authorizer) — liveness/readiness + the API contract docs, so agents
# and humans can discover the interface before authenticating:
#     GET /readyz        GET /healthz        GET /openapi.json      GET /docs
#
# AUTHENTICATED (Cognito JWT authorizer required) — the whole data-plane:
#     ANY /api/v1/{proxy+}
#
# The HTTP API JWT authorizer verifies signature (against Cognito JWKS), `iss`,
# `exp`, and that `aud`/`client_id` is one of the configured audiences. It does
# NOT do per-route scope checks — HTTP API authorizers cannot express a
# per-route required-scope array natively. So FINE-GRAINED SCOPE ENFORCEMENT
# (tasks.read vs tasks.write vs projects.admin) happens IN THE APP (AUTH-2),
# which reads the validated `scope` claim off the request. Intended mapping the
# app enforces (documented here for the contract; cognito.tf defines the scope
# strings via `cognito_api_scope_identifiers`):
#
#     GET    /api/v1/**                      -> requires  tasks.read
#     POST/PATCH/DELETE /api/v1/.../tasks**  -> requires  tasks.write
#       (claim-next / complete / release / reserve are writes)
#     projects/epics/reservations admin      -> requires  projects.admin
#
# The authorizer is the coarse gate (valid Cognito token, right audience); the
# app is the fine gate (right scope for the route+method).
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to INFRA-3 edge; kept in this file on purpose)
# ---------------------------------------------------------------------------

variable "custom_domain" {
  description = "Optional custom domain for the API (e.g. api.spec-server.example.com). Empty (default) disables the custom-domain resources so `terraform validate`/`plan` succeed offline without ACM/DNS. When set, also provide var.custom_domain_certificate_arn; deploy-coordinator wires the DNS record."
  type        = string
  default     = ""
}

variable "custom_domain_certificate_arn" {
  description = "ACM certificate ARN (in this API's region) covering var.custom_domain. Required only when var.custom_domain is set. Cannot be validated offline, hence gated behind count."
  type        = string
  default     = ""
}

variable "apigw_log_retention_days" {
  description = "CloudWatch retention for the API Gateway access-log group. Finite so access logs never accrue cost forever."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------------------
# The JWT audiences accepted by BOTH the API GW authorizer and the app-level
# validator (lambda.tf reads it too for COGNITO_AUDIENCE) are defined in
# cognito.tf as `local.cognito_agent_audiences` = [agents client id, UI client
# id]. AUTH-9 replaced the old per-agent M2M client_ids with that pair.
# ---------------------------------------------------------------------------
locals {
  # Public routes (no authorizer): health/readiness + the OpenAPI contract/docs,
  # plus the HA-7 public signup queue surface. POST /api/v1/signup (uniform-202
  # anti-enumeration intake) and GET /api/v1/validate (magic-link redeem) are
  # PUBLIC BY DESIGN — the public request page is unauthenticated. They are
  # listed here explicitly so the JWT-authorized `ANY /api/v1/{proxy+}` route
  # below does NOT capture them; the app itself does no auth on these two.
  public_routes = [
    "GET /readyz",
    "GET /healthz",
    "GET /openapi.json",
    "GET /docs",
    "POST /api/v1/signup",
    "GET /api/v1/validate",
  ]
}

# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
variable "cors_allow_origins" {
  description = "Browser origins allowed to call the API (the SPA host). API Gateway answers the CORS preflight itself BEFORE the JWT authorizer (which would 401 the token-less OPTIONS) and injects the headers on responses. Empty disables gateway CORS."
  type        = list(string)
  default     = []
}

resource "aws_apigatewayv2_api" "http" {
  name          = "${local.name_prefix}-api"
  description   = "Spec Server HTTP API — Lambda proxy, Cognito JWT authorizer on the data-plane."
  protocol_type = "HTTP"

  # Gateway-level CORS: HTTP API answers the OPTIONS preflight without invoking
  # the authorizer/Lambda, so the browser's cross-origin check passes, and adds
  # Access-Control-Allow-Origin to the actual (authorized) responses. Credentials
  # are false because the SPA sends a Bearer token in a header, not a cookie.
  dynamic "cors_configuration" {
    for_each = length(var.cors_allow_origins) > 0 ? [1] : []
    content {
      allow_origins     = var.cors_allow_origins
      allow_methods     = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD"]
      allow_headers     = ["authorization", "content-type", "if-match"]
      expose_headers    = ["etag"]
      allow_credentials = false
      max_age           = 3600
    }
  }

  tags = local.tags
}

# --- Lambda proxy integration (payload format 2.0). The WSGI adapter in the
# function must speak API Gateway HTTP API v2.0 events. ---
resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.app.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

# ---------------------------------------------------------------------------
# AUTH-3 — Cognito JWT authorizer. issuer = Cognito OIDC issuer; audience = the
# accepted client_ids; identity source = the Authorization header (Bearer JWT).
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.http.id
  authorizer_type  = "JWT"
  name             = "${local.name_prefix}-cognito-jwt"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.this.endpoint}"
    audience = local.cognito_agent_audiences
  }
}

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# PUBLIC routes — no authorizer. All target the same Lambda proxy integration.
resource "aws_apigatewayv2_route" "public" {
  for_each = toset(local.public_routes)

  api_id             = aws_apigatewayv2_api.http.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

# AUTHENTICATED data-plane — the whole /api/v1 surface behind the JWT authorizer.
# Explicit methods (NOT `ANY`) so OPTIONS is left UNrouted: with cors_configuration
# on the API, API Gateway then answers the CORS preflight itself, unauthenticated
# (an `ANY` route would capture OPTIONS and the JWT authorizer would 401 the
# token-less preflight, breaking the browser's cross-origin call).
resource "aws_apigatewayv2_route" "api" {
  for_each           = toset(["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"])
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = "${each.value} /api/v1/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

# NOTE: no $default route on purpose — only the explicit routes above reach the
# Lambda. Unmatched paths get a 404 from API Gateway and never invoke compute,
# which also avoids an unauthenticated catch-all.

# ---------------------------------------------------------------------------
# Access-log group + $default stage (auto-deploy).
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "apigw_access" {
  name              = "/aws/apigateway/${local.name_prefix}-api"
  retention_in_days = var.apigw_log_retention_days

  tags = local.tags
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_access.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      httpMethod       = "$context.httpMethod"
      routeKey         = "$context.routeKey"
      path             = "$context.path"
      status           = "$context.status"
      protocol         = "$context.protocol"
      responseLength   = "$context.responseLength"
      sourceIp         = "$context.identity.sourceIp"
      requestTime      = "$context.requestTime"
      integrationError = "$context.integrationErrorMessage"
      authorizerError  = "$context.authorizer.error"
    })
  }

  # Light, cost-bounded throttling — this is a low-RPS service; keeps a runaway
  # client from generating surprise cost while leaving ample headroom.
  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 100
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Permission for API Gateway to invoke the Lambda. Scoped to this API's ARN.
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------
# OPTIONAL custom domain (gated by var.custom_domain). Needs ACM + DNS that
# cannot be validated offline, so it is count-gated OFF by default.
# deploy-coordinator creates the DNS alias/CNAME to the regional target below.
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_domain_name" "this" {
  count       = var.custom_domain != "" ? 1 : 0
  domain_name = var.custom_domain

  domain_name_configuration {
    certificate_arn = var.custom_domain_certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = local.tags
}

resource "aws_apigatewayv2_api_mapping" "this" {
  count       = var.custom_domain != "" ? 1 : 0
  api_id      = aws_apigatewayv2_api.http.id
  domain_name = aws_apigatewayv2_domain_name.this[0].id
  stage       = aws_apigatewayv2_stage.default.id
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "api_endpoint" {
  description = "Invoke URL of the HTTP API ($default stage). Base for /readyz, /docs, /api/v1/**."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "apigw_authorizer_id" {
  description = "ID of the Cognito JWT authorizer guarding the /api/v1 data-plane (AUTH-3)."
  value       = aws_apigatewayv2_authorizer.jwt.id
}

output "apigw_custom_domain_target" {
  description = "Regional target hostname for the custom domain (empty unless var.custom_domain is set). deploy-coordinator points DNS here."
  value       = var.custom_domain != "" ? aws_apigatewayv2_domain_name.this[0].domain_name_configuration[0].target_domain_name : ""
}
