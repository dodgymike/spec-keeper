#!/usr/bin/env bash
# scripts/build_lambda.sh — INFRA-4 Lambda deployment-artifact builder.
# =============================================================================
# Produces the REAL Lambda zip that replaces the INFRA-3 placeholder. The output
# path is what infra/terraform/lambda.tf's `var.lambda_zip_path` consumes:
#
#     build/lambda.zip   (repo-root relative; override with $OUTPUT_ZIP)
#
# It:
#   1. installs the runtime deps (requirements.txt) for the Lambda TARGET
#      (manylinux2014_aarch64 / CPython 3.12) into a clean package dir, then
#   2. adds the app source (app/, wsgi_lambda.py, wsgi.py) at the zip TOP LEVEL
#      so the runtime resolves `handler = "wsgi_lambda.handler"`, then
#   3. zips it (best-effort deterministic: sorted entries + fixed mtimes).
#
# Zip vs container image: a zip is used because the packaged size is small and
# well under the 250 MB unzipped Lambda limit; a container image (ECR) would be
# the choice only if the artifact outgrew that. Runtime + architecture MUST match
# lambda.tf: runtime=python3.12, architectures=["arm64"] (=> aarch64 wheels).
#
# How the deploy-coordinator consumes this (NOT run here — code-only):
#     bash scripts/build_lambda.sh
#     # then EITHER (terraform-managed code):
#     cd infra/terraform && terraform apply -var lambda_zip_path="$PWD/../../build/lambda.zip"
#     # OR (fast code-only push to an already-provisioned function):
#     aws lambda update-function-code \
#       --function-name <name_prefix>-api --zip-file fileb://build/lambda.zip
# Note: terraform reads var.lambda_zip_path relative to the terraform working
# dir (infra/terraform/), so pass an ABSOLUTE path (as above) to avoid surprises.
# =============================================================================
set -euo pipefail

# --- Locations ------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build}"
PKG_DIR="$BUILD_DIR/package"
OUTPUT_ZIP="${OUTPUT_ZIP:-$BUILD_DIR/lambda.zip}"
REQUIREMENTS="${REQUIREMENTS:-$REPO_ROOT/requirements.txt}"

# --- Lambda target (MUST match infra/terraform/lambda.tf) ------------------ #
PY_VERSION="${PY_VERSION:-3.12}"
# manylinux2014_aarch64 == AWS Lambda arm64 (Graviton). For x86_64 Lambdas use
# manylinux2014_x86_64 and drop `architectures=["arm64"]` in lambda.tf.
PLATFORM="${PLATFORM:-manylinux2014_aarch64}"

echo "[build_lambda] repo root:   $REPO_ROOT"
echo "[build_lambda] output zip:  $OUTPUT_ZIP"
echo "[build_lambda] target:      py${PY_VERSION} / ${PLATFORM}"

# --- Clean --------------------------------------------------------------- #
rm -rf "$PKG_DIR" "$OUTPUT_ZIP"
mkdir -p "$PKG_DIR"

# --- 1. Install runtime deps for the Lambda target ------------------------- #
# --platform + --only-binary=:all: forces prebuilt wheels for the TARGET arch
# (never the build host's) so compiled deps (psycopg[binary], cryptography) are
# aarch64 manylinux wheels. If a required wheel is unavailable for the target,
# pip fails loudly rather than silently shipping a host-arch binary.
echo "[build_lambda] installing deps into $PKG_DIR ..."
# We install into a --target dir, not a virtualenv, so neutralise any host
# `require-virtualenv` pip setting for THIS call only (does not leak out).
PIP_REQUIRE_VIRTUALENV=false python3 -m pip install \
  --disable-pip-version-check \
  --no-cache-dir \
  --platform "$PLATFORM" \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  --upgrade \
  --target "$PKG_DIR" \
  -r "$REQUIREMENTS"

# Trim dev/test-only weight that is never imported in the Lambda request path
# (keeps the zip small => faster cold start). requirements.txt stays the single
# source of truth; we prune here rather than fork a second requirements file.
echo "[build_lambda] pruning dev-only deps + bytecode ..."
rm -rf "$PKG_DIR"/pytest "$PKG_DIR"/_pytest "$PKG_DIR"/pytest-* \
       "$PKG_DIR"/pluggy* "$PKG_DIR"/iniconfig* \
       "$PKG_DIR"/gunicorn "$PKG_DIR"/gunicorn-* 2>/dev/null || true
find "$PKG_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG_DIR" -type d -name "*.dist-info" -path "*/tests/*" -prune -exec rm -rf {} + 2>/dev/null || true

# --- 2. Add first-party source at the zip TOP LEVEL ------------------------ #
# `handler = "wsgi_lambda.handler"` => wsgi_lambda.py must sit at the archive
# root. app/ is the package it imports; wsgi.py is included for parity/debug.
echo "[build_lambda] adding app source ..."
cp "$REPO_ROOT/wsgi_lambda.py" "$PKG_DIR/"
cp "$REPO_ROOT/wsgi.py" "$PKG_DIR/"
# Copy the app package WITHOUT bytecode/caches.
( cd "$REPO_ROOT" && find app -name '__pycache__' -prune -o -type f -print \
    | while read -r f; do mkdir -p "$PKG_DIR/$(dirname "$f")"; cp "$f" "$PKG_DIR/$f"; done )

# --- 3. Zip (best-effort deterministic) ------------------------------------ #
# Fixed mtimes + sorted entries => stable source_code_hash across identical
# inputs (avoids needless Lambda redeploys). Zip has no sub-second/UID metadata
# variance once mtimes are pinned.
echo "[build_lambda] zipping ..."
find "$PKG_DIR" -exec touch -h -d "2020-01-01T00:00:00Z" {} + 2>/dev/null || true
( cd "$PKG_DIR" && find . -type f -o -type l | LC_ALL=C sort \
    | zip -q -X -y "$OUTPUT_ZIP" -@ )

SIZE="$(du -h "$OUTPUT_ZIP" | cut -f1)"
echo "[build_lambda] wrote $OUTPUT_ZIP ($SIZE)"
echo "[build_lambda] sanity: wsgi_lambda.py present at zip root:"
unzip -l "$OUTPUT_ZIP" | grep -E ' wsgi_lambda\.py$' || {
  echo "[build_lambda] ERROR: wsgi_lambda.py missing from zip root" >&2; exit 1; }
echo "[build_lambda] done."
