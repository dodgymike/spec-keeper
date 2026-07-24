# backend.tf
# Remote state: S3 bucket for state + DynamoDB table for state locking.
#
# CHICKEN-AND-EGG: the S3 state bucket and the DynamoDB lock table must exist
# BEFORE `terraform init` can store state remotely here. They are bootstrapped
# ONCE, out-of-band, by a human/deploy-coordinator (see ../README.md ->
# "One-time remote-state bootstrap"). We deliberately do NOT manage the state
# bucket/lock table as resources inside this same root module — a module cannot
# cleanly create the backend it is already using.
#
# This block is intentionally a PARTIAL configuration: no bucket/key/region/
# table values are hardcoded (that keeps environment-specific names and any
# account identifiers out of git). Supply them at init time:
#
#   terraform init -backend-config=backend.hcl
#
# See backend.hcl.example for the expected keys. backend.hcl itself is
# gitignored.

terraform {
  backend "s3" {
    # Values provided via -backend-config=backend.hcl :
    #   bucket         = "spec-server-tfstate-<suffix>"
    #   key            = "spec-server/terraform.tfstate"
    #   region         = "us-east-1"
    #   dynamodb_table = "spec-server-tflock"
    #   encrypt        = true
  }
}

# ---------------------------------------------------------------------------
# Remote-state backing resources, now IMPORTED into management so they carry
# the mandatory cost-allocation tags (they were previously created out-of-band
# and left untagged). These were bootstrapped ONCE before the first remote
# `terraform init` (the chicken-and-egg noted above); we do NOT recreate them.
#
# These are the most DURABLE resources in the account: the S3 bucket holds this
# very Terraform state and the DynamoDB table holds its lock. They are
# `prevent_destroy = true` and must NEVER be tagged transient=true or touched by
# the reaper. The ONLY acceptable change to them from Terraform is in-place tag
# additions — any plan proposing destroy/replace here is a bug, stop.
#
# Standard tags (project / owner / managed-by=terraform / transient=false /
# environment) come from the provider `default_tags` (see providers.tf); the
# per-resource tags below add `protect=true` (reaper exemption) + a `purpose`.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tfstate" {
  bucket = "spec-server-tfstate-${data.aws_caller_identity.current.account_id}"

  # Guard against accidental `terraform destroy` of the remote-state bucket.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    protect = "true"
    purpose = "terraform-remote-state"
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tfstate_lock" {
  name         = "spec-server-tflock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  # Guard against accidental `terraform destroy` of the state-lock table.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    protect = "true"
    purpose = "terraform-state-lock"
  }
}
