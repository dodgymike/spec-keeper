"""Cross-backend parity for project membership (ISO-1).

Membership is a DORMANT storage entity — no route wires it up and nothing
enforces authorization from it yet — so these tests drive ``app.storage``
directly (there is no HTTP surface to go through). The ``app`` fixture is
parametrised over both backends, so every assertion below is proven identical on
the Postgres reference adapter AND the DynamoDB adapter (DynamoDB Local), which
is the ISO-1 backend-parity requirement.
"""
from __future__ import annotations


def _norm(m):
    """Comparable view of a MemberDTO, normalising created_at to a naive UTC
    instant. (A freshly-built DTO carries a tz-aware ``utcnow()``; a Postgres
    re-read from a ``TIMESTAMP WITHOUT TIME ZONE`` column is naive — a pre-existing
    codebase quirk, unrelated to membership, so we compare the instant not the
    tzinfo.)"""
    ca = m.created_at.replace(tzinfo=None) if m.created_at else None
    return (m.project_slug, m.principal_sub, m.principal_name, m.role, ca)


def _mk_project(app, slug):
    c = app.test_client()
    assert c.post("/api/v1/projects",
                  json={"slug": slug, "name": slug.title()}).status_code == 201


def test_add_get_and_idempotent_role_update(app, project):
    """add -> get, then a re-add updates role/name in place (idempotent upsert)
    keeping the original created_at — identical on both backends."""
    with app.app_context():
        s = app.storage
        assert s.get_membership("demo", "sub-1") is None

        m = s.add_member("demo", "sub-1", "Alice", "reader")
        assert (m.project_slug, m.principal_sub, m.principal_name, m.role) == \
            ("demo", "sub-1", "Alice", "reader")
        assert m.created_at is not None

        got = s.get_membership("demo", "sub-1")
        assert got is not None
        assert _norm(got) == _norm(m)

        # idempotent re-add: same (project, principal_sub) -> updates role/name,
        # does NOT create a second row, and preserves created_at.
        m2 = s.add_member("demo", "sub-1", "Alice Cooper", "admin")
        assert m2.role == "admin"
        assert m2.principal_name == "Alice Cooper"
        # created_at preserved (same instant; tzinfo normalised, see _norm).
        assert m2.created_at.replace(tzinfo=None) == m.created_at.replace(tzinfo=None)
        assert len(s.list_members("demo")) == 1


def test_list_members_and_remove(app, project):
    with app.app_context():
        s = app.storage
        s.add_member("demo", "sub-b", "Bob", "writer")
        s.add_member("demo", "sub-a", "Ann", "reader")
        s.add_member("demo", "sub-c", None, "admin")

        members = s.list_members("demo")
        assert [m.principal_sub for m in members] == ["sub-a", "sub-b", "sub-c"]
        # principal_name is nullable/informational only.
        assert members[2].principal_name is None
        assert members[0].role == "reader"

        s.remove_member("demo", "sub-b")
        assert s.get_membership("demo", "sub-b") is None
        assert [m.principal_sub for m in s.list_members("demo")] == ["sub-a", "sub-c"]

        # remove is idempotent: removing an absent principal is a no-op.
        s.remove_member("demo", "sub-b")
        assert len(s.list_members("demo")) == 2


def test_list_projects_for_principal_across_projects(app, project):
    """A principal belonging to multiple projects is listed once per project
    (with that project's role), sorted by slug — the GSI6 access pattern, proven
    identical on both backends."""
    _mk_project(app, "alpha")
    _mk_project(app, "beta")
    with app.app_context():
        s = app.storage
        s.add_member("demo", "shared-sub", "Zoe", "reader")
        s.add_member("alpha", "shared-sub", "Zoe", "writer")
        s.add_member("beta", "shared-sub", "Zoe", "admin")
        # a different principal that must NOT appear in shared-sub's listing.
        s.add_member("alpha", "other-sub", "Nate", "reader")

        rows = s.list_projects_for_principal("shared-sub")
        assert [(r.project_slug, r.role) for r in rows] == [
            ("alpha", "writer"), ("beta", "admin"), ("demo", "reader"),
        ]
        assert all(r.principal_sub == "shared-sub" for r in rows)

        # removing one membership drops exactly that project from the listing.
        s.remove_member("alpha", "shared-sub")
        rows = s.list_projects_for_principal("shared-sub")
        assert [r.project_slug for r in rows] == ["beta", "demo"]


def test_membership_is_dormant_no_route(app, project):
    """ISO-1 adds NO HTTP surface: there is no members route (a no-op on all
    existing behaviour). Guards against an accidental blueprint wiring."""
    c = app.test_client()
    assert c.get("/api/v1/projects/demo/members").status_code == 404
