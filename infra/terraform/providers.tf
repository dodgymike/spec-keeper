# providers.tf
# AWS provider with default_tags applying the mandatory tag set to every
# taggable resource created by this module. Individual resources may add
# resource-specific tags on top (e.g. transient/expiry for preview envs — but
# note preview envs are provisioned via CLI, not this durable module).

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

# CloudFront + ACM for the UI distribution (INFRA-6) require certificates in
# us-east-1 regardless of the stack's primary region. This aliased provider is
# ready for that; resources reference it explicitly via `provider = aws.us_east_1`.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.tags
  }
}
