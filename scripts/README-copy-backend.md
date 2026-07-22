# copy_backend.py — Postgres ⇄ DynamoDB backlog copy/sync (SLS-10)

A one-off operator tool to move a project's backlog between the two Spec Server
storage backends so a repo can flip `STORAGE_BACKEND` without losing data. It is
**not** part of the app runtime.

## How it works

It instantiates **both** backends behind the same `app.storage.StorageBackend`
interface and copies entities through the neutral DTOs (`app/storage/dto.py`) —
so reading the backlog needs zero knowledge of either physical schema. Two things
the neutral interface can't express use a small backend-specific path:

- **Numbered resources** — counters are *set* to the source's high-water value
  (never lowered) so the destination's next `reserve_number` continues
  monotonically; reservation audit rows are inserted with their original values.
- **Relations & chain-runs** — enumerated per-backend (there is no
  `list_relations` / `list_chain_runs`), then written back through the neutral
  API (`add_relation` / `create_chain_run` + `upsert_chain_step`).

## Usage

```
python scripts/copy_backend.py --source {postgres|dynamodb} \
                               --dest   {postgres|dynamodb} \
                               (--project <slug> | --all) [--dry-run] [-v]
```

`--dry-run` reads the source only and prints per-entity counts of what *would*
be copied — it never touches the destination.

### Examples

Postgres → DynamoDB (preview, then real), one project:

```bash
export SRC_DATABASE_URL=postgresql+psycopg://spec:spec@localhost:5432/specserver
export DEST_DYNAMODB_TABLE=spec-server
export DEST_DYNAMODB_ENDPOINT_URL=http://localhost:8000     # DynamoDB Local
export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_REGION=us-east-1
python scripts/copy_backend.py --source postgres --dest dynamodb --project spec-server --dry-run
python scripts/copy_backend.py --source postgres --dest dynamodb --project spec-server
```

DynamoDB → Postgres, all projects:

```bash
export SRC_DYNAMODB_TABLE=spec-server SRC_DYNAMODB_ENDPOINT_URL=http://localhost:8000
export DEST_DATABASE_URL=postgresql+psycopg://spec:spec@localhost:5432/specserver
python scripts/copy_backend.py --source dynamodb --dest postgres --all
```

## Environment

| Variable | Side | Notes |
|---|---|---|
| `SRC_DATABASE_URL` / `DEST_DATABASE_URL` | postgres | SQLAlchemy URL |
| `SRC_DYNAMODB_TABLE` / `DEST_DYNAMODB_TABLE` | dynamodb | default `spec-server` |
| `SRC_DYNAMODB_ENDPOINT_URL` / `DEST_DYNAMODB_ENDPOINT_URL` | dynamodb | set for DynamoDB Local; unset for real AWS |
| `SRC_AWS_REGION` / `DEST_AWS_REGION` | dynamodb | default `AWS_REGION` / `us-east-1` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_PROFILE` | dynamodb | ambient boto3 chain, shared by both dynamo sides |

## Guarantees & limits

- **Idempotent / re-runnable**: keyed entities (projects, epics, tasks, agents,
  reservations by value, commits by sha, counters by value) are skipped or
  max-merged if already present — never duplicated. Nothing is ever DELETEd from
  either side.
- **Not re-run safe** (no natural key → appended again on a second run):
  epic/task notes, events, decisions, and tasks that have no human key. Run once,
  or run into a fresh destination.
- **Not perfectly preserved** (regenerated/dropped by the destination):
  `public_id` (new UUID), `version` (reset to 1), all timestamps (copy-time),
  `owner` / `lease_expires_at` (dropped — run against a quiesced source), internal
  DB integer ids, and the reservation→task linkage.
- **At most one side may be postgres** — the Postgres adapter uses one global
  Flask-SQLAlchemy session per process. A postgres→postgres copy is refused; use
  `pg_dump` / `pg_restore` for that.

See the module docstring in `copy_backend.py` for the full preservation table.
