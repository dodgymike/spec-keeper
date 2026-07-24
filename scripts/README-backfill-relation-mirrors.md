# RELIN relation-mirror backfill runbook (SLS-J2 follow-up)

`scripts/backfill_relation_mirrors.py` creates the missing **incoming-edge mirror**
items for DynamoDB relations that were created *before* SLS-J2.

## Background
SLS-J2 made relations bidirectional: `add_relation` now writes, in one
`TransactWriteItems`, a forward item `SK=TASK#<src>#REL#<kind>#<dst>`
(`type=relation`) **and** a mirror `SK=TASK#<dst>#RELIN#<kind>#<src>`
(`type=relation_in`). `list_relations` reads incoming edges with a
`begins_with(TASK#<dst>#RELIN#)` range query. Relations created *before* J2 have
only the forward item, so their **incoming** edges are invisible. This one-shot
backfill creates the missing `RELIN` mirrors.

This is a **DynamoDB-only** migration. The Postgres adapter stores each relation
as a single row queried from both ends, so there is no forward/mirror split and
nothing to backfill — there is no parity gap to close.

## Safety properties
- **Dry-run is the default** and is **read-only**: it scans forward relations and
  probes each mirror, reporting `scanned` / `would-create` / `skipped-existing`,
  and writes nothing.
- `--apply` uses **idempotent conditional puts**
  (`attribute_not_exists(PK) AND attribute_not_exists(SK)`), so a second run is a
  no-op and an existing mirror is never overwritten.
- The mirror item is byte-identical to what `add_relation` writes today (same
  `type=relation_in`, `kind`, `src`, `dst`, and the forward item's `created_at`);
  the mirror SK is derived with the runtime encoder `app.storage.keys.relation_in_sk`.
- The `type` filter value binds via `ExpressionAttributeValues` (never
  string-formatted into the expression). No credentials live in the script; boto3
  resolves them from the environment/role.

## Config (env, same knobs as the app storage layer)
- `DYNAMODB_TABLE` — the single table (default `spec-server`; **never** hardcode
  the prod name in the script).
- `AWS_REGION` — default `us-east-1`; use `eu-west-1` for prod.
- `DYNAMODB_ENDPOINT_URL` — optional; point at DynamoDB Local for testing.

## Procedure
1. **Preview** (safe, writes nothing, default):
   ```
   DYNAMODB_TABLE=<prod-table> AWS_REGION=eu-west-1 \
     python scripts/backfill_relation_mirrors.py --dry-run
   ```
   Eyeball `scanned` and `would-create`.
2. **Apply** (a deploy agent runs this after review):
   ```
   DYNAMODB_TABLE=<prod-table> AWS_REGION=eu-west-1 \
     python scripts/backfill_relation_mirrors.py --apply
   ```
   Idempotent — safe to re-run; `skipped-existing` should equal the total on a
   second run.

## Tests
`tests/test_backfill_relation_mirrors.py` (DynamoDB Local, self-skips on Postgres)
proves: a forward-only relation gets a byte-identical mirror on `--apply` and then
appears as an incoming edge in `list_relations`; re-running is idempotent; dry-run
writes nothing; an already-mirrored relation is left untouched.
