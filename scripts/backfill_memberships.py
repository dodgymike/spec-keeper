#!/usr/bin/env python3
"""Idempotent, reversible project-membership backfill (ISO-5).

Before per-project isolation is *enforced* (ISO-6: ``PROJECT_ISOLATION_ENFORCED``
flipped ON), every existing agent must already be a member of every existing
project — otherwise a non-admin agent is instantly locked out of a backlog it
was happily using. This script seeds those memberships through the deployed Spec
Server API so the flip is a no-op for well-behaved callers.

What it does
------------
1. **Resolve** each agent's immutable Cognito ``sub`` and group(s) by walking the
   three role groups (``spec-admins`` / ``spec-writers`` / ``spec-readers``) of the
   user pool, and map each to a single project role by highest privilege::

       spec-admins  -> admin      (admin > writer > reader; the highest wins)
       spec-writers -> writer
       spec-readers -> reader

2. **Enumerate** existing projects via ``GET /api/v1/projects`` with an admin
   bearer token (an admin sees every project).

3. **Upsert** membership for each (project, agent) via
   ``POST /api/v1/projects/<slug>/members`` with body
   ``{principal_sub, principal_name, role}``. The endpoint is an idempotent upsert,
   so re-runs are no-ops (201 on first create, 200 on update).

Flags
-----
  ``--dry-run``   (DEFAULT) Resolve subs + enumerate projects + PRINT every planned
                  action. Writes NOTHING — never calls the members endpoint.
  ``--apply``     Actually perform the writes (POST, or DELETE with ``--revoke``).
  ``--revoke``    Inverse direction: DELETE each membership this run would create
                  (``DELETE /api/v1/projects/<slug>/members/<principal_sub>``).
                  Idempotent. Combine with ``--apply`` to execute; on its own it is
                  a dry-run of the revocation.
  ``--project S`` Limit scope to a single project slug (repeatable).

Auth & safety
-------------
- The admin bearer token is minted in-memory by ``agent_token`` (Cognito
  ``USER_PASSWORD_AUTH``) and is NEVER printed, logged, or placed in a traceback.
  Select the admin identity with ``AGENT_USERNAME=spec-keeper`` (or ``aws-infra``)
  and point ``AGENT_CREDENTIALS_SECRET_ARN`` / ``AGENT_CREDENTIALS_SECRET`` at the
  ``spec-server-dev/agent-credentials`` secret (same as every other agent call).
- ``principal_sub`` is treated as an opaque identity key.
- Idempotent (server upserts) and reversible (``--revoke``). Failures are reported
  generically per-item; the run continues and exits non-zero if any item failed.

Deploy ordering (IMPORTANT)
--------------------------
The ISO-1..4 members endpoint is NOT live until the ISO deploy wave. Run
``--dry-run`` any time to preview; run ``--apply`` only AFTER that deploy and
BEFORE flipping ``PROJECT_ISOLATION_ENFORCED`` ON. See
``scripts/README-backfill-memberships.md``.

Env: ``POOL_ID`` (default eu-west-1_S1fUqxuKv), ``REGION`` (eu-west-1),
``SPEC_API_BASE`` (https://api.spec.elasticninja.com), ``AGENT_DOMAIN``
(agents.spec-server.internal) override the defaults.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse

# Import the token helper that lives next to this script.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import agent_token  # noqa: E402  (path set above)

POOL_ID = os.environ.get("POOL_ID", "eu-west-1_S1fUqxuKv")
REGION = os.environ.get("REGION", "eu-west-1")
API_BASE = os.environ.get("SPEC_API_BASE", "https://api.spec.elasticninja.com").rstrip("/")
AGENT_DOMAIN = os.environ.get("AGENT_DOMAIN", "agents.spec-server.internal")

# Cognito group -> project role, and the privilege order used to pick the highest
# role when an agent belongs to more than one group.
GROUP_ROLE = {"spec-admins": "admin", "spec-writers": "writer", "spec-readers": "reader"}
ROLE_RANK = {"reader": 0, "writer": 1, "admin": 2}

# Cloudflare fronts the API and blocks the default urllib User-Agent (error 1010),
# so every call carries an explicit, non-secret UA.
USER_AGENT = "spec-keeper-backfill/1.0"


# --------------------------------------------------------------------------- #
# Pure resolution / mapping logic (unit-testable; no I/O)
# --------------------------------------------------------------------------- #
def highest_role(groups) -> str | None:
    """Return the highest-privilege project role for a set of Cognito groups.

    Unknown groups are ignored. Returns ``None`` if no group maps to a role."""
    roles = [GROUP_ROLE[g] for g in groups if g in GROUP_ROLE]
    if not roles:
        return None
    return max(roles, key=lambda r: ROLE_RANK[r])


def agent_name_from_username(username: str) -> str:
    """Derive the display agent-name from a Cognito username or email.

    Enrolment records the agent's email as ``<name>@<AGENT_DOMAIN>``; the display
    name is the local part. If there is no domain suffix, the value is used as-is."""
    return username.split("@", 1)[0] if "@" in username else username


def is_agent_identity(email: str | None) -> bool:
    """True if this principal is an enrolled AGENT (email under ``AGENT_DOMAIN``).

    Human operators (e.g. a personal email in ``spec-admins``) are excluded — they
    are global admins and are never locked out by isolation, so they need no
    per-project seed. Set ``AGENT_DOMAIN=""`` to disable the filter (seed all)."""
    if not AGENT_DOMAIN:
        return True
    return bool(email) and email.endswith("@" + AGENT_DOMAIN)


def parse_projects(payload) -> list[str]:
    """Extract project slugs from a ``GET /api/v1/projects`` JSON body.

    Accepts either a bare list of project objects or ``{"items": [...]}`` and
    ignores entries without a ``slug``."""
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [p["slug"] for p in items if isinstance(p, dict) and p.get("slug")]


class Action:
    """A single planned membership change (POST upsert or DELETE revoke)."""

    __slots__ = ("verb", "slug", "sub", "name", "role")

    def __init__(self, verb: str, slug: str, sub: str, name: str, role: str) -> None:
        self.verb = verb
        self.slug = slug
        self.sub = sub
        self.name = name
        self.role = role

    def path(self) -> str:
        # Path components are trusted today (slugs from the admin projects API,
        # subs are Cognito UUIDs) but are quoted as defence-in-depth.
        slug = urllib.parse.quote(self.slug, safe="")
        base = f"/api/v1/projects/{slug}/members"
        if self.verb == "DELETE":
            return f"{base}/{urllib.parse.quote(self.sub, safe='')}"
        return base

    def describe(self) -> str:
        if self.verb == "DELETE":
            return f"DELETE {self.path()}  ({self.name})"
        body = f'{{principal_sub: {self.sub}, principal_name: {self.name}, role: {self.role}}}'
        return f"POST {self.path()}  {body}"


def plan_actions(agents: dict, project_slugs: list[str], *, revoke: bool) -> list[Action]:
    """Build the ordered list of planned actions for (project x agent).

    ``agents`` maps agent-name -> {"sub", "role"}. ``revoke`` flips POST -> DELETE."""
    verb = "DELETE" if revoke else "POST"
    actions: list[Action] = []
    for slug in project_slugs:
        for name in sorted(agents):
            a = agents[name]
            actions.append(Action(verb, slug, a["sub"], name, a["role"]))
    return actions


# --------------------------------------------------------------------------- #
# Live I/O (Cognito walk + Spec Server calls)
# --------------------------------------------------------------------------- #
def resolve_agents() -> dict:
    """Walk the three role groups and return name -> {"sub", "role", "groups"}.

    Uses boto3 (signed, needs ``cognito-idp:ListUsersInGroup``). An agent in
    multiple groups is collapsed to a single highest-privilege role."""
    import boto3

    idp = boto3.client("cognito-idp", region_name=REGION)
    by_sub: dict[str, dict] = {}
    for group in GROUP_ROLE:
        paginator = idp.get_paginator("list_users_in_group")
        for page in paginator.paginate(UserPoolId=POOL_ID, GroupName=group):
            for user in page.get("Users", []):
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                sub = attrs.get("sub") or user.get("Username")
                if not sub:
                    continue
                email = attrs.get("email")
                if not is_agent_identity(email):
                    continue  # skip human operators / non-agent principals
                name = agent_name_from_username(email or user.get("Username", sub))
                rec = by_sub.setdefault(sub, {"sub": sub, "name": name, "groups": set()})
                rec["groups"].add(group)
    agents: dict[str, dict] = {}
    for rec in by_sub.values():
        role = highest_role(rec["groups"])
        if role is None:
            continue
        agents[rec["name"]] = {"sub": rec["sub"], "role": role, "groups": sorted(rec["groups"])}
    return agents


def _get_json(path: str):
    """Authorized GET returning parsed JSON, or raise RuntimeError (no token leak)."""
    status, body = agent_token.authorized_request(
        "GET", f"{API_BASE}{path}", headers={"User-Agent": USER_AGENT})
    if status != 200:
        raise RuntimeError(f"GET {path} -> HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"GET {path}: could not parse JSON ({type(exc).__name__})") from None


def resolve_projects(only: list[str] | None) -> list[str]:
    """Enumerate project slugs via the admin-visible projects list, honouring --project."""
    slugs = parse_projects(_get_json("/api/v1/projects"))
    if only:
        wanted = set(only)
        slugs = [s for s in slugs if s in wanted]
    return sorted(slugs)


def execute(action: Action) -> tuple[bool, str]:
    """Perform one live action. Returns (ok, http_status_or_error). No secret leak."""
    try:
        if action.verb == "DELETE":
            status, _ = agent_token.authorized_request(
                "DELETE", f"{API_BASE}{action.path()}",
                headers={"User-Agent": USER_AGENT})
            ok = status in (200, 204)
        else:
            payload = json.dumps(
                {"principal_sub": action.sub, "principal_name": action.name, "role": action.role}
            ).encode("utf-8")
            status, _ = agent_token.authorized_request(
                "POST", f"{API_BASE}{action.path()}", data=payload,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            ok = status in (200, 201)
        return ok, f"HTTP {status}"
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        # Generic failure text — never surfaces token/password material.
        return False, f"error ({type(exc).__name__})"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill (or revoke) project memberships for all agents (ISO-5).",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Preview only (DEFAULT when --apply is absent); writes nothing.")
    p.add_argument("--apply", action="store_true",
                   help="Actually perform the writes. Without this the run is a dry-run.")
    p.add_argument("--revoke", action="store_true",
                   help="Inverse: DELETE each membership this run would create.")
    p.add_argument("--project", action="append", default=None, metavar="SLUG",
                   help="Limit to a project slug (repeatable). Default: all projects.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    apply = args.apply and not args.dry_run
    mode = "APPLY" if apply else "DRY-RUN"
    action_word = "REVOKE" if args.revoke else "BACKFILL"
    print(f"== membership {action_word} [{mode}] ==  api={API_BASE}  pool={POOL_ID}")

    # 1) Resolve agents from Cognito (works live regardless of deploy state).
    try:
        agents = resolve_agents()
    except Exception as exc:  # noqa: BLE001 - generic: never surface AWS/secret detail
        print(f"\nERROR: could not resolve agents from Cognito ({type(exc).__name__}).",
              file=sys.stderr)
        return 2
    print(f"\nResolved {len(agents)} agents from Cognito:")
    for name in sorted(agents):
        a = agents[name]
        print(f"  {name:<24} {a['role']:<7} sub={a['sub']}  groups={','.join(a['groups'])}")

    # 2) Enumerate projects (needs an admin token; the projects endpoint is live).
    try:
        slugs = resolve_projects(args.project)
    except (RuntimeError, urllib.error.URLError, agent_token.TokenError) as exc:
        print(f"\nERROR: could not enumerate projects: {exc}", file=sys.stderr)
        return 2
    print(f"\nProjects in scope ({len(slugs)}): {', '.join(slugs) or '(none)'}")

    # 3) Plan.
    actions = plan_actions(agents, slugs, revoke=args.revoke)
    print(f"\nPlanned actions: {len(actions)}")
    if not apply:
        for act in actions:
            print(f"  {act.describe()}")
        print(f"\nDRY-RUN: nothing written. Re-run with --apply to execute "
              f"{len(actions)} action(s).")
        return 0

    # 4) Apply.
    ok_n = fail_n = 0
    failures: list[str] = []
    for act in actions:
        ok, status = execute(act)
        if ok:
            ok_n += 1
        else:
            fail_n += 1
            failures.append(f"{act.verb} {act.path()} -> {status}")
        print(f"  {'ok ' if ok else 'FAIL'} {act.verb} {act.path()}  [{status}]")
    print(f"\n{action_word} done: ok={ok_n} failed={fail_n}")
    for f in failures:
        print(f"  ! {f}", file=sys.stderr)
    return 1 if fail_n else 0


if __name__ == "__main__":
    raise SystemExit(main())
