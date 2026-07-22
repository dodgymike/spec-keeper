"""Unit tests for the pure resolution/mapping logic of the ISO-5 backfill script.

These cover the parts that do NOT need AWS/HTTP: role precedence, name derivation,
project-list parsing, and action planning. The live Cognito walk / API calls are
validated separately via a real `--dry-run`."""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "backfill_memberships",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "backfill_memberships.py",
)
bm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bm)


def test_highest_role_precedence():
    assert bm.highest_role({"spec-readers"}) == "reader"
    assert bm.highest_role({"spec-writers"}) == "writer"
    assert bm.highest_role({"spec-admins"}) == "admin"
    # multi-group collapses to the highest privilege
    assert bm.highest_role({"spec-writers", "spec-admins"}) == "admin"
    assert bm.highest_role({"spec-readers", "spec-writers"}) == "writer"


def test_highest_role_ignores_unknown_groups():
    assert bm.highest_role({"some-other-group"}) is None
    assert bm.highest_role(set()) is None
    assert bm.highest_role({"spec-readers", "unrelated"}) == "reader"


def test_agent_name_from_username():
    assert bm.agent_name_from_username("spec-keeper@agents.spec-server.internal") == "spec-keeper"
    assert bm.agent_name_from_username("aws-infra") == "aws-infra"


def test_is_agent_identity_filters_humans():
    # agents live under the enrolment domain; humans (personal email) are excluded
    assert bm.is_agent_identity("spec-keeper@agents.spec-server.internal") is True
    assert bm.is_agent_identity("dodgymike@gmail.com") is False
    assert bm.is_agent_identity(None) is False
    # empty domain disables the filter (seed everyone)
    orig = bm.AGENT_DOMAIN
    bm.AGENT_DOMAIN = ""
    try:
        assert bm.is_agent_identity("dodgymike@gmail.com") is True
    finally:
        bm.AGENT_DOMAIN = orig


def test_parse_projects_variants():
    assert bm.parse_projects([{"slug": "a"}, {"slug": "b"}]) == ["a", "b"]
    assert bm.parse_projects({"items": [{"slug": "x"}]}) == ["x"]
    # entries without a slug are ignored; non-list -> empty
    assert bm.parse_projects([{"name": "no slug"}, {"slug": "y"}]) == ["y"]
    assert bm.parse_projects({"unexpected": 1}) == []


def test_plan_actions_backfill_and_revoke():
    agents = {"spec-keeper": {"sub": "S1", "role": "admin"},
              "planner": {"sub": "S2", "role": "writer"}}
    posts = bm.plan_actions(agents, ["spec-server"], revoke=False)
    assert [a.verb for a in posts] == ["POST", "POST"]
    # agents are ordered deterministically (sorted by name)
    assert [a.name for a in posts] == ["planner", "spec-keeper"]
    assert posts[1].path() == "/api/v1/projects/spec-server/members"

    dels = bm.plan_actions(agents, ["spec-server"], revoke=True)
    assert [a.verb for a in dels] == ["DELETE", "DELETE"]
    # DELETE path targets the principal_sub
    assert dels[1].path() == "/api/v1/projects/spec-server/members/S1"


def test_action_describe_never_shows_secrets():
    a = bm.Action("POST", "spec-server", "SUB", "planner", "writer")
    d = a.describe()
    assert "principal_sub: SUB" in d and "role: writer" in d
