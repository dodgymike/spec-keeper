# ses.tf
# =============================================================================
# HA-6 — SES transactional email for the auth Lambdas.
#
# The auth flows need to send transactional email:
#   * HA-2 invite emails (org/project invitations)
#   * HA-3 email-OTP (one-time codes for email-based login)
#
# This mirrors the bird project's terraform/ses.tf, adapted for Spec Server:
#   * The final sending domain is spec.elasticninja.com, whose DNS is on
#     CLOUDFLARE (not Route53). So — unlike bird, which writes the DKIM /
#     verification records straight into its Route53 zone — this file creates the
#     SES identity and OUTPUTS the DNS records the user must add in Cloudflare by
#     hand (see outputs `ses_domain_dkim_records` / verification below).
#   * A configuration set (`${local.name_prefix}-auth`) with reputation metrics +
#     a CloudWatch event destination, matching bird's `birdup-signup` pattern.
#   * A least-privilege managed send-policy (ses:SendEmail / ses:SendRawEmail
#     scoped to the from-identity + config-set ARNs, no wildcards) exposed by ARN
#     so HA-2 / HA-3 can ATTACH it to the auth Lambdas' execution roles once those
#     roles exist. This file does NOT create or edit those roles.
#
# SELF-CONTAINED: its own variables + outputs live here. It only reads
# `local.name_prefix` / `local.tags` (variables.tf) and `data.aws_region` /
# `data.aws_caller_identity` (main.tf). Nothing is added to variables.tf /
# outputs.tf / main.tf, so this stays merge-conflict-free with the parallel
# auth-role work.
#
# -----------------------------------------------------------------------------
# !! SANDBOX WARNING (read before wiring the Lambdas to real recipients) !!
# -----------------------------------------------------------------------------
# A brand-new SES account starts in the SES *SANDBOX*:
#   * You can ONLY send to email addresses/domains you have VERIFIED in SES.
#   * A low sending quota / rate applies (e.g. 200 msg/day, 1 msg/sec).
# So even after the identity below verifies, HA-2/HA-3 can only email verified
# test addresses until you request PRODUCTION ACCESS ("sending limit increase")
# for this region in the SES console (or via `aws sesv2 put-account-details`).
# Steps to go live:
#   1. Add the DKIM CNAMEs + (email path) confirm the verification email so the
#      identity reaches VerificationStatus=Success.
#   2. Request production access; wait for approval (removes the sandbox).
#   3. Then HA-2/HA-3 can email arbitrary recipients.
# =============================================================================

# ---------------------------------------------------------------------------
# Variables (own — gated with count/ternary so `terraform validate` passes when
# unset; the domain path is a no-op until var.ses_domain is provided).
# ---------------------------------------------------------------------------
variable "ses_from_address" {
  description = <<-EOT
    The transactional "From" email address the auth Lambdas send as
    (e.g. no-reply@spec.elasticninja.com). When non-empty, an SESv2 EMAIL
    identity is created for it — SES then sends a confirmation email to this
    address that must be clicked to verify it. If ses_domain is also set and
    covers this address, verifying the DOMAIN alone is sufficient and this email
    identity is redundant (but harmless). Set to "" to skip the email identity.
  EOT
  type        = string
  default     = "no-reply@spec.elasticninja.com"
}

variable "ses_domain" {
  description = <<-EOT
    The sending DOMAIN to verify via Easy DKIM (e.g. spec.elasticninja.com).
    PREFERRED over a single email identity: verifying the domain lets the auth
    Lambdas send as any address @<domain> and gives proper DKIM signing. When
    non-empty an SESv2 domain identity is created and the DKIM CNAME records to
    add in CLOUDFLARE are surfaced via the `ses_domain_dkim_records` output.
    Default "" so the domain path is a no-op (validate passes) until set.
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------
locals {
  ses_domain_enabled = var.ses_domain != ""
  ses_email_enabled  = var.ses_from_address != ""

  # ARN of the identity the auth Lambdas send FROM. Prefer the domain identity
  # (covers every address @<domain>); fall back to the single email identity.
  ses_from_identity_arn = local.ses_domain_enabled ? (
    aws_sesv2_email_identity.domain[0].arn
    ) : (
    local.ses_email_enabled ? aws_sesv2_email_identity.email[0].arn : ""
  )

  # Every identity ARN a send may be authorized against (domain and/or email).
  # Used as the Resource set on the send policy alongside the config-set ARN.
  ses_identity_arns = compact([
    local.ses_domain_enabled ? aws_sesv2_email_identity.domain[0].arn : "",
    local.ses_email_enabled ? aws_sesv2_email_identity.email[0].arn : "",
  ])
}

# ---------------------------------------------------------------------------
# Configuration set — reputation tracking + a CloudWatch event destination so
# sends/bounces/complaints are visible in CloudWatch metrics. Mirrors bird's
# `birdup-signup` config set. TLS is required on delivery. HA-2/HA-3 should pass
# ConfigurationSetName = this on every SendEmail call so their traffic is tracked
# here (and the send policy's Resource set below includes this config-set ARN).
# ---------------------------------------------------------------------------
resource "aws_sesv2_configuration_set" "auth" {
  configuration_set_name = "${local.name_prefix}-auth"

  reputation_options {
    reputation_metrics_enabled = true
  }

  delivery_options {
    tls_policy = "REQUIRE"
  }

  sending_options {
    sending_enabled = true
  }

  tags = local.tags
}

# CloudWatch event destination (self-contained — no SNS topic / KMS key needed).
# Emits per-event-type CloudWatch metrics dimensioned by the config set, so
# bounce/complaint/reject rates are graphable/alarmable without extra plumbing.
# An SNS destination could be swapped in later if push alerting is wanted.
resource "aws_sesv2_configuration_set_event_destination" "auth_cw" {
  configuration_set_name = aws_sesv2_configuration_set.auth.configuration_set_name
  event_destination_name = "${local.name_prefix}-auth-cw"

  event_destination {
    enabled = true
    matching_event_types = [
      "SEND",
      "REJECT",
      "BOUNCE",
      "COMPLAINT",
      "DELIVERY",
      "DELIVERY_DELAY",
    ]

    cloud_watch_destination {
      dimension_configuration {
        # Dimension the metrics by the config-set name so this stack's auth
        # email is isolated from any other SES traffic in the account.
        default_dimension_value = aws_sesv2_configuration_set.auth.configuration_set_name
        dimension_name          = "ses:configuration-set"
        dimension_value_source  = "MESSAGE_TAG"
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Sender identities.
#
# Both use aws_sesv2_email_identity: passing a bare DOMAIN creates a domain
# identity (Easy DKIM); passing an email ADDRESS creates an email identity
# (verified via a confirmation email). Each is gated with count so validate
# passes when its variable is unset. The config set is bound as the identity's
# default so sends are tracked even if a caller forgets ConfigurationSetName.
# ---------------------------------------------------------------------------

# Domain identity (PREFERRED) — spec.elasticninja.com on Cloudflare. Easy DKIM
# generates 3 CNAME tokens; add them in Cloudflare (see output below). SESv2
# uses successful DKIM resolution as the domain-verification signal (no separate
# _amazonses TXT is required for the DKIM/verification flow here).
resource "aws_sesv2_email_identity" "domain" {
  count          = local.ses_domain_enabled ? 1 : 0
  email_identity = var.ses_domain

  configuration_set_name = aws_sesv2_configuration_set.auth.configuration_set_name

  tags = local.tags
}

# Email-address identity — useful in the SANDBOX to verify a specific test
# recipient/sender without owning the domain's DNS, or as the sole sender if no
# domain is configured. Verified by clicking the confirmation email SES sends.
resource "aws_sesv2_email_identity" "email" {
  count          = local.ses_email_enabled ? 1 : 0
  email_identity = var.ses_from_address

  configuration_set_name = aws_sesv2_configuration_set.auth.configuration_set_name

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Least-privilege SEND policy (managed) — for HA-2 / HA-3 to ATTACH to the auth
# Lambdas' execution roles. Grants ONLY ses:SendEmail / ses:SendRawEmail, and
# ONLY against this stack's identity ARN(s) + the auth config-set ARN. No
# `ses:*`, no `Resource = "*"`. When a from-address is configured, sends are
# further pinned to it via the ses:FromAddress condition.
#
# NOTE: SESv2 SendEmail with a ConfigurationSetName authorizes against BOTH the
# identity ARN AND the configuration-set ARN — granting the identity alone would
# AccessDenied every send. Hence both are in the Resource set.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ses_send" {
  statement {
    sid     = "SendAuthTransactionalEmail"
    effect  = "Allow"
    actions = ["ses:SendEmail", "ses:SendRawEmail"]

    resources = concat(
      local.ses_identity_arns,
      [aws_sesv2_configuration_set.auth.arn],
    )

    # Pin the envelope From to the configured sender when one is set.
    dynamic "condition" {
      for_each = local.ses_email_enabled ? [1] : []
      content {
        test     = "StringEquals"
        variable = "ses:FromAddress"
        values   = [var.ses_from_address]
      }
    }
  }
}

resource "aws_iam_policy" "ses_send" {
  name        = "${local.name_prefix}-ses-send"
  description = "Least-privilege SES send policy for the auth Lambdas (HA-2 invites / HA-3 email-OTP). ses:SendEmail+SendRawEmail scoped to the ${local.name_prefix} SES identity + auth config set only."
  policy      = data.aws_iam_policy_document.ses_send.json

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "ses_from_identity_arn" {
  description = "ARN of the SES identity the auth Lambdas send FROM (domain identity if ses_domain set, else the email identity). Empty if neither is configured."
  value       = local.ses_from_identity_arn
}

output "ses_configuration_set_name" {
  description = "Name of the SES configuration set (<name_prefix>-auth). HA-2/HA-3 pass this as ConfigurationSetName on every SendEmail call so reputation/bounce/complaint events are tracked."
  value       = aws_sesv2_configuration_set.auth.configuration_set_name
}

output "ses_send_policy_arn" {
  description = "ARN of the least-privilege managed SES send policy. HA-2/HA-3 ATTACH this to the auth Lambdas' execution roles (aws_iam_role_policy_attachment)."
  value       = aws_iam_policy.ses_send.arn
}

output "ses_domain_dkim_records" {
  description = <<-EOT
    Easy-DKIM CNAME records to add in CLOUDFLARE for the sending domain so SES
    can verify + DKIM-sign it. Empty when ses_domain is unset. Add each as a
    CNAME (proxy/orange-cloud OFF — DNS only). Once these resolve, the domain
    identity reaches VerificationStatus=Success.
  EOT
  value = local.ses_domain_enabled ? [
    for t in aws_sesv2_email_identity.domain[0].dkim_signing_attributes[0].tokens : {
      type  = "CNAME"
      name  = "${t}._domainkey.${var.ses_domain}"
      value = "${t}.dkim.amazonses.com"
    }
  ] : []
}

output "ses_email_verification_pending" {
  description = "When an email-address identity is used, SES sends a confirmation email to this address that must be clicked to verify it before any send succeeds. Empty when no email identity is configured."
  value       = local.ses_email_enabled ? var.ses_from_address : ""
}
