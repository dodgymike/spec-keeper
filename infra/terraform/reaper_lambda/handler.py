"""Spec Server teardown reaper (INFRA-6).

Durable, EventBridge-Scheduler-driven Lambda that deletes TRANSIENT preview
environments once they expire. The transient class for this serverless service
is per-branch / per-PR preview stacks:

  * ephemeral Lambda functions / aliases
  * throwaway DynamoDB tables (or table-name prefixes)
  * scoped API Gateway (HTTP API) stages / apis
  * preview S3 object prefixes

There is NO GPU plane in this service.

Reap rule (ALL must hold):
  1. resource is tagged  transient = true
  2. resource has an  expiry  tag that is a UTC-ISO timestamp in the PAST
  3. resource is NOT tagged  protect = true

Anything tagged `protect=true` is an ABSOLUTE exemption and is surfaced in the
report so it can never hide. A resource with `transient=true` but no parseable
`expiry`, or one whose expiry is still in the future, is left alone (a preview
table that still has data within its TTL must survive until expiry passes).

TEARDOWN SAFETY (do no harm) — defense in depth:
  * PRIMARY guard is IAM (see reaper.tf): the role can only delete resources
    carrying aws:ResourceTag/transient=true, and an explicit Deny on the durable
    ARNs (data table, state bucket, cognito pool, app lambda/api, UI bucket)
    means even a MIS-TAGGED durable resource cannot be deleted — an explicit
    Deny always beats any Allow.
  * SECONDARY guard is this code: a name/ARN denylist. A `transient=true` tag on
    a durable resource is a BUG; the reaper REFUSES to act on it and surfaces it
    loudly (report + SNS) instead of deleting.

The reaper, its role, its schedule, its log group and its SNS topic are DURABLE
(transient=false) and are additionally self-denylisted so the reaper can never
reap itself.

Env vars (set by reaper.tf):
  DRY_RUN            "true"/"false" — when true, list only, delete nothing.
  SNS_TOPIC_ARN      topic to publish the run summary to.
  REGIONS            comma-separated regions to sweep (default: AWS_REGION).
  DURABLE_DENY_NAMES comma-separated substrings that mark a resource durable.
  NAME_PREFIX        the durable stack name prefix (e.g. spec-server-dev).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DRY_RUN = _env_bool("DRY_RUN", default=True)  # fail SAFE: default to list-only
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
NAME_PREFIX = os.environ.get("NAME_PREFIX", "spec-server-dev")

_DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
REGIONS = [
    r.strip()
    for r in os.environ.get("REGIONS", _DEFAULT_REGION).split(",")
    if r.strip()
]

# Substrings that positively identify a DURABLE resource. If any appears in a
# resource's ARN or name, the reaper refuses to delete it regardless of tags.
# Keep this in lock-step with the explicit IAM Deny in reaper.tf.
_DURABLE_DEFAULTS = ",".join(
    [
        f"{NAME_PREFIX}-app",  # DynamoDB data table
        f"{NAME_PREFIX}-api",  # app Lambda + its log group
        f"{NAME_PREFIX}-ui",  # UI S3 bucket
        f"{NAME_PREFIX}-reaper",  # THIS reaper (self-protection)
        f"{NAME_PREFIX}-cost-alerts",  # cost SNS topic
        "spec-server-tfstate",  # remote state bucket (bootstrapped out-of-band)
        "spec-server-tflock",  # state lock table
        "userpool",  # cognito user pool ARN fragment
        "cognito",
    ]
)
DURABLE_DENY_NAMES = [
    s.strip().lower()
    for s in os.environ.get("DURABLE_DENY_NAMES", _DURABLE_DEFAULTS).split(",")
    if s.strip()
]

TRANSIENT_TAG = "transient"
EXPIRY_TAG = "expiry"
PROTECT_TAG = "protect"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_expiry(raw: str) -> dt.datetime | None:
    """Parse a UTC-ISO timestamp tag. Returns None if unparseable."""
    if not raw:
        return None
    text = raw.strip()
    # Accept a trailing Z (Zulu) which fromisoformat historically rejected.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    # Treat a naive timestamp as UTC (the TTL convention is UTC-ISO).
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _is_durable(arn: str, tags: dict[str, str]) -> bool:
    """Refuse anything whose ARN/name matches the durable denylist."""
    hay = arn.lower()
    name = (tags.get("Name") or tags.get("name") or "").lower()
    for needle in DURABLE_DENY_NAMES:
        if needle and (needle in hay or needle in name):
            return True
    return False


def _parse_arn(arn: str) -> dict[str, str]:
    # arn:partition:service:region:account:resourcetype/resource[:qualifier]
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return {}
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


# --------------------------------------------------------------------------- #
# Discovery — Resource Groups Tagging API finds every transient=true resource
# across supported services in one place, so we do not hand-roll a per-service
# List+ListTags loop for discovery.
# --------------------------------------------------------------------------- #
def _discover(region: str) -> list[dict]:
    client = boto3.client("resourcegroupstaggingapi", region_name=region)
    found: list[dict] = []
    paginator = client.get_paginator("get_resources")
    for page in paginator.paginate(
        TagFilters=[{"Key": TRANSIENT_TAG, "Values": ["true"]}],
        ResourcesPerPage=100,
    ):
        for item in page.get("ResourceTagMappingList", []):
            arn = item["ResourceARN"]
            tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
            found.append({"arn": arn, "tags": tags, "region": region})
    return found


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _classify(resource: dict) -> tuple[str, str]:
    """Return (decision, reason).

    decision in {"reap", "protected", "durable-bug", "not-expired",
                 "no-expiry", "bad-expiry"}.
    """
    arn = resource["arn"]
    tags = resource["tags"]

    # Durable guard first — a durable resource tagged transient=true is a BUG.
    if _is_durable(arn, tags):
        return "durable-bug", (
            "matches durable denylist; transient=true on a durable resource is a "
            "bug — refusing to reap (surface, do not act)"
        )

    if str(tags.get(PROTECT_TAG, "")).strip().lower() == "true":
        return "protected", "protect=true is an absolute exemption"

    raw_expiry = tags.get(EXPIRY_TAG, "")
    if not raw_expiry:
        return "no-expiry", "transient=true but no expiry tag — leaving alone"

    expiry = _parse_expiry(raw_expiry)
    if expiry is None:
        return "bad-expiry", f"expiry tag {raw_expiry!r} is not parseable UTC-ISO"

    if expiry > _now_utc():
        return "not-expired", f"expiry {expiry.isoformat()} is in the future"

    return "reap", f"expired at {expiry.isoformat()} (before {_now_utc().isoformat()})"


# --------------------------------------------------------------------------- #
# Deletion dispatch (only reached for decision == "reap" and DRY_RUN false)
# --------------------------------------------------------------------------- #
def _delete(resource: dict) -> str:
    arn = resource["arn"]
    region = resource["region"]
    meta = _parse_arn(arn)
    service = meta.get("service", "")
    res = meta.get("resource", "")

    if service == "lambda":
        # function:<name>  or  function:<name>:<alias/version>
        client = boto3.client("lambda", region_name=region)
        segs = res.split(":")
        # res like "function:name" or "function:name:alias"
        if len(segs) >= 3 and segs[0] == "function":
            fn, qualifier = segs[1], segs[2]
            client.delete_alias(FunctionName=fn, Name=qualifier)
            return f"deleted lambda alias {fn}:{qualifier}"
        fn = segs[1] if len(segs) >= 2 else res
        client.delete_function(FunctionName=fn)
        return f"deleted lambda function {fn}"

    if service == "dynamodb":
        # table/<name>
        client = boto3.client("dynamodb", region_name=region)
        table = res.split("/", 1)[1] if "/" in res else res
        client.delete_table(TableName=table)
        return f"deleted dynamodb table {table}"

    if service in ("apigateway", "execute-api"):
        # apigatewayv2 HTTP API: arn .../apis/<apiId>  or  .../apis/<apiId>/stages/<name>
        client = boto3.client("apigatewayv2", region_name=region)
        segs = res.strip("/").split("/")
        if "stages" in segs:
            i = segs.index("stages")
            api_id, stage = segs[i - 1], segs[i + 1]
            client.delete_stage(ApiId=api_id, StageName=stage)
            return f"deleted apigw stage {api_id}/{stage}"
        if "apis" in segs:
            i = segs.index("apis")
            api_id = segs[i + 1]
            client.delete_api(ApiId=api_id)
            return f"deleted apigw api {api_id}"
        return f"UNHANDLED apigateway resource {res}"

    if service == "s3":
        # A tagged preview PREFIX is modelled as an object whose ARN is
        # arn:aws:s3:::<bucket>/<prefix>. Delete only under that prefix; never
        # the bucket itself (durable buckets are denylisted + IAM-denied).
        client = boto3.client("s3", region_name=region)
        if "/" not in res:
            return f"REFUSED s3 bucket-level delete for {res} (prefix required)"
        bucket, prefix = res.split("/", 1)
        deleted = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                deleted += len(objs)
        return f"deleted {deleted} s3 objects under {bucket}/{prefix}"

    return f"UNHANDLED service {service} for {arn}"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _publish(summary: dict) -> None:
    logger.info("REAPER SUMMARY %s", json.dumps(summary, default=str))
    if not SNS_TOPIC_ARN:
        return
    counts = summary["counts"]
    subject = (
        f"[spec-server reaper] "
        f"{'DRY-RUN ' if summary['dry_run'] else ''}"
        f"reaped={counts.get('reaped', 0)} "
        f"would_reap={counts.get('reap', 0)} "
        f"protected={counts.get('protected', 0)} "
        f"durable-bugs={counts.get('durable-bug', 0)}"
    )
    try:
        boto3.client("sns", region_name=_DEFAULT_REGION).publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],
            Message=json.dumps(summary, indent=2, default=str),
        )
    except Exception as exc:  # noqa: BLE001 — never let notify failure mask a run
        logger.error("SNS publish failed: %s", exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def handler(event, context):  # noqa: ARG001 — Lambda signature
    dry_run = DRY_RUN
    # Allow a one-off manual invoke to force real deletion or force dry-run.
    if isinstance(event, dict) and "dry_run" in event:
        dry_run = bool(event["dry_run"])

    actions: list[dict] = []
    counts: dict[str, int] = {}

    for region in REGIONS:
        try:
            resources = _discover(region)
        except Exception as exc:  # noqa: BLE001
            logger.error("discovery failed in %s: %s", region, exc)
            actions.append({"region": region, "error": str(exc)})
            continue

        for resource in resources:
            decision, reason = _classify(resource)
            counts[decision] = counts.get(decision, 0) + 1
            entry = {
                "arn": resource["arn"],
                "region": region,
                "decision": decision,
                "reason": reason,
                "expiry": resource["tags"].get(EXPIRY_TAG),
            }

            if decision == "durable-bug":
                # Loud: this is a tagging bug that must be fixed by a human.
                logger.error("DURABLE-BUG %s :: %s", resource["arn"], reason)
            elif decision == "protected":
                logger.warning("PROTECTED %s :: %s", resource["arn"], reason)

            if decision == "reap":
                if dry_run:
                    entry["action"] = "would-delete (dry-run)"
                    logger.info("WOULD REAP %s :: %s", resource["arn"], reason)
                else:
                    try:
                        result = _delete(resource)
                        entry["action"] = result
                        counts["reaped"] = counts.get("reaped", 0) + 1
                        logger.info("REAPED %s :: %s :: %s",
                                    resource["arn"], reason, result)
                    except Exception as exc:  # noqa: BLE001
                        entry["action"] = f"ERROR: {exc}"
                        counts["reap-errors"] = counts.get("reap-errors", 0) + 1
                        logger.error("REAP FAILED %s :: %s",
                                     resource["arn"], exc)

            actions.append(entry)

    summary = {
        "dry_run": dry_run,
        "ran_at": _now_utc().isoformat(),
        "regions": REGIONS,
        "counts": counts,
        "actions": actions,
    }
    _publish(summary)
    return summary
