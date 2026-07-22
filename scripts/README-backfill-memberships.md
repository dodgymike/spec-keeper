# Membership backfill runbook (ISO-5)

`scripts/backfill_memberships.py` seeds every existing agent as a member of every
existing project, so that flipping `PROJECT_ISOLATION_ENFORCED` ON (ISO-6) does not
lock any non-admin agent out of a backlog it already uses.

## Prerequisites
- An admin identity in the `agent-credentials` secret: export
  `AGENT_USERNAME=spec-keeper` (or `aws-infra`) and
  `AGENT_CREDENTIALS_SECRET_ARN` (or `AGENT_CREDENTIALS_SECRET`) =
  `spec-server-dev/agent-credentials`.
- AWS creds able to `cognito-idp:ListUsersInGroup` on pool `eu-west-1_S1fUqxuKv`.
- **The ISO members endpoint must be DEPLOYED** before `--apply` (it 404s until the
  ISO deploy wave). `--dry-run` works any time (it never calls that endpoint).

## Procedure (do these in order)
1. **Preview** (safe, writes nothing, default):
   ```
   AGENT_USERNAME=spec-keeper AGENT_CREDENTIALS_SECRET=spec-server-dev/agent-credentials \
     python scripts/backfill_memberships.py --dry-run
   ```
   Eyeball the resolved agents (sub + role) and the planned `POST`s.
2. **Wait for the ISO deploy wave** (ISO-1..4 members endpoint live).
3. **Apply** — AFTER the deploy, BEFORE flipping `PROJECT_ISOLATION_ENFORCED`:
   ```
   AGENT_USERNAME=spec-keeper AGENT_CREDENTIALS_SECRET=spec-server-dev/agent-credentials \
     python scripts/backfill_memberships.py --apply
   ```
   Idempotent — safe to re-run (server upserts).
4. **Then** flip `PROJECT_ISOLATION_ENFORCED` ON (ISO-6).

## Reverse / scope
- Undo: `--revoke --apply` DELETEs each membership the run would have created
  (idempotent). Preview a revoke with `--revoke` alone.
- Scope to one project: `--project <slug>` (repeatable).

The admin bearer token lives only in memory (minted by `agent_token.py`) and is
never printed or logged; `principal_sub` is treated as an opaque identity.
