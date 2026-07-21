# cloudfront.tf
# =============================================================================
# INFRA-5 / INFRA-6 — Static UI hosting: private S3 origin + CloudFront (OAC).
#
# Serves the React/Vite SPA (`ui/` -> `dist/` of static assets) over HTTPS via
# CloudFront. The S3 bucket is PRIVATE: all public access is blocked and the
# only reader is this CloudFront distribution, authenticated with Origin Access
# Control (OAC, SigV4). No public S3 principal, no website-endpoint.
#
# This file is deliberately SELF-CONTAINED (its own variables + outputs) so it
# stays merge-conflict-free with the parallel lambda/apigw work. It only reuses
# `local.tags` and `local.name_prefix` from the INFRA-1 skeleton and the
# `aws.us_east_1` provider alias (from providers.tf) for the CloudFront ACM
# certificate. Do NOT move anything from here into variables.tf/outputs.tf/etc.
#
# Cost posture: CloudFront + S3 are pennies at this scale. PriceClass_100 keeps
# edge locations to the cheapest regions. No idle cost.
#
# Publish flow (owned by deploy-coordinator, NOT this module):
#   1. `npm --prefix ui run build`  -> ui/dist/
#   2. `aws s3 sync ui/dist/ s3://<ui_bucket_name>/ --delete` (with the profile)
#   3. `aws cloudfront create-invalidation --distribution-id <ui_distribution_id>
#         --paths '/*'`  (bust the edge cache so users get the new build)
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (scoped to the UI stack; kept in this file on purpose)
# ---------------------------------------------------------------------------

variable "api_origin" {
  description = "Origin (scheme://host[:port]) of the Spec Server API that the SPA calls via fetch(). Injected into the CSP connect-src so the browser is allowed to reach it. E.g. https://api.spec-server.example.com or the API Gateway invoke URL origin."
  type        = string
  default     = "'self'"

  validation {
    condition     = can(regex("^('self'|https?://[^/ ]+)$", var.api_origin))
    error_message = "api_origin must be \"'self'\" or a scheme://host origin with no trailing path."
  }
}

variable "cognito_origins" {
  description = "Extra origins the SPA must reach for OAuth2/JWT: the Cognito Hosted-UI/token endpoint and the JWKS endpoint. Added to the CSP connect-src so login + token refresh + JWKS fetch are permitted. E.g. [\"https://spec-server-auth-xxxx.auth.us-east-1.amazoncognito.com\", \"https://cognito-idp.us-east-1.amazonaws.com\"]."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.cognito_origins : can(regex("^https://[^/ ]+$", o))])
    error_message = "each cognito_origins entry must be an https://host origin with no trailing path."
  }
}

variable "ui_domain" {
  description = "OPTIONAL custom domain for the UI (e.g. app.spec-server.example.com). Empty (default) => use the free CloudFront default cert + *.cloudfront.net domain and create NO ACM certificate, so `terraform validate`/plan works with no DNS. When set, an ACM cert is requested in us-east-1 and aliased on the distribution; deploy-coordinator completes DNS validation + the alias record."
  type        = string
  default     = ""
}

variable "ui_price_class" {
  description = "CloudFront price class. PriceClass_100 (US/CA/EU only) is the cheapest and fine for this small service."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.ui_price_class)
    error_message = "ui_price_class must be PriceClass_100, PriceClass_200, or PriceClass_All."
  }
}

variable "hsts_max_age_seconds" {
  description = "max-age for the Strict-Transport-Security header (seconds). Default 2 years, with includeSubDomains + preload."
  type        = number
  default     = 63072000
}

# ---------------------------------------------------------------------------
# Composed values — the Content-Security-Policy connect-src is assembled from
# 'self' + the API origin + the Cognito origins so the SPA can call the API and
# authenticate, but nothing else. The rest of the policy is locked down:
# only same-origin scripts/styles (matches the UI's strict CSP), inline data:
# images (Vite emits some), and no framing (frame-ancestors 'none').
# ---------------------------------------------------------------------------
locals {
  ui_connect_src = join(" ", distinct(concat(
    ["'self'", var.api_origin],
    var.cognito_origins,
  )))

  ui_csp = join(" ", [
    "default-src 'self';",
    "script-src 'self';",
    "style-src 'self';",
    "img-src 'self' data:;",
    "font-src 'self';",
    "connect-src ${local.ui_connect_src};",
    "object-src 'none';",
    "base-uri 'self';",
    "form-action 'self';",
    "frame-ancestors 'none';",
    "upgrade-insecure-requests;",
  ])

  ui_custom_domain_enabled = var.ui_domain != ""
}

# ---------------------------------------------------------------------------
# Stable-but-unique suffix so the (globally-namespaced) S3 bucket name never
# collides across accounts/environments. Same pattern as the Cognito domain.
# ---------------------------------------------------------------------------
resource "random_string" "ui_bucket_suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

# ---------------------------------------------------------------------------
# Private S3 bucket for the built SPA assets. Reached ONLY via CloudFront OAC.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "ui" {
  bucket = "${local.name_prefix}-ui-${random_string.ui_bucket_suffix.result}"
  tags   = local.tags
}

# Version the object store so a bad deploy can be rolled back and OAC reads have
# a consistent view during a sync. Cheap at this asset volume.
resource "aws_s3_bucket_versioning" "ui" {
  bucket = aws_s3_bucket.ui.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption at rest (SSE-S3 / AES256 — no KMS key cost).
resource "aws_s3_bucket_server_side_encryption_configuration" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Block ALL public access — the bucket is private; CloudFront OAC is the only
# reader. Belt-and-suspenders alongside the least-privilege bucket policy below.
resource "aws_s3_bucket_public_access_block" "ui" {
  bucket = aws_s3_bucket.ui.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enforce bucket-owner ownership (ACLs disabled) — objects synced by
# deploy-coordinator are owned by the bucket, and OAC reads work without ACLs.
resource "aws_s3_bucket_ownership_controls" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# ---------------------------------------------------------------------------
# Origin Access Control (OAC, SigV4). CloudFront signs its S3 origin requests
# with this; the bucket policy below trusts exactly this distribution's ARN.
# OAC replaces the legacy Origin Access Identity (OAI).
# ---------------------------------------------------------------------------
resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = "${local.name_prefix}-ui-oac"
  description                       = "SigV4 OAC for the Spec Server UI S3 origin (private bucket)."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ---------------------------------------------------------------------------
# Response-headers policy — strong security headers on every UI response.
#   * Content-Security-Policy: same-origin scripts/styles (matches the UI's
#     strict CSP), connect-src limited to self + API + Cognito, no framing.
#   * Strict-Transport-Security (HSTS) with includeSubDomains + preload.
#   * X-Content-Type-Options: nosniff.
#   * Referrer-Policy: strict-origin-when-cross-origin.
#   * X-Frame-Options: DENY (defence-in-depth alongside frame-ancestors).
#   * Permissions-Policy: deny all powerful features by default.
# ---------------------------------------------------------------------------
resource "aws_cloudfront_response_headers_policy" "ui" {
  name    = "${local.name_prefix}-ui-security-headers"
  comment = "Strong security headers (CSP/HSTS/nosniff/Referrer/Permissions) for the Spec Server UI."

  security_headers_config {
    content_security_policy {
      content_security_policy = local.ui_csp
      override                = true
    }

    strict_transport_security {
      access_control_max_age_sec = var.hsts_max_age_seconds
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_type_options {
      override = true # emits X-Content-Type-Options: nosniff
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }
  }

  # Permissions-Policy has no first-class block; set it as a custom header.
  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "accelerometer=(), autoplay=(), camera=(), display-capture=(), encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
      override = true
    }
  }
}

# ---------------------------------------------------------------------------
# OPTIONAL ACM certificate for a custom domain. MUST live in us-east-1 for
# CloudFront (via the aws.us_east_1 alias). Gated behind var.ui_domain with
# count so `terraform validate`/plan pass with no domain + no DNS. DNS
# validation + the alias DNS record are completed by deploy-coordinator.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate" "ui" {
  count             = local.ui_custom_domain_enabled ? 1 : 0
  provider          = aws.us_east_1
  domain_name       = var.ui_domain
  validation_method = "DNS"
  tags              = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# CloudFront distribution. S3 origin via OAC, HTTPS-only, compressed, and a
# SPA custom-error mapping so client-side routes resolve.
# ---------------------------------------------------------------------------
resource "aws_cloudfront_distribution" "ui" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${local.name_prefix} static UI (React SPA)"
  default_root_object = "index.html"
  price_class         = var.ui_price_class

  # Custom aliases only when a domain is configured; otherwise the default
  # *.cloudfront.net domain is used.
  aliases = local.ui_custom_domain_enabled ? [var.ui_domain] : []

  origin {
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_id                = "s3-ui"
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-ui"
    viewer_protocol_policy = "redirect-to-https" # HTTPS-only
    compress               = true                # gzip/brotli at the edge

    allowed_methods = ["GET", "HEAD", "OPTIONS"]
    cached_methods  = ["GET", "HEAD"]

    response_headers_policy_id = aws_cloudfront_response_headers_policy.ui.id

    # AWS-managed cache/origin-request policies (no cost, no maintenance):
    #   CachingOptimized     — sensible static-asset caching.
    #   CORS-S3Origin        — forwards the headers S3+OAC needs, nothing more.
    cache_policy_id          = data.aws_cloudfront_cache_policy.optimized.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.cors_s3.id
  }

  # ---- SPA client-side routing ----------------------------------------------
  # A hard refresh on a deep link (e.g. /projects/spec-server) asks S3 for a key
  # that doesn't exist -> S3+OAC returns 403 (and 404 for truly-missing keys).
  # Rewrite BOTH to /index.html with a 200 so the React router can take over and
  # render the route. Assets (hashed .js/.css) still 200 normally.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # Default: free CloudFront cert on *.cloudfront.net. When a custom domain is
    # set, use the ACM cert (us-east-1) with SNI + modern TLS.
    cloudfront_default_certificate = local.ui_custom_domain_enabled ? null : true
    acm_certificate_arn            = local.ui_custom_domain_enabled ? aws_acm_certificate.ui[0].arn : null
    ssl_support_method             = local.ui_custom_domain_enabled ? "sni-only" : null
    minimum_protocol_version       = local.ui_custom_domain_enabled ? "TLSv1.2_2021" : null
  }

  tags = local.tags
}

# AWS-managed policies referenced by the cache behavior above.
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "cors_s3" {
  name = "Managed-CORS-S3Origin"
}

# ---------------------------------------------------------------------------
# Bucket policy: allow ONLY this CloudFront distribution (matched by its ARN via
# the AWS:SourceArn condition) to read objects. Least privilege — service
# principal cloudfront.amazonaws.com, s3:GetObject only, no public principal.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ui_bucket" {
  statement {
    sid       = "AllowCloudFrontOACRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.ui.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.ui.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = data.aws_iam_policy_document.ui_bucket.json

  # Ensure the public-access block is in place before we attach any policy.
  depends_on = [aws_s3_bucket_public_access_block.ui]
}

# ---------------------------------------------------------------------------
# Outputs (consumed by deploy-coordinator for the sync + invalidation, and for
# wiring DNS to a custom domain).
# ---------------------------------------------------------------------------
output "ui_bucket_name" {
  description = "Private S3 bucket holding the built SPA. deploy-coordinator runs `aws s3 sync ui/dist/ s3://<this>/ --delete`."
  value       = aws_s3_bucket.ui.bucket
}

output "ui_distribution_id" {
  description = "CloudFront distribution id. deploy-coordinator invalidates '/*' on it after each sync."
  value       = aws_cloudfront_distribution.ui.id
}

output "ui_distribution_domain_name" {
  description = "The distribution's *.cloudfront.net domain. Point a DNS CNAME/alias here (or use directly when no custom domain is set)."
  value       = aws_cloudfront_distribution.ui.domain_name
}

output "ui_origin_access_control_id" {
  description = "Id of the SigV4 Origin Access Control fronting the private S3 origin."
  value       = aws_cloudfront_origin_access_control.ui.id
}
