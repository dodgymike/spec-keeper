# Spec Server — Infrastructure

AWS infrastructure for the **Spec Server** serverless stack. This is a small,
cheap, always-cheap web service: DynamoDB (on-demand), Lambda (arm64, scales to
zero), API Gateway **HTTP** API, Cognito (JWT), and CloudFront + S3 for the UI.

## Durable vs transient

- **Durable** lives in `terraform/` and is managed by Terraform: remote state,
  DynamoDB data tables + GSIs, Lambda + least-privilege IAM, API Gateway + JWT
  authorizer + custom domain, Cognito, CloudFront/S3 UI, Budgets + Cost Anomaly
  Detection + SNS, and the teardown reaper. Long-lived, changes rarely.
- **Transient** (per-branch / per-PR preview envs) is created via the **aws CLI /
  boto3**, NOT Terraform — an ephemeral Lambda alias + a scoped DynamoDB table
  prefix/throwaway table, tagged `transient=true` + `expiry=<UTC ISO>`. Kept out
  of Terraform state so the reaper can delete them without causing drift.

## Credentials — the dedicated profile (hard rule)

Every **mutating** `aws`/`terraform` command runs with the dedicated profile:

```bash
export AWS_PROFILE=spec-server-infra
```

Never mutate infra with default/SSO credentials. If `spec-server-infra` is not
configured, STOP and configure it (a scoped IAM principal for this stack) before
proceeding.

**Agents do NOT apply.** This directory is authored by the `aws-infra` agent but
**nothing is applied by agents**. `terraform apply` (and any mutating AWS call) is
owned by **deploy-coordinator**, run by a human once the `spec-server-infra`
profile exists. Agents may run read-only/offline checks only:
`terraform fmt`, and `terraform init -backend=false` + `terraform validate`.

## One-time remote-state bootstrap (chicken-and-egg)

Terraform stores its state in S3 with a DynamoDB lock table (see
`terraform/backend.tf`). Those two resources must exist **before** the first
`terraform init`, so they are created **once, out-of-band**. They are durable and
must **never** be tagged `transient=true`.

Pick a globally-unique suffix (e.g. the account ID or a random hex) and run, with
the dedicated profile:

```bash
export AWS_PROFILE=spec-server-infra
REGION=us-east-1
SUFFIX=<account-id-or-random>
BUCKET=spec-server-tfstate-$SUFFIX
LOCK=spec-server-tflock

# State bucket: versioned + encrypted + public access fully blocked.
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Lock table: on-demand billing (no idle cost), key `LockID`.
aws dynamodb create-table --table-name "$LOCK" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --tags Key=project,Value=spec-server Key=managed-by,Value=cli \
         Key=transient,Value=false Key=owner,Value=<owner>
```

> The state bucket + lock table are intentionally NOT managed inside the root
> module that uses them. Do not import them.

## Initialise Terraform (partial backend config)

`terraform/backend.tf` is a **partial** config — no bucket/key/region/table are
hardcoded. Copy the example, fill it in, and init:

```bash
cd terraform
cp backend.hcl.example backend.hcl          # gitignored
cp terraform.tfvars.example terraform.tfvars # gitignored
# edit both

export AWS_PROFILE=spec-server-infra
terraform init -backend-config=backend.hcl
```

## Plan / apply (deploy-coordinator only)

```bash
export AWS_PROFILE=spec-server-infra
terraform plan -out tfplan   # review EVERY change, especially data-table drops
terraform apply tfplan       # deploy-coordinator / human only
```

Never auto-apply changes that destroy or replace durable resources — the
DynamoDB data tables carry `prevent_destroy` + PITR and must never be dropped
silently.

## Offline checks agents may run

```bash
cd terraform
terraform fmt -recursive
terraform init -backend=false   # no backend, no creds, no network state
terraform validate
```

## Files

| File                      | Purpose                                                        |
| ------------------------- | ------------------------------------------------------------- |
| `versions.tf`             | Terraform + provider version pins (`aws ~> 5.0`, random, archive). |
| `backend.tf`              | S3 + DynamoDB remote-state backend (partial config).          |
| `backend.hcl.example`     | Template for the gitignored `backend.hcl`.                    |
| `providers.tf`            | AWS provider + `default_tags` (mandatory tag set); us-east-1 alias for ACM/CloudFront. |
| `variables.tf`            | Inputs + the merged `tags` local.                            |
| `budgets.tf`              | AWS Budgets + Cost Anomaly Detection + SNS alerts.           |
| `main.tf`                 | Stack overview + module boundaries for INFRA-2..6 / AUTH-1.   |
| `outputs.tf`              | Region, account, budget ARN, alerts topic ARN.               |
| `terraform.tfvars.example`| Template for the gitignored `terraform.tfvars`.              |

## Tagging (mandatory)

Every resource carries `project=spec-server`, `owner`, `managed-by`
(`terraform`|`cli`), `transient` (`true`|`false`). Transient resources also carry
`expiry` (UTC ISO) and optionally `protect=true`. The durable module applies the
first four via provider `default_tags`. Never tag the state bucket, lock table,
data tables, or the reaper itself `transient=true`.
