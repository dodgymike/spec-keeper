# security_secrets.tf
# =============================================================================
# SEC-PII-1 (SIGNUP_PEPPER) + SEC-EDGE-1 (origin-lock secret) — Terraform wiring.
#
# Two strong secrets generated IN TERRAFORM STATE via the `random` provider
# (already used by cognito.tf `random_password.agent`). They are NEVER placed in
# tfvars, never printed, never committed:
#   - signup_pepper : the HMAC pepper for the signups email_hash. SEC-PII-1: the
#     live SIGNUP_PEPPER var is EMPTY, so email hashes are unsalted (offline
#     dictionary-reversible). lambda.tf now falls back to this generated value so
#     the pepper is NEVER empty. A provided var.signup_pepper (Secrets Manager /
#     prod override) still wins; else this strong in-state pepper is used.
#   - origin_lock : the Cloudflare<->app shared origin-header secret (SEC-EDGE-1).
#     Cloudflare injects it on the `X-Origin-Lock` header via a Transform Rule;
#     the app compares it to ORIGIN_LOCK_SECRET to reject requests that bypass
#     the CDN. Read it out with `terraform output -raw origin_lock_secret` to
#     configure the Cloudflare rule.
#
# `special = false` keeps both values env-var-safe (no quoting/escaping hazards
# in the Lambda environment block).
#
# SELF-CONTAINED on purpose (its own variable + output), so it stays merge-
# conflict-free with parallel work on lambda.tf / variables.tf / outputs.tf.
# =============================================================================

# ---------------------------------------------------------------------------
# Generated secrets (in-state; not tfvars, not printed).
# ---------------------------------------------------------------------------
resource "random_password" "signup_pepper" {
  length  = 48
  special = false
}

resource "random_password" "origin_lock" {
  length  = 48
  special = false
}

# ---------------------------------------------------------------------------
# Variable — the staged origin-lock rollout switch.
# ---------------------------------------------------------------------------
variable "origin_lock_mode" {
  description = "Origin-lock enforcement mode (SEC-EDGE-1). 'off' (default) => the app ignores the origin-lock header (first apply is a no-op for origin-lock); 'warn' => log a mismatch but still serve; 'enforce' => reject requests missing/mismatching the shared secret. Stay 'off' until the Cloudflare Transform Rule injecting X-Origin-Lock is confirmed."
  type        = string
  default     = "off"

  validation {
    condition     = contains(["off", "warn", "enforce"], var.origin_lock_mode)
    error_message = "origin_lock_mode must be one of: off, warn, enforce."
  }
}

# ---------------------------------------------------------------------------
# Output — the origin-lock secret (SENSITIVE), so the orchestrator can read it
# via `terraform output -raw origin_lock_secret` to set the Cloudflare rule.
# The pepper is deliberately NOT output.
# ---------------------------------------------------------------------------
output "origin_lock_secret" {
  description = "The Cloudflare<->app shared origin-lock header secret (SEC-EDGE-1). Configure the Cloudflare Transform Rule to inject this as X-Origin-Lock. Read with: terraform output -raw origin_lock_secret."
  value       = random_password.origin_lock.result
  sensitive   = true
}
