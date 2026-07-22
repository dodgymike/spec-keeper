# versions.tf
# Terraform + provider version pins for the Spec Server durable stack.
# Pin majors so `terraform init` upgrades stay in a known-good range.

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }

    # Used later for stable-but-unique suffixes (e.g. S3 bucket names,
    # Cognito domain prefixes) so global-namespace resources don't collide.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }

    # Used later to zip Lambda source into deployment artifacts (INFRA-4).
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
