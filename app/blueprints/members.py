"""Project-membership management (ISO-3) — global-admin-gated.

Wires up the storage membership methods added (dormant) in ISO-1
(``list_members`` / ``add_member`` / ``remove_member``) as an HTTP surface:

  * ``GET    /api/v1/projects/<slug>/members``                  — list members.
  * ``POST   /api/v1/projects/<slug>/members``                  — add/update a
    member (idempotent upsert); 201 on create, 200 on update.
  * ``DELETE /api/v1/projects/<slug>/members/<principal_sub>``  — remove
    (idempotent) -> 204.

All three routes are gated on the GLOBAL admin permission (``spec-admins``) via
``require_api_key(required="admin")`` — the SAME mechanism the admin surface
(``app/blueprints/admin.py``) uses. Per-project authorization is a later task
(ISO-4); this task adds only global-admin-gated management of the records.

SECURITY INVARIANT: the authorization decision keys ONLY off the caller's
verified token (its ``cognito:groups``, checked inside ``require_api_key``). The
``principal_sub`` / ``principal_name`` / ``role`` in the request body are the
TARGET member's data and are NEVER used to authorize the CALLER — identity/authz
is never read from a request body or header.

Both backends raise ``NotFound`` (-> 404) for a missing project via the storage
layer, so an unknown ``<slug>`` yields 404 without an explicit lookup here. With
auth OFF (the local-only default, no ``COGNITO_ISSUER``) ``require_api_key`` is a
no-op, so these behave like every other endpoint locally.
"""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_api_key
from ..schemas import MemberIn, MemberOut

blp = Blueprint(
    "members", __name__, url_prefix="/api/v1/projects/<slug>/members",
    description="Project membership (principal -> role); global-admin-gated.",
)


@blp.route("")
class MembersCollection(MethodView):
    @blp.response(200, MemberOut(many=True))
    def get(self, slug):
        """List a project's members. 404 if the project does not exist."""
        require_api_key(required="admin")
        return current_app.storage.list_members(slug)

    @blp.arguments(MemberIn)
    @blp.response(201, MemberOut)
    def post(self, data, slug):
        """Add or update a member (idempotent upsert): 201 on create, 200 on
        update. 404 if the project does not exist.

        ``data`` (validated by ``MemberIn``: ``role`` must be one of
        reader/writer/admin, else 422) describes the TARGET member — never the
        caller, who is authorized solely by their verified token's groups."""
        require_api_key(required="admin")
        storage = current_app.storage
        existed = storage.get_membership(slug, data["principal_sub"]) is not None
        member = storage.add_member(
            slug, data["principal_sub"], data.get("principal_name"), data["role"],
        )
        return (member, 200) if existed else member


@blp.route("/<principal_sub>")
class MemberItem(MethodView):
    @blp.response(204)
    def delete(self, slug, principal_sub):
        """Remove a member (idempotent). 404 if the project does not exist."""
        require_api_key(required="admin")
        current_app.storage.remove_member(slug, principal_sub)
        return ""
