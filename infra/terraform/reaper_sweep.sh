#!/usr/bin/env bash
# =============================================================================
# reaper_sweep.sh — manual, Terraform-INDEPENDENT teardown sweep.
#
# Lists (then, only on explicit confirmation, deletes) every resource tagged
# transient=true whose `expiry` UTC-ISO tag is in the PAST and that is NOT
# tagged protect=true — across the configured region(s). Use this for an
# immediate cleanup between the reaper's 15-minute runs, or to verify what the
# scheduled reaper would do.
#
# ALWAYS dry-run/list first; it deletes ONLY when you pass --confirm.
#
# Teardown safety mirrors the Lambda: a durable-looking resource (name/ARN in
# the denylist) is SKIPPED and surfaced as a BUG, never deleted. protect=true is
# an absolute exemption and is surfaced too.
#
# Requires: awscli v2, jq. Mutating calls use AWS_PROFILE=spec-server-infra.
#
# Usage:
#   ./reaper_sweep.sh                       # list-only (dry run), default region
#   ./reaper_sweep.sh --regions us-east-1,eu-west-1
#   ./reaper_sweep.sh --confirm             # actually delete past-expiry resources
#
# TTL EXTEND one-liner (requires a justification recorded in your report):
#   AWS_PROFILE=spec-server-infra aws dynamodb tag-resource \
#     --resource-arn <arn> --tags Key=expiry,Value=<new-UTC-ISO>
#   AWS_PROFILE=spec-server-infra aws lambda tag-resource \
#     --resource <arn> --tags expiry=<new-UTC-ISO>
#   (NEVER extend the durable stack — it has no expiry.)
# =============================================================================
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-spec-server-infra}"

CONFIRM=0
REGIONS_CSV="${AWS_REGION:-us-east-1}"
NAME_PREFIX="${NAME_PREFIX:-spec-server-dev}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm) CONFIRM=1; shift ;;
    --regions) REGIONS_CSV="$2"; shift 2 ;;
    --name-prefix) NAME_PREFIX="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

# Substrings that mark a DURABLE resource — must be kept in lock-step with
# handler.py's DURABLE_DENY_NAMES and reaper.tf's durable_deny_arns.
DURABLE_DENY=(
  "${NAME_PREFIX}-app"
  "${NAME_PREFIX}-api"
  "${NAME_PREFIX}-ui"
  "${NAME_PREFIX}-reaper"
  "${NAME_PREFIX}-cost-alerts"
  "spec-server-tfstate"
  "spec-server-tflock"
  "userpool"
  "cognito"
)

NOW_EPOCH="$(date -u +%s)"

is_durable() {
  local arn_lc; arn_lc="$(echo "$1" | tr '[:upper:]' '[:lower:]')"
  for needle in "${DURABLE_DENY[@]}"; do
    [[ "$arn_lc" == *"$(echo "$needle" | tr '[:upper:]' '[:lower:]')"* ]] && return 0
  done
  return 1
}

# Convert a UTC-ISO tag to epoch seconds; echoes nothing on parse failure.
to_epoch() {
  local ts="$1"
  ts="${ts/Z/+0000}"
  date -u -d "$ts" +%s 2>/dev/null || true
}

REAP_ARNS=()

for REGION in ${REGIONS_CSV//,/ }; do
  echo "=================================================================="
  echo "Region: ${REGION}  (profile: ${AWS_PROFILE}, now: $(date -u -Iseconds))"
  echo "=================================================================="

  RESOURCES_JSON="$(aws resourcegroupstaggingapi get-resources \
    --region "$REGION" \
    --tag-filters Key=transient,Values=true \
    --output json)"

  COUNT="$(echo "$RESOURCES_JSON" | jq '.ResourceTagMappingList | length')"
  echo "Found ${COUNT} resource(s) tagged transient=true."
  echo

  while IFS= read -r row; do
    [[ -z "$row" ]] && continue
    ARN="$(echo "$row" | jq -r '.ResourceARN')"
    EXPIRY="$(echo "$row" | jq -r '(.Tags[] | select(.Key=="expiry") | .Value) // ""')"
    PROTECT="$(echo "$row" | jq -r '(.Tags[] | select(.Key=="protect") | .Value) // ""')"

    if is_durable "$ARN"; then
      echo "  [DURABLE-BUG] $ARN"
      echo "      transient=true on a durable resource — REFUSING (fix the tag)."
      continue
    fi
    if [[ "${PROTECT,,}" == "true" ]]; then
      echo "  [PROTECTED]   $ARN  (protect=true — absolute exemption)"
      continue
    fi
    if [[ -z "$EXPIRY" ]]; then
      echo "  [NO-EXPIRY]   $ARN  (transient=true but no expiry tag — leaving)"
      continue
    fi
    EXP_EPOCH="$(to_epoch "$EXPIRY")"
    if [[ -z "$EXP_EPOCH" ]]; then
      echo "  [BAD-EXPIRY]  $ARN  (expiry='$EXPIRY' not parseable — leaving)"
      continue
    fi
    if (( EXP_EPOCH > NOW_EPOCH )); then
      echo "  [NOT-EXPIRED] $ARN  (expiry=$EXPIRY in the future — leaving)"
      continue
    fi

    echo "  [REAP]        $ARN  (expired at $EXPIRY)"
    REAP_ARNS+=("${REGION}|${ARN}")
  done < <(echo "$RESOURCES_JSON" | jq -c '.ResourceTagMappingList[]')
  echo
done

echo "=================================================================="
echo "Reap candidates (past-expiry, transient=true, not protected): ${#REAP_ARNS[@]}"
echo "=================================================================="

if (( CONFIRM == 0 )); then
  echo "DRY RUN — nothing deleted. Re-run with --confirm to delete the above."
  exit 0
fi

if (( ${#REAP_ARNS[@]} == 0 )); then
  echo "Nothing to delete."
  exit 0
fi

read -r -p "Delete the ${#REAP_ARNS[@]} resource(s) above? type 'reap' to proceed: " ANSWER
[[ "$ANSWER" == "reap" ]] || { echo "Aborted."; exit 0; }

delete_arn() {
  local region="$1" arn="$2"
  local service resource
  service="$(echo "$arn" | cut -d: -f3)"
  resource="$(echo "$arn" | cut -d: -f6-)"

  case "$service" in
    lambda)
      # function:name  or  function:name:alias
      IFS=':' read -r _ fn qual <<<"$resource"
      if [[ -n "${qual:-}" ]]; then
        aws lambda delete-alias --region "$region" --function-name "$fn" --name "$qual"
        echo "      deleted lambda alias $fn:$qual"
      else
        aws lambda delete-function --region "$region" --function-name "$fn"
        echo "      deleted lambda function $fn"
      fi ;;
    dynamodb)
      local table="${resource#table/}"
      aws dynamodb delete-table --region "$region" --table-name "$table" >/dev/null
      echo "      deleted dynamodb table $table" ;;
    apigateway)
      # /apis/<id>  or  /apis/<id>/stages/<name>
      if [[ "$resource" == *"/stages/"* ]]; then
        local api_id stage
        api_id="$(echo "$resource" | sed -E 's#.*/apis/([^/]+)/stages/.*#\1#')"
        stage="$(echo "$resource" | sed -E 's#.*/stages/([^/]+).*#\1#')"
        aws apigatewayv2 delete-stage --region "$region" --api-id "$api_id" --stage-name "$stage"
        echo "      deleted apigw stage $api_id/$stage"
      else
        local api_id2
        api_id2="$(echo "$resource" | sed -E 's#.*/apis/([^/]+).*#\1#')"
        aws apigatewayv2 delete-api --region "$region" --api-id "$api_id2"
        echo "      deleted apigw api $api_id2"
      fi ;;
    s3)
      # bucket/prefix — delete objects under the prefix only, never the bucket.
      local bucket="${resource%%/*}" prefix="${resource#*/}"
      if [[ "$resource" != */* ]]; then
        echo "      REFUSED bucket-level s3 delete for $resource"
      else
        aws s3 rm "s3://${bucket}/${prefix}" --recursive
        echo "      deleted s3 objects under $bucket/$prefix"
      fi ;;
    *)
      echo "      UNHANDLED service '$service' for $arn — skipped" ;;
  esac
}

for entry in "${REAP_ARNS[@]}"; do
  REGION="${entry%%|*}"
  ARN="${entry#*|}"
  echo "  Deleting $ARN"
  delete_arn "$REGION" "$ARN" || echo "      ERROR deleting $ARN (continuing)"
done

echo "Sweep complete."
