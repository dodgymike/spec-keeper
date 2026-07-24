# dynamodb.tf
# =============================================================================
# INFRA-2 / SLS-3.0 — Spec Server single-table DynamoDB store + GSIs.
#
# Self-contained on purpose: this file declares its own table, GSIs, outputs
# (and would declare its own variables if it needed any). It only reads the
# shared `local.name_prefix` and `local.tags` from variables.tf so it stays
# conflict-free with a parallel cognito.tf. Do NOT move anything from here into
# variables.tf / outputs.tf / main.tf.
#
# Design source of truth: STORAGE_ABSTRACTION_DEEPDIVE.md (SLS-1), sections
# 3.1 (key convention / item shapes) and 3.2 (the 5 GSIs). One table holds every
# entity for every project under partition `P#<slug>`; a task item and its
# children share the `TASK#<public_id>` SK prefix so one Query returns the task
# plus its commits/notes/relations. No access pattern requires a Scan.
#
# Cost posture: PAY_PER_REQUEST (on-demand) — scales to zero, no provisioned
# capacity to forget about; PITR on so `terraform destroy` can never silently
# drop the backlog. Durable resource: transient=false (via provider
# default_tags), prevent_destroy + deletion_protection_enabled on.
#
# ---------------------------------------------------------------------------
# GSI name allocation — dogfooded via the Spec Server reservations API
# (POST /api/v1/projects/spec-server/reservations, namespace "dynamo-gsi",
# reserved_by aws-infra). Returned monotonic values -> index names:
#   value 1 -> GSI1  (claim / status)
#   value 2 -> GSI2  (owner, sparse)
#   value 3 -> GSI3  (task-key, sparse)
#   value 4 -> GSI4  (feed: events / notes)
#   value 5 -> GSI5  (all-projects)
#   value 6 -> GSI6  (project membership: list a principal's projects, ISO-1/2)
#   value 7 -> GSI7  (change-log: per-project ascending seq delta feed, UI-DELTA)
# The reservation is monotonic + collision-proof, so these names are stable and
# were not hand-picked by reading max+1.
# =============================================================================

resource "aws_dynamodb_table" "app" {
  name         = "${local.name_prefix}-app"
  billing_mode = "PAY_PER_REQUEST" # on-demand: no idle cost, scales to zero
  hash_key     = "PK"
  range_key    = "SK"

  # --- key + GSI key attributes (only keys are declared; all other item
  # attributes are schema-less). All String (S) per the design's string keys. ---
  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  # GSI1 claim/status: PK P#<slug>#ST#<status>, SK <priority_rank>#<pos>#<pubid>
  attribute {
    name = "GSI1PK"
    type = "S"
  }
  attribute {
    name = "GSI1SK"
    type = "S"
  }

  # GSI2 owner (sparse): PK P#<slug>#OWN#<owner>, SK TASK#<pubid>
  attribute {
    name = "GSI2PK"
    type = "S"
  }
  attribute {
    name = "GSI2SK"
    type = "S"
  }

  # GSI3 task-key (sparse): PK P#<slug>#KEY#<key>, SK TASK#<pubid>
  attribute {
    name = "GSI3PK"
    type = "S"
  }
  attribute {
    name = "GSI3SK"
    type = "S"
  }

  # GSI4 feed: PK P#<slug>#FEED#<kind>, SK <ts>#<uuid>
  attribute {
    name = "GSI4PK"
    type = "S"
  }
  attribute {
    name = "GSI4SK"
    type = "S"
  }

  # GSI5 all-projects: PK PROJECTS (constant), SK <slug>
  attribute {
    name = "GSI5PK"
    type = "S"
  }
  attribute {
    name = "GSI5SK"
    type = "S"
  }

  # GSI6 project membership (sparse): PK MEMBER#<sub>, SK <project-slug>
  attribute {
    name = "GSI6PK"
    type = "S"
  }
  attribute {
    name = "GSI6SK"
    type = "S"
  }

  # GSI7 change-log (sparse): PK P#<slug>#CHANGES, SK <zero-padded seq> (UI-DELTA)
  attribute {
    name = "GSI7PK"
    type = "S"
  }
  attribute {
    name = "GSI7SK"
    type = "S"
  }

  # --- GSI1: claim-next candidate query + list_tasks?status= (ordered by
  # priority_rank#position). Serves the hot claim path and the primary
  # status-filtered task listing that returns full task cards -> ALL. ---
  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  # --- GSI2: "my specs" (list_tasks?owner=). Sparse — GSI2PK/GSI2SK are written
  # only when a task is claimed and removed on release, so write amplification is
  # bounded to claimed tasks. Returns full task cards -> ALL. ---
  global_secondary_index {
    name            = "GSI2"
    hash_key        = "GSI2PK"
    range_key       = "GSI2SK"
    projection_type = "ALL"
  }

  # --- GSI3: get_task by human key. Sparse — present only when key != null. The
  # query resolves key -> (PK,SK) and the adapter then GetItems the base item +
  # children, so only the keys are needed -> KEYS_ONLY (cheapest projection). ---
  global_secondary_index {
    name            = "GSI3"
    hash_key        = "GSI3PK"
    range_key       = "GSI3SK"
    projection_type = "KEYS_ONLY"
  }

  # --- GSI4: newest-first feeds (list_events, list_project_notes). Feed items
  # (events/notes) are small and self-contained; the feed must return their
  # bodies without a follow-up read -> ALL. ---
  global_secondary_index {
    name            = "GSI4"
    hash_key        = "GSI4PK"
    range_key       = "GSI4SK"
    projection_type = "ALL"
  }

  # --- GSI5: list_projects (PK constant "PROJECTS", SK slug). Only project-meta
  # items carry GSI5 keys, so the partition holds one small item per project and
  # must return the full ProjectDTO -> ALL. ---
  global_secondary_index {
    name            = "GSI5"
    hash_key        = "GSI5PK"
    range_key       = "GSI5SK"
    projection_type = "ALL"
  }

  # --- GSI6: list a principal's projects (list_projects_for_principal, ISO-1).
  # Sparse — GSI6PK/GSI6SK are written only on MEMBER#<sub> items. The adapter
  # queries this index and builds MemberDTOs (project_slug + role + name +
  # created_at) straight from the projected rows with NO follow-up GetItem, so
  # the non-key member attributes must be projected -> ALL. ---
  global_secondary_index {
    name            = "GSI6"
    hash_key        = "GSI6PK"
    range_key       = "GSI6SK"
    projection_type = "ALL"
  }

  # --- GSI7: per-project change-log delta feed (list_changes, UI-DELTA). Sparse —
  # GSI7PK/GSI7SK are written only on CHANGE#<seq> items. The delta query reads
  # "seq > cursor" ascending and returns the change entry (incl. the lean snapshot)
  # straight off the index with NO follow-up GetItem -> project ALL. ---
  global_secondary_index {
    name            = "GSI7"
    hash_key        = "GSI7PK"
    range_key       = "GSI7SK"
    projection_type = "ALL"
  }

  # TTL: garbage-collects expired lease-history items (`...#LEASE#<ts>`). NOTE:
  # per SLS-1 §3.3, TTL is GC-only — lease *reclaim* is done inline by the
  # claim's conditional write, never by TTL (TTL deletion can lag up to ~48h).
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Backlog must never be silently lost.
  point_in_time_recovery {
    enabled = true
  }

  # Encryption at rest. DynamoDB always encrypts at rest with an AWS-owned key at
  # no cost; enabling this block opts into the AWS-managed KMS CMK (aws/dynamodb)
  # for auditable, key-policy-visible encryption. `kms_key_arn` is intentionally
  # omitted so no customer-managed key (and its monthly cost) is created.
  server_side_encryption {
    enabled = true
  }

  # Belt-and-suspenders against accidental deletion of the data table: Terraform
  # lifecycle guard (blocks `terraform destroy`) + DynamoDB-side deletion
  # protection (blocks a raw DeleteTable API call outside Terraform).
  deletion_protection_enabled = true

  tags = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Outputs. Consumed by INFRA-4 (app Lambda IAM policy): the policy MUST be
# scoped to exactly this table ARN + "<arn>/index/*" and NOTHING wider — least
# privilege, no wildcards on resource ARNs.
# ---------------------------------------------------------------------------
output "dynamodb_table_name" {
  description = "Name of the Spec Server single-table store. Wire to the app Lambda as DYNAMODB_TABLE."
  value       = aws_dynamodb_table.app.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table. INFRA-4 scopes the app Lambda's dynamodb:*Item/Query actions to this exact ARN."
  value       = aws_dynamodb_table.app.arn
}

output "dynamodb_table_index_arn_pattern" {
  description = "Wildcard ARN for the table's GSIs. Grant Query/GetItem on this alongside the table ARN — and nothing wider."
  value       = "${aws_dynamodb_table.app.arn}/index/*"
}

output "dynamodb_gsi_names" {
  description = "GSI names in reserved order: GSI1 claim/status, GSI2 owner, GSI3 task-key, GSI4 feed, GSI5 all-projects, GSI6 project-membership, GSI7 change-log."
  value       = ["GSI1", "GSI2", "GSI3", "GSI4", "GSI5", "GSI6", "GSI7"]
}
