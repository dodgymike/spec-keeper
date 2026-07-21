# Lambda packaging (INFRA-4)

The Spec Server's Flask app runs on AWS Lambda behind an API Gateway **HTTP API**
(payload format **2.0**). This directory ships the build side of that; the
terraform wiring lives in `infra/terraform/lambda.tf` (function
`<name_prefix>-api`, `arm64` / `python3.12`, `handler = "wsgi_lambda.handler"`).

## Pieces

- **`../wsgi_lambda.py`** — the real Lambda handler (replaces the INFRA-3
  placeholder). Adapter: **Mangum** (ASGI↔Lambda) + **a2wsgi** (WSGI↔ASGI),
  because Flask is WSGI and Mangum is ASGI. `create_app()` and the adapter are
  built at **module import** (cold start) and reused across warm invocations.
  Single-dependency alternative: `apig-wsgi` (pure WSGI, native HTTP API v2.0).
- **`build_lambda.sh`** — produces `build/lambda.zip` with aarch64/py3.12 wheels
  (`--platform manylinux2014_aarch64 --only-binary=:all:`) plus the app source at
  the archive root. `build/` is gitignored.

## Build (code-only; run by the deploy-coordinator, not committed)

```bash
bash scripts/build_lambda.sh          # -> build/lambda.zip (aarch64, py3.12)
```

Zip vs image: a zip is used (small artifact, well under the 250 MB unzipped
limit). Switch to a container image (ECR) only if the artifact outgrows that.

## Ship it (deploy-coordinator; requires AWS creds — NOT done here)

Either terraform-managed code:

```bash
cd infra/terraform
terraform apply -var lambda_zip_path="$PWD/../../build/lambda.zip"
```

…or a fast code-only push to an already-provisioned function:

```bash
aws lambda update-function-code \
  --function-name <name_prefix>-api --zip-file fileb://build/lambda.zip
```

`terraform` reads `var.lambda_zip_path` relative to `infra/terraform/`, so pass
an **absolute** path (as above). Leaving `var.lambda_zip_path` empty (default)
keeps the committed INFRA-3 placeholder bootstrap for offline
`terraform validate`/`plan`.

## Runtime notes

- Config is env-driven (set by `lambda.tf`): `STORAGE_BACKEND=dynamodb`,
  `DDB_TABLE`/`DYNAMODB_TABLE`, `COGNITO_*`. The handler never writes to CWD
  (Lambda FS is read-only except `/tmp`).
- **Schema creation / migrations are a deploy step, never run in the handler.**
  `flask init-db` / `alembic upgrade head` (Postgres) or the DynamoDB table
  (terraform `dynamodb.tf`) are provisioned out-of-band by the deploy-coordinator.
