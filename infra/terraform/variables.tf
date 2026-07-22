# variables.tf
# Inputs for the Spec Server durable stack. Cost-minimal defaults; override in
# terraform.tfvars (gitignored) — see terraform.tfvars.example.

variable "aws_region" {
  description = "AWS region for all regional resources. us-east-1 is required for ACM certs fronting CloudFront (INFRA-6) and for Cost Anomaly Detection."
  type        = string
  default     = "us-east-1"
}

variable "owner" {
  description = "Human/team accountable for this stack. Applied as the mandatory `owner` tag."
  type        = string
}

variable "project" {
  description = "Project slug. Applied as the mandatory `project` tag and used as a name prefix."
  type        = string
  default     = "spec-server"
}

variable "environment" {
  description = "Deployment environment (dev|prod). Part of resource names so multiple envs can coexist."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be one of: dev, prod."
  }
}

variable "budget_monthly_usd" {
  description = "Monthly cost budget ceiling (USD). Kept low on purpose — this is a small, always-cheap service. Alerts fire at 80% and 100% of forecast."
  type        = number
  default     = 20

  validation {
    condition     = var.budget_monthly_usd > 0 && var.budget_monthly_usd <= 500
    error_message = "budget_monthly_usd must be > 0 and <= 500 (this service is meant to stay cheap)."
  }
}

variable "alert_email" {
  description = "Email address subscribed to budget + cost-anomaly SNS alerts. Requires confirming the SNS subscription email on first apply."
  type        = string
}

# ---------------------------------------------------------------------------
# Local tag set — the MANDATORY tags applied to (nearly) every resource.
# Durable resources are transient=false. Preview/ephemeral resources are NOT
# managed here (they are created via CLI/boto3 with transient=true + expiry).
# ---------------------------------------------------------------------------
locals {
  name_prefix = "${var.project}-${var.environment}"

  tags = {
    project     = var.project
    owner       = var.owner
    managed-by  = "terraform"
    transient   = "false"
    environment = var.environment
  }
}

variable "enable_cost_anomaly" {
  description = "Create the Cost Anomaly Detection monitor/subscription. Off by default: AWS caps dimensional monitors per account, and the Budget already alerts. Enable only where a free monitor slot exists."
  type        = bool
  default     = false
}
