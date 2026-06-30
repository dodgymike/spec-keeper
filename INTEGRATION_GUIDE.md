# Spec Server — Integration & Migration Guide (for agents)

This guide tells an AI coding agent how to migrate a repo's `SPEC.md` workflow onto the
**Spec Server** and then drive day-to-day work through its API instead of hand-editing
`SPEC.md`. It is safe, incremental, and reversible — your `SPEC.md` is never destroyed.

> Authoritative machine-readable contract: `GET http://localhost:8080/openapi.json`
> (Swagger UI at `/docs`). Endpoint recipe book: `spec-server/AGENTS_API.md`.

---

## Why migrate

The flat-`SPEC.md` workflow has two races the server eliminates:

1. **Two agents pick the same "next unchecked task."** → `POST .../tasks/claim-next` hands each
   caller a *distinct* task (`FOR UPDATE SKIP LOCKED`) or 204 when empty.
2. **Two agents grab the same migration/table number.** → `POST .../reservations` allocates a
   unique, monotonic number atomically (`ON CONFLICT DO UPDATE RETURNING`).

Plus: optimistic locking (`version`/`If-Match` → 412) so concurrent edits never silently clobber,
an append-only event log + decision records, and an `owner` field so **each agent keeps its specs
separate** (`GET .../tasks?owner=<me>`).

---

## 0. Prerequisites (once per machine)

Start the server (it runs locally in Docker):

```bash
cd /Users/mrdavis/source/spec-server
docker compose up -d --build
curl -sf http://localhost:8080/readyz     # -> {"status":"ready"}
```

- API base URL: `http://localhost:8080/api/v1`
- Auth: none by default (local-only). If the server was started with `API_KEYS`, send
  `Authorization: Bearer <key>` on every request (and `export APIKEY=<key>` for the scripts).

---

## 1. Migrate this repo (one-time, safe, idempotent)

From the spec-server repo, run the generic migration script with your project **slug** and the path
to your `SPEC.md`:

```bash
cd /Users/mrdavis/source/spec-server
scripts/migrate-repo.sh <slug> /path/to/your/SPEC.md "Human Name"
```

It will, idempotently and **without modifying your `SPEC.md`**:
1. create the project (tolerates "already exists"),
2. register the agent roster,
3. import `SPEC.md` (upsert by task key — re-running creates no duplicates),
4. **verify** by printing an `export/diff` (server view vs your file),
5. print the task/todo counts.

**Project slugs (use these):**

| Repo | slug | SPEC.md |
|---|---|---|
| journalizer | `journalizer` | (none yet — starts empty) |
| bird-song-visualisation | `bird-song` | `bird-song-visualisation/SPEC.md` |
| corsearch | `corsearch` | `corsearch/SPEC.md` |
| corsearch/zeal-local-pack | `zeal-local-pack` | (none yet) |
| feed-reader/rss-collector | `rss-collector` | `feed-reader/rss-collector/SPEC.md` |

If your repo has no `SPEC.md` yet, the script just creates an empty project — start adding tasks
with `POST .../tasks`.

### Verify before you trust it

The script already prints a diff. You can re-check any time:

```bash
B=http://localhost:8080/api/v1
curl -s -X POST $B/projects/<slug>/export/diff \
  --data-binary @SPEC.md -H 'Content-Type: text/markdown'
```

`0 new / 0 only-in-server / 0 changed` means the server is a faithful copy. A few "changed" entries
are normal for very rich, multi-line task descriptions — the round-trip preserves the **normalized**
fields (id, title, status, priority, component, epic, proof), not byte-for-byte prose.

---

## 2. Start using the server (the new atomic loop)

Replace these three file operations with API calls; everything else in your `CLAUDE.md` stays:

| Old (SPEC.md) | New (Spec Server) |
|---|---|
| Scan the file, pick an unchecked `[ ]` task | `POST .../tasks/claim-next {"agent":"<me>"}` |
| Flip the checkbox to `[x]` | `POST .../tasks/<id>/complete {...}` |
| Write a `MIGRATION 0NN reserved` stub | `POST .../reservations {"namespace":"migration"}` |
| Add a discovered follow-up line | `POST .../tasks {...}` |
| Append to `AGENT_LOG.md` | `POST .../events {...}` (claim/complete/reserve auto-log) |
| Add a `DECISIONS.md` entry | `POST .../decisions {...}` |

Concretely, per atomic increment:

```bash
B=http://localhost:8080/api/v1; SLUG=<slug>; ME=<your-agent-slug>

# 1) Claim exactly one task (collision-proof). 204 => backlog empty.
task=$(curl -s -X POST $B/projects/$SLUG/tasks/claim-next \
  -H 'Content-Type: application/json' -d "{\"agent\":\"$ME\"}")
key=$(echo "$task" | python3 -c "import sys,json;print(json.load(sys.stdin)['key'])")

# 2) ... do the work: smallest change, narrowest test, commit ...

# 3) Need a migration/table/queue number? Reserve it (never choose by hand):
curl -s -X POST $B/projects/$SLUG/reservations \
  -H 'Content-Type: application/json' -d '{"namespace":"migration","reserved_by":"'$ME'"}'

# 4) Complete the task (the "flip the checkbox" step):
curl -s -X POST $B/projects/$SLUG/tasks/$key/complete \
  -H 'Content-Type: application/json' \
  -d '{"commit_sha":"<sha>","test_summary":"<n/n pass>","proof_cmd":"<cmd>"}'
```

Other useful calls (full set in `AGENTS_API.md`):
- "My specs": `GET .../tasks?owner=<me>`
- Block/defer: `POST .../tasks/<id>/status {"status":"blocked","note":"..."}`
- Give a task back: `POST .../tasks/<id>/release`
- Track the mandated chain: `POST .../tasks/<id>/chain-runs`, then
  `PUT .../chain-runs/<id>/steps/<step>` (a skipped step needs a justification → else 422)
- Safe retry after a network blip: resend `claim-next`/`reservations` with the same
  `Idempotency-Key: <token>` header — it replays the original result.

### Optimistic locking (avoid lost updates)

`GET .../tasks/<id>` returns `ETag: "v<n>"`. When you read-then-write, send `If-Match: "v<n>"` on the
`PATCH`/`complete`; a concurrent change yields **412** — re-read and retry.

---

## 3. Keep `SPEC.md` as a mirror (during and after transition)

You don't have to delete `SPEC.md`. Regenerate it from the server whenever you want a readable,
committable snapshot:

```bash
curl -s http://localhost:8080/api/v1/projects/<slug>/export > SPEC.md
```

A good transition is: **server is the source of truth for task state; `SPEC.md` is a generated
mirror** you refresh (and commit) at the end of a work session. Once your team trusts the server,
you can stop hand-editing `SPEC.md` entirely.

---

## 4. Parallel-agent safety (now enforced by the server)

The old "append-only shared file, one writer at a time" convention is replaced by DB guarantees:
- **Claim** work — two agents never get the same task.
- **Reserve** shared identifiers — two agents never get the same number.
- **Owner + lease** — your in-flight tasks are yours; an abandoned task (expired lease) is
  automatically reclaimable by the next `claim-next`.
- **Events/decisions** are append-only and concurrent-safe — no file-lock dance.

---

## 5. Safety & rollback

This migration is **additive and reversible**:
- `scripts/migrate-repo.sh` never writes or deletes your `SPEC.md`; the server holds a *copy*.
- Import is idempotent — re-run it any time; tasks upsert by key, no duplicates.
- If anything looks wrong, just keep using `SPEC.md` as before — nothing is lost. You can also
  `DELETE .../projects/<slug>` to remove the server-side copy and start the import over.
- The server is local-only (Docker + Postgres on your machine); no data leaves the box.

---

## 6. Quick reference

- Endpoint recipes & examples: `spec-server/AGENTS_API.md`
- Machine-readable spec: `http://localhost:8080/openapi.json` · Swagger UI: `/docs`
- Generic migration: `spec-server/scripts/migrate-repo.sh <slug> <SPEC.md> [name]`
- Reference migration (the server's own): `spec-server/scripts/dogfood.sh`
