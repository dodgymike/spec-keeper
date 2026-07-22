# main.tf
# =============================================================================
# Spec Server — durable serverless stack (Terraform-managed).
#
# This root module owns the LONG-LIVED, rarely-changing infrastructure. Ephemeral
# per-branch/per-PR preview environments are NOT here — they are provisioned via
# aws CLI/boto3 with transient=true + expiry tags and reaped by the teardown
# reaper, deliberately kept out of this state to avoid drift/churn.
#
# Intended stack (built out across INFRA-2..6 / AUTH-1):
#
#   [DATA]  DynamoDB data tables (on-demand billing, PITR on, prevent_destroy)
#           + GSIs. The task/spec backlog. Never dropped silently.        -> INFRA-2
#
#   [AUTH]  Cognito user pool + resource server + app clients (JWT). App-client
#           secrets live in Secrets Manager, referenced by ARN — never in git. -> AUTH-1
#
#   [APP]   Lambda function(s) (arm64, small package, scales to zero) with
#           least-privilege IAM roles: only the specific dynamodb:*Item/Query
#           actions on the specific table ARNs + the function's own log group. -> INFRA-4
#
#   [EDGE]  API Gateway HTTP API (not REST — cheaper) + JWT authorizer wired to
#           the Cognito pool + custom domain (ACM).                        -> INFRA-5
#
#   [UI]    S3 (private) + CloudFront distribution with Origin Access Control
#           and a security-headers response policy for the static UI.      -> INFRA-6
#
#   [OPS]   Teardown reaper: EventBridge Scheduler + Lambda that deletes
#           transient preview resources past their expiry.                 -> (reaper task)
#
#   [COST]  AWS Budgets + Cost Anomaly Detection + SNS alerts.             -> budgets.tf (this wave)
#
# Cost posture: DynamoDB on-demand, Lambda scales to zero, HTTP API, CloudFront+S3.
# Everything carries the mandatory tag set via provider default_tags.
# =============================================================================

# Handy account/region context for building ARNs and names in later tasks.
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Module boundaries / placeholders. Later tasks add resources or child modules
# here (or in dedicated files: dynamodb.tf, cognito.tf, lambda.tf, apigw.tf,
# cloudfront.tf, reaper.tf). Left intentionally empty this wave.
# ---------------------------------------------------------------------------

# INFRA-2: DynamoDB data tables + GSIs  -> dynamodb.tf
# AUTH-1 : Cognito user pool + clients  -> cognito.tf
# INFRA-4: App Lambda + IAM role        -> lambda.tf
# INFRA-5: API Gateway HTTP API + JWT   -> apigw.tf
# INFRA-6: S3 + CloudFront UI (OAC)     -> cloudfront.tf
