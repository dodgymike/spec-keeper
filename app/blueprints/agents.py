"""Per-project agent registry (ownership of tasks is by slug string)."""
from __future__ import annotations

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_api_key
from ..schemas import AgentIn, AgentOut

blp = Blueprint(
    "agents", __name__, url_prefix="/api/v1/projects/<slug>/agents",
    description="Per-project agent/actor registry.",
)


@blp.route("")
class AgentsCollection(MethodView):
    @blp.response(200, AgentOut(many=True))
    def get(self, slug):
        """List a project's registered agents."""
        require_api_key()
        return current_app.storage.list_agents(slug)

    @blp.arguments(AgentIn)
    @blp.response(201, AgentOut)
    def post(self, data, slug):
        """Register an agent in this project (idempotent upsert by slug)."""
        require_api_key()
        return current_app.storage.upsert_agent(slug, data)
