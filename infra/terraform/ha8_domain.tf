# ha8_domain.tf
# =============================================================================
# HA-8 — custom domains fronted by Cloudflare.
#   UI:  spec.elasticninja.com      -> CloudFront (aws_acm_certificate.ui, us-east-1)
#   API: api.spec.elasticninja.com  -> API Gateway custom domain (this REGIONAL cert)
#
# Both are proxied (orange-cloud) at Cloudflare. The UI cert is created in
# cloudfront.tf (gated on var.ui_domain); the API needs a REGIONAL cert in the
# stack's primary region, created here (gated on var.api_domain) and passed to
# apigw.tf via var.custom_domain_certificate_arn once validated.
#
# ACM DNS validation records + the final proxied CNAMEs are added to Cloudflare
# out-of-band (by the orchestrator via the Cloudflare API), not by Terraform.
# =============================================================================

variable "api_domain" {
  description = "Custom domain for the API behind Cloudflare (e.g. api.spec.elasticninja.com). Empty disables the regional ACM cert. Pair with var.custom_domain (apigw.tf) once the cert is validated."
  type        = string
  default     = ""
}

resource "aws_acm_certificate" "api" {
  count             = var.api_domain != "" ? 1 : 0
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = local.tags
}

output "api_certificate_arn" {
  description = "ARN of the regional API ACM cert (feed into var.custom_domain_certificate_arn once validated)."
  value       = var.api_domain != "" ? aws_acm_certificate.api[0].arn : ""
}

output "api_cert_validation_records" {
  description = "DNS validation CNAMEs to add in Cloudflare (DNS-only) for the API cert."
  value = var.api_domain != "" ? [
    for o in aws_acm_certificate.api[0].domain_validation_options : {
      name  = o.resource_record_name
      value = o.resource_record_value
      type  = o.resource_record_type
    }
  ] : []
}
