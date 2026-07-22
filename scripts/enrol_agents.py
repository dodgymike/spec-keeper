#!/usr/bin/env python3
"""Enrol AI-agent Cognito users for the deployed Spec Server (idempotent).

Why a script (not Terraform): the `aws_cognito_user` resource creates users via
AdminCreateUser WITHOUT a create-time password, which the pool rejects
("User is required to have a password") once MessageAction=SUPPRESS is in play,
and the PreSignUp trigger further blocks non-invite creation. The CLI path works:
AdminCreateUser(--temporary-password, SUPPRESS) — the PreSignUp handler bypasses
`PreSignUp_AdminCreateUser` — then AdminSetUserPassword(--permanent) so
USER_PASSWORD_AUTH returns tokens directly.

Idempotent: agents already present in the credentials secret are left untouched.
New agents are created, set to a strong permanent password, added to a group
(spec-admins for ADMINS, else spec-writers), and merged into the secret.

Usage:  python scripts/enrol_agents.py [agent-name ...]
        (no args => the full .claude/agents/ roster)
Env:    POOL_ID, REGION, SECRET_ID, AGENT_DOMAIN, ADMINS(comma) override defaults.
"""
from __future__ import annotations
import json, os, secrets, string, subprocess, sys, pathlib

POOL_ID = os.environ.get("POOL_ID", "eu-west-1_S1fUqxuKv")
REGION = os.environ.get("REGION", "eu-west-1")
SECRET_ID = os.environ.get("SECRET_ID", "spec-server-dev/agent-credentials")
DOMAIN = os.environ.get("AGENT_DOMAIN", "agents.spec-server.internal")
ADMINS = set((os.environ.get("ADMINS", "spec-keeper,aws-infra")).split(","))


def aws(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", *args], capture_output=True, text=True)


def genpw() -> str:
    alph = string.ascii_letters + string.digits
    return "Ag1!" + "".join(secrets.choice(alph) for _ in range(22))


def roster() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    d = pathlib.Path(__file__).resolve().parent.parent / ".claude" / "agents"
    return sorted(p.stem for p in d.glob("*.md"))


def main() -> int:
    sec = json.loads(
        aws("secretsmanager", "get-secret-value", "--secret-id", SECRET_ID,
            "--region", REGION, "--query", "SecretString", "--output", "text").stdout
    )
    users = sec.setdefault("users", {})
    created, skipped, failed = [], [], []
    for name in roster():
        if name in users:
            skipped.append(name)
            continue
        username = f"{name}@{DOMAIN}"
        group = "spec-admins" if name in ADMINS else "spec-writers"
        pw = genpw()
        r = aws("cognito-idp", "admin-create-user", "--user-pool-id", POOL_ID,
                "--region", REGION, "--username", username, "--message-action", "SUPPRESS",
                "--temporary-password", pw,
                "--user-attributes", f"Name=email,Value={username}", "Name=email_verified,Value=true")
        if r.returncode != 0 and "UsernameExistsException" not in r.stderr:
            failed.append((name, r.stderr.strip().splitlines()[-1][:140]))
            continue
        aws("cognito-idp", "admin-set-user-password", "--user-pool-id", POOL_ID,
            "--region", REGION, "--username", username, "--password", pw, "--permanent")
        aws("cognito-idp", "admin-add-user-to-group", "--user-pool-id", POOL_ID,
            "--region", REGION, "--username", username, "--group-name", group)
        users[name] = {"username": username, "password": pw, "groups": [group]}
        created.append(f"{name}->{group}")
    if created:
        aws("secretsmanager", "put-secret-value", "--secret-id", SECRET_ID,
            "--region", REGION, "--secret-string", json.dumps(sec))
    print(f"created={len(created)} skipped={len(skipped)} failed={len(failed)} | secret users={len(users)}")
    for c in created:
        print("  +", c)
    for n, e in failed:
        print("  ! FAIL", n, e)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
