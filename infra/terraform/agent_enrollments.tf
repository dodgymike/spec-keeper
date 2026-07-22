# agent_enrollments.tf
# =============================================================================
# ONBOARD-1 — Dedicated agent-enrollment single-use token store.
#
# Mirrors invites.tf: a DEDICATED DynamoDB table (NOT the app single-table
# `${name_prefix}-app`), because enrollment tokens are an auth artifact, not a
# storage-abstraction entity. ONBOARD-2 mints a single-use token (PutItem),
# ONBOARD-3 redeems it (GetItem + conditional UpdateItem/DeleteItem) to create
# the agent's Cognito user. Only the SHA-256 HASH of the token is ever stored
# (hash_key = token_hash) — a table dump cannot recover a live token.
#
# On-demand billing (no idle cost, scales to zero), PITR (cheap insurance
# against an accidental bulk delete), server-side encryption, and a TTL
# attribute (`expires_at`) that garbage-collects spent/expired tokens.
#
# SEC hardening: unlike the earlier invites/signups tables (which were flagged
# for lacking both guards), this table carries BOTH deletion_protection_enabled
# (blocks a raw DeleteTable API call outside Terraform) AND a lifecycle
# prevent_destroy (blocks `terraform destroy`).
#
# SELF-CONTAINED (its own outputs). It reads only `local.name_prefix` /
# `local.tags` (variables.tf). The app-Lambda IAM grant lives in iam.tf
# (extending the existing least-privilege policy), so this file never edits it.
# =============================================================================

# ---------------------------------------------------------------------------
# The agent-enrollments table (dedicated; NOT the app single-table store).
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "agent_enrollments" {
  name         = "${local.name_prefix}-agent-enrollments"
  billing_mode = "PAY_PER_REQUEST" # on-demand: no idle cost, scales to zero
  hash_key     = "token_hash"

  # Only the key attribute is declared; every other attribute (status,
  # agent_name, groups, created_at, expires_at) is schema-less.
  attribute {
    name = "token_hash"
    type = "S"
  }

  # TTL: DynamoDB garbage-collects spent/expired tokens once expires_at passes.
  # The redeem step (ONBOARD-3) ALSO bounds on expires_at in its conditional
  # write, so an expired-but-not-yet-swept token still cannot be redeemed (TTL
  # deletion can lag).
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Enrollment tokens are short-lived, but PITR is cheap insurance against an
  # accidental bulk delete and mirrors the app table's posture.
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  # Belt-and-suspenders against accidental deletion: DynamoDB-side deletion
  # protection (blocks a raw DeleteTable API call outside Terraform) + the
  # Terraform lifecycle guard (blocks `terraform destroy`).
  deletion_protection_enabled = true

  tags = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "agent_enrollments_table_name" {
  description = "Name of the dedicated agent-enrollments DynamoDB table. Wired to the app Lambda as AGENT_ENROLLMENTS_TABLE so the mint/redeem endpoints can create/consume single-use enrollment tokens."
  value       = aws_dynamodb_table.agent_enrollments.name
}

output "agent_enrollments_table_arn" {
  description = "ARN of the agent-enrollments DynamoDB table."
  value       = aws_dynamodb_table.agent_enrollments.arn
}
