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
