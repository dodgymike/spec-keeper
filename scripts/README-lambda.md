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
- **`../requirements.lock`** — INFRA-7: the hash-pinned, full transitive-dependency
  closure of `requirements.txt`, generated with `pip-compile --generate-hashes`.
  This is the **source of truth for what actually gets built into the zip**;
  `build_lambda.sh` installs from it with `--require-hashes` so the artifact
  (and its `source_code_hash`) is reproducible across rebuilds/machines.
  `requirements.txt` stays the short, human-readable list of *direct* deps —
  it is not fed to pip directly by the build unless the lock is missing.

## Regenerating `requirements.lock` (do this whenever `requirements.txt` changes)

```bash
python3 -m venv /tmp/lockvenv && source /tmp/lockvenv/bin/activate
pip install --upgrade pip pip-tools
pip-compile --generate-hashes --allow-unsafe --resolver=backtracking \
  --output-file=requirements.lock requirements.txt
```

Then **verify it actually installs for the Lambda target** before committing
(the lock is generated on whatever host runs this, but must resolve for
`python3.12` / `manylinux2014_aarch64`, the target `build_lambda.sh` installs
for):

```bash
python3 -m pip install --require-hashes \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --target /tmp/lambda_check -r requirements.lock
```

**Known caveat — pin overrides for the Lambda platform tag.** pip's
`--platform` install mode matches wheel tags *literally*; it does not expand
the manylinux glibc-version hierarchy the way auto-detected host installs do.
So a compiled package whose newest release dropped the legacy
`manylinux2014_aarch64` wheel tag (only shipping newer `manylinux_2_2x_aarch64`
tags) will resolve fine in the lock but then fail the install above. As of
this writing that applies to `greenlet` and `psycopg[binary]`, so the lock
pins them below latest with `-P greenlet==<version> -P psycopg==<version>`
(see the header comment in `requirements.lock` for the exact versions and
reasoning). If regenerating without those `-P` overrides fails the verify
step above, find the newest version of the offending package that still ships
a `manylinux2014_aarch64` (or `manylinux2014_x86_64`, for an x86_64 build)
wheel and re-pin it the same way. If `build_lambda.sh`'s `PLATFORM` is ever
changed (e.g. to a newer `manylinux_2_28_aarch64` baseline matching the
Lambda `python3.12`/AL2023 runtime's actual glibc), re-verify and drop
whichever `-P` overrides are no longer needed.

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
