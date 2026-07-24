#!/usr/bin/env python3
"""Idempotent, dry-run-first backfill of RELIN relation mirrors (post-SLS-J2).

Why
---
SLS-J2 made DynamoDB relations bidirectional: ``add_relation`` now writes a
forward edge ``SK=TASK#<src>#REL#<kind>#<dst>`` (``type=relation``) AND a MIRROR
edge ``SK=TASK#<dst>#RELIN#<kind>#<src>`` (``type=relation_in``) in the same
``TransactWriteItems`` so an incoming-edge lookup is a ``begins_with(TASK#<dst>#
RELIN#)`` range read. Relations created BEFORE J2 only have the forward item, so
``list_relations`` / the relations-GET endpoint miss their INCOMING edges. This
one-shot script creates the missing ``RELIN`` mirror for every pre-existing
forward relation.

What it does
------------
1. **Enumerate** existing forward relation items with a single paginated
   ``Scan`` filtered to ``type = "relation"`` (the value is bound via
   ``ExpressionAttributeValues`` by ``boto3.dynamodb.conditions.Attr`` — never
   string-formatted into the expression). ``LastEvaluatedKey`` is followed to the
   end so nothing is missed.
2. For each forward item it derives the mirror key via the SAME
   ``app.storage.keys.relation_in_sk`` the runtime uses (imported, never
   duplicated) and builds a mirror item byte-identical to what ``add_relation``
   writes today (``type=relation_in``, same ``kind``/``src``/``dst`` and the
   forward item's ``created_at`` carried across).
3. It writes each mirror with a **conditional put**
   (``attribute_not_exists(PK) AND attribute_not_exists(SK)``) so re-runs never
   duplicate and never overwrite an existing mirror — fully idempotent.

Modes
-----
* ``--dry-run`` (**DEFAULT**) — read-only. Scans, and for every forward relation
  probes whether its mirror already exists, printing how many mirrors *would* be
  created vs are already present. Writes NOTHING.
* ``--apply`` — performs the idempotent conditional writes.

Config (read from the environment, exactly like the app's storage layer — never
hardcode the prod table):
* ``DYNAMODB_TABLE``        (default ``spec-server``) — the single table.
* ``AWS_REGION``           (default ``us-east-1``).
* ``DYNAMODB_ENDPOINT_URL`` (optional) — point at DynamoDB Local for testing.
No credentials live in this script; boto3 resolves them from the environment/role.

Safe to run against prod: dry-run is the default and is read-only; ``--apply``
uses idempotent conditional writes, so a second run is a no-op.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

# Import the runtime key encoders so the mirror SK is byte-identical to what
# add_relation writes — never re-implement the format string here.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.storage import keys as K  # noqa: E402

FORWARD_TYPE = "relation"
MIRROR_TYPE = "relation_in"


# --------------------------------------------------------------------------- #
# Pure logic (unit-testable; no I/O)
# --------------------------------------------------------------------------- #
def mirror_item_from_forward(forward: dict) -> dict:
    """Build the RELIN mirror item for a forward ``type=relation`` item.

    Mirrors ``DynamoBackend.add_relation``'s ``rel_in`` shape exactly: same
    partition (``PK``), ``type=relation_in``, the same ``kind``/``src``/``dst``,
    and the forward item's ``created_at`` carried across. The SK is derived with
    the runtime encoder ``keys.relation_in_sk(dst, kind, src)``.
    """
    src = forward["src"]
    dst = forward["dst"]
    kind = forward["kind"]
    return {
        "PK": forward["PK"],
        "SK": K.relation_in_sk(dst, kind, src),
        "type": MIRROR_TYPE,
        "kind": kind,
        "src": src,
        "dst": dst,
        "created_at": forward.get("created_at"),
    }


class Summary:
    """Running tallies for the backfill run."""

    __slots__ = ("scanned", "created", "skipped_existing")

    def __init__(self) -> None:
        self.scanned = 0
        self.created = 0
        self.skipped_existing = 0

    def as_line(self, applied: bool) -> str:
        verb = "created" if applied else "would-create"
        return (f"scanned={self.scanned}  {verb}={self.created}  "
                f"skipped-existing={self.skipped_existing}")


# --------------------------------------------------------------------------- #
# Live I/O (DynamoDB)
# --------------------------------------------------------------------------- #
def make_table(table_name=None, region=None, endpoint_url=None):
    """Build the boto3 Table handle from args/env (same knobs as the app)."""
    table_name = table_name or os.environ.get("DYNAMODB_TABLE", "spec-server")
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    endpoint_url = endpoint_url or os.environ.get("DYNAMODB_ENDPOINT_URL")
    kwargs = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    resource = boto3.resource("dynamodb", **kwargs)
    return resource.Table(table_name)


def scan_forward_relations(table):
    """Yield every forward ``type=relation`` item, following pagination fully.

    The ``type`` value is bound (via ``Attr(...).eq``) into
    ``ExpressionAttributeValues`` by boto3 — never interpolated into the filter
    expression string (the DynamoDB analogue of parameterised SQL)."""
    kwargs = {"FilterExpression": Attr("type").eq(FORWARD_TYPE)}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        last = resp.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def _mirror_exists(table, mirror: dict) -> bool:
    got = table.get_item(Key={"PK": mirror["PK"], "SK": mirror["SK"]})
    return "Item" in got


def backfill(table, *, apply: bool, log=print) -> Summary:
    """Enumerate forward relations and (in ``--apply``) create missing mirrors.

    Returns a :class:`Summary`. In dry-run mode it only *probes* each mirror
    (``get_item``) and writes nothing."""
    summary = Summary()
    for forward in scan_forward_relations(table):
        summary.scanned += 1
        mirror = mirror_item_from_forward(forward)
        if not apply:
            if _mirror_exists(table, mirror):
                summary.skipped_existing += 1
            else:
                summary.created += 1
                log(f"  would-create RELIN mirror {mirror['SK']} "
                    f"(in {mirror['PK']})")
            continue
        try:
            table.put_item(
                Item=mirror,
                ConditionExpression=(Attr("PK").not_exists()
                                     & Attr("SK").not_exists()),
            )
            summary.created += 1
            log(f"  created RELIN mirror {mirror['SK']} (in {mirror['PK']})")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                summary.skipped_existing += 1  # mirror already present -> no-op
            else:
                raise
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill missing RELIN relation mirrors (SLS-J2 follow-up).",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Preview only (DEFAULT when --apply is absent); writes nothing.")
    p.add_argument("--apply", action="store_true",
                   help="Actually create the missing mirrors (idempotent conditional puts).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    apply = args.apply and not args.dry_run
    mode = "APPLY" if apply else "DRY-RUN"
    table = make_table()
    print(f"== RELIN mirror backfill [{mode}] ==  table={table.name}")
    summary = backfill(table, apply=apply)
    print(f"\n{mode}: {summary.as_line(applied=apply)}")
    if not apply:
        print("DRY-RUN: nothing written. Re-run with --apply to create the "
              f"{summary.created} missing mirror(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
