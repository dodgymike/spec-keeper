#!/usr/bin/env python3
"""Restore a Spec Server HTTP-API JSON export into a fresh Postgres database.

Runs INSIDE the app container (SQLAlchemy present, DATABASE_URL set):
    python restore_backup.py /tmp/backup [--force]

Faithfully reconstructs projects/epics/tasks/notes/reservations from the JSON
export, rebuilding the integer surrogate-PK relationships from the business
keys (epic.key, task.display_id). Events are audit-only and reference stale
integer task ids, so they are intentionally NOT restored.
"""
from __future__ import annotations

import json
import os
import sys

import sqlalchemy as sa

BACKUP = sys.argv[1] if len(sys.argv) > 1 else "/tmp/backup"
FORCE = "--force" in sys.argv[2:]
SLUG = "bird-song"


def load(name):
    with open(os.path.join(BACKUP, name)) as fh:
        d = json.load(fh)
    return d if isinstance(d, list) else d.get("items", d)


def main():
    url = os.environ["DATABASE_URL"]
    engine = sa.create_engine(url)

    project = load("project.json")
    epics = load("epics.json")
    tasks = load("tasks.json")
    notes = load("notes.json")
    reservations = load("reservations.json")

    with engine.begin() as cx:
        # --- project ---------------------------------------------------------
        pid = cx.execute(
            sa.text("SELECT id FROM projects WHERE slug=:s"), {"s": SLUG}
        ).scalar()
        if pid is None:
            pid = cx.execute(
                sa.text(
                    "INSERT INTO projects (public_id, slug, name, description, "
                    "default_branch, created_at, updated_at) VALUES "
                    "(:pub,:slug,:name,:desc,:br,:ca,:ua) RETURNING id"
                ),
                {
                    "pub": project["public_id"], "slug": SLUG,
                    "name": project.get("name") or "Bird Song Visualisation",
                    "desc": project.get("description"),
                    "br": project.get("default_branch") or "main",
                    "ca": project.get("created_at"), "ua": project.get("updated_at"),
                },
            ).scalar()
            print(f"[project] created id={pid}")
        else:
            existing = cx.execute(
                sa.text("SELECT count(*) FROM tasks WHERE project_id=:p"), {"p": pid}
            ).scalar()
            print(f"[project] exists id={pid} with {existing} task(s)")
            if existing and not FORCE:
                sys.exit(
                    "REFUSING to restore into a non-empty project. "
                    "Re-run with --force to wipe project rows first."
                )
            if existing and FORCE:
                # wipe child rows for a clean idempotent re-restore
                cx.execute(sa.text(
                    "DELETE FROM epic_notes WHERE epic_id IN "
                    "(SELECT id FROM epics WHERE project_id=:p)"), {"p": pid})
                cx.execute(sa.text(
                    "DELETE FROM task_notes WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE project_id=:p)"), {"p": pid})
                cx.execute(sa.text(
                    "DELETE FROM commit_refs WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE project_id=:p)"), {"p": pid})
                cx.execute(sa.text(
                    "DELETE FROM task_tags WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE project_id=:p)"), {"p": pid})
                cx.execute(sa.text("DELETE FROM leases WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE project_id=:p)"), {"p": pid})
                cx.execute(sa.text("DELETE FROM reservations WHERE project_id=:p"), {"p": pid})
                cx.execute(sa.text("DELETE FROM counters WHERE project_id=:p"), {"p": pid})
                cx.execute(sa.text("DELETE FROM tags WHERE project_id=:p"), {"p": pid})
                cx.execute(sa.text("DELETE FROM tasks WHERE project_id=:p"), {"p": pid})
                cx.execute(sa.text("DELETE FROM epics WHERE project_id=:p"), {"p": pid})
                print("[project] --force: wiped existing project rows")

        # --- epics -----------------------------------------------------------
        epic_id = {}
        for e in epics:
            eid = cx.execute(
                sa.text(
                    "INSERT INTO epics (public_id, project_id, key, title, "
                    "description, section, position, created_at, updated_at) "
                    "VALUES (:pub,:p,:k,:t,:d,:s,:pos, now(), now()) RETURNING id"
                ),
                {
                    "pub": e["public_id"], "p": pid, "k": e["key"],
                    "t": e["title"], "d": e.get("description"),
                    "s": e.get("section") or "backlog",
                    "pos": e.get("position", 1000.0),
                },
            ).scalar()
            epic_id[e["key"]] = eid
        print(f"[epics] inserted {len(epic_id)}")

        # --- tags (collected from tasks) ------------------------------------
        tag_id = {}
        for t in tasks:
            for tag in t.get("tags") or []:
                if tag not in tag_id:
                    tag_id[tag] = cx.execute(
                        sa.text(
                            "INSERT INTO tags (project_id, key) VALUES (:p,:k) "
                            "RETURNING id"
                        ),
                        {"p": pid, "k": tag},
                    ).scalar()
        if tag_id:
            print(f"[tags] inserted {len(tag_id)}")

        # --- tasks -----------------------------------------------------------
        task_id = {}
        n_commits = 0
        for t in tasks:
            tid = cx.execute(
                sa.text(
                    "INSERT INTO tasks (public_id, project_id, epic_id, key, "
                    "title, description, status, priority, component, proof_cmd, "
                    "status_note, section, owner, lease_expires_at, position, "
                    "version, created_at, updated_at, completed_at) VALUES "
                    "(:pub,:p,:eid,:k,:t,:d, CAST(:st AS task_status), "
                    "CAST(:pr AS priority), :comp,:proof,:sn,:sec,:own,:lease,"
                    ":pos,:ver,:ca,:ua,:comp_at) RETURNING id"
                ),
                {
                    "pub": t["public_id"], "p": pid,
                    "eid": epic_id.get(t.get("epic_key")),
                    "k": t.get("key"), "t": t["title"], "d": t.get("description"),
                    "st": t.get("status") or "todo", "pr": t.get("priority"),
                    "comp": t.get("component"), "proof": t.get("proof_cmd"),
                    "sn": t.get("status_note"),
                    "sec": t.get("section") or "backlog",
                    "own": t.get("owner"), "lease": t.get("lease_expires_at"),
                    "pos": t.get("position", 1000.0), "ver": t.get("version", 1),
                    "ca": t.get("created_at"), "ua": t.get("updated_at"),
                    "comp_at": t.get("completed_at"),
                },
            ).scalar()
            task_id[t["display_id"]] = tid
            # task tags
            for tag in t.get("tags") or []:
                cx.execute(
                    sa.text("INSERT INTO task_tags (task_id, tag_id) VALUES (:t,:g)"),
                    {"t": tid, "g": tag_id[tag]},
                )
            # commits
            for c in t.get("commits") or []:
                cx.execute(
                    sa.text(
                        "INSERT INTO commit_refs (task_id, sha, repo, "
                        "test_summary, created_at) VALUES (:t,:sha,:repo,:ts,:ca)"
                    ),
                    {
                        "t": tid, "sha": c["sha"], "repo": c.get("repo"),
                        "ts": c.get("test_summary"), "ca": c.get("created_at"),
                    },
                )
                n_commits += 1
        print(f"[tasks] inserted {len(task_id)}; commit_refs {n_commits}")

        # --- notes (task + epic) --------------------------------------------
        n_tn = n_en = skipped = 0
        for note in notes:
            if note.get("scope") == "epic":
                eid = epic_id.get(note.get("epic"))
                if eid is None:
                    skipped += 1
                    continue
                cx.execute(
                    sa.text(
                        "INSERT INTO epic_notes (epic_id, author, body, created_at)"
                        " VALUES (:e,:a,:b,:c)"
                    ),
                    {"e": eid, "a": note.get("author"), "b": note["body"],
                     "c": note.get("created_at")},
                )
                n_en += 1
            else:
                tid = task_id.get(note.get("task"))
                if tid is None:
                    skipped += 1
                    continue
                cx.execute(
                    sa.text(
                        "INSERT INTO task_notes (task_id, author, body, created_at)"
                        " VALUES (:t,:a,:b,:c)"
                    ),
                    {"t": tid, "a": note.get("author"), "b": note["body"],
                     "c": note.get("created_at")},
                )
                n_tn += 1
        print(f"[notes] task_notes {n_tn}; epic_notes {n_en}; skipped {skipped}")

        # --- reservations + counters ----------------------------------------
        maxval = {}
        for r in reservations:
            ns = r["namespace"]
            cx.execute(
                sa.text(
                    "INSERT INTO reservations (project_id, namespace, value, "
                    "reserved_by, note, created_at) VALUES (:p,:ns,:v,:by,:n,:c)"
                ),
                {"p": pid, "ns": ns, "v": r["value"], "by": r.get("reserved_by"),
                 "n": r.get("note"), "c": r.get("created_at")},
            )
            maxval[ns] = max(maxval.get(ns, 0), r["value"])
        for ns, v in maxval.items():
            cx.execute(
                sa.text(
                    "INSERT INTO counters (project_id, namespace, current_value) "
                    "VALUES (:p,:ns,:v) ON CONFLICT (project_id, namespace) "
                    "DO UPDATE SET current_value = GREATEST(counters.current_value, :v)"
                ),
                {"p": pid, "ns": ns, "v": v},
            )
        print(f"[reservations] inserted {len(reservations)}; counters set {maxval}")

    print("RESTORE COMPLETE")


if __name__ == "__main__":
    main()
