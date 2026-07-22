"""Boto3 glue for the signup queue (HA-7), isolated so tests monkeypatch it.

One place resolves the signups DynamoDB table, the intake SQS queue, and the SES
sender — mirroring ``admin._invites_table`` / ``admin._cognito_client``. Every
function no-ops (returns ``None``/``False``) when its resource is unconfigured so
a local run without the infra is graceful (the intake still returns its uniform
202; validate returns the neutral "invalid"; approve degrades to no email).
"""
from __future__ import annotations

import json
import logging

_log = logging.getLogger(__name__)


def _ddb_kwargs(cfg) -> dict:
    kwargs = {}
    if cfg.get("AWS_REGION"):
        kwargs["region_name"] = cfg["AWS_REGION"]
    if cfg.get("DYNAMODB_ENDPOINT_URL"):
        kwargs["endpoint_url"] = cfg["DYNAMODB_ENDPOINT_URL"]
    return kwargs


def signups_table(cfg):
    """Return a boto3 DynamoDB Table for the signups store, or ``None`` if unset."""
    name = cfg.get("SIGNUPS_TABLE")
    if not name:
        return None
    import boto3  # lazy: keep boto3 off the import path when signups are unused

    return boto3.resource("dynamodb", **_ddb_kwargs(cfg)).Table(name)


def send_intake_message(cfg, message: dict) -> bool:
    """SendMessage the branch-free intake payload to the SQS intake queue.

    Returns ``False`` (silently, like the bird kill-switch) when
    ``SIGNUP_INTAKE_QUEUE_URL`` is unset so a local run without SQS still returns
    the uniform 202 — never an existence-dependent error."""
    queue_url = cfg.get("SIGNUP_INTAKE_QUEUE_URL")
    if not queue_url:
        return False
    import boto3

    kwargs = {}
    if cfg.get("AWS_REGION"):
        kwargs["region_name"] = cfg["AWS_REGION"]
    if cfg.get("SQS_ENDPOINT_URL"):
        kwargs["endpoint_url"] = cfg["SQS_ENDPOINT_URL"]
    client = boto3.client("sqs", **kwargs)
    client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
    return True


def ses_send(cfg, *, to_addr: str, subject: str, body: str) -> bool:
    """Send one transactional email via SESv2. Returns ``False`` (logs) when no
    ``SES_FROM_ADDRESS`` is configured so approve degrades gracefully in dev.

    Routes through the HA-6 configuration set (``SES_CONFIG_SET``) when set so
    bounce/complaint events are tracked; the param is OMITTED (never sent empty)
    when cleared, since SES rejects an empty ``ConfigurationSetName``."""
    sender = cfg.get("SES_FROM_ADDRESS")
    if not sender:
        _log.warning("signup: SES_FROM_ADDRESS unset; skipping email send")
        return False
    import boto3

    kwargs = {}
    if cfg.get("AWS_REGION"):
        kwargs["region_name"] = cfg["AWS_REGION"]
    client = boto3.client("sesv2", **kwargs)
    send_kwargs = {
        "FromEmailAddress": sender,
        "Destination": {"ToAddresses": [to_addr]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            }
        },
    }
    config_set = (cfg.get("SES_CONFIG_SET") or "").strip()
    if config_set:
        send_kwargs["ConfigurationSetName"] = config_set
    client.send_email(**send_kwargs)
    return True
